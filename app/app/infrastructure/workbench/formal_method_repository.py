"""Admission run storage and projections of KnowLab-owned frozen versions."""

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.workbench.errors import IdempotencyConflict, NotFound, StaleStateVersion
from app.domain.workbench.native_registry import NATIVE_ACTIONS


class FormalMethodRepositoryMixin:
    session: AsyncSession

    async def incubation_asset(self, owner, reference):
        return await self.session.scalar(
            text("""SELECT asset_id FROM workbench_incubation_tool_bindings
        WHERE owner_actor_id=:owner AND method_id=:method_id AND version=:version AND digest=:digest"""),
            {"owner": owner, **reference.model_dump()},
        )

    async def select_incubation_asset(self, owner, reference, asset_id):
        await self.session.execute(
            text("""INSERT INTO workbench_incubation_tool_bindings
        (owner_actor_id,method_id,version,digest,asset_id) VALUES(:owner,:method_id,:version,:digest,:asset)
        ON CONFLICT(owner_actor_id,method_id,version) DO UPDATE SET asset_id=EXCLUDED.asset_id
        WHERE workbench_incubation_tool_bindings.digest=EXCLUDED.digest"""),
            {"owner": owner, **reference.model_dump(), "asset": asset_id},
        )

    async def active_source_tasks(self, sandbox_ids):
        rows = await self.session.scalars(
            text("""SELECT t.id FROM workbench_tasks t JOIN workbench_sessions s
            ON s.id=t.session_id AND s.active_task_id=t.id
            WHERE t.pack_source='registry-source' AND s.sandbox_id=ANY(CAST(:sandboxes AS uuid[]))
            AND t.state IN ('RECEIVED','VALIDATING','QUEUED','RUNNING')
            AND NOT ((t.original_input->'client_context') ? 'queued_behind')"""),
            {"sandboxes": list(sandbox_ids)},
        )
        return [str(task_id) for task_id in rows]

    async def scene_function_revisions(self, bindings, *, scene):
        from app.application.errors.exceptions import ConcurrencyConflictError
        from app.domain.workbench.capabilities import CapabilityError
        from app.infrastructure.workbench.service_functions import checked_revision

        rows = await self.session.execute(
            text("""
            SELECT f.* FROM workbench_registry_service_functions f
            JOIN jsonb_to_recordset(CAST(:bindings AS jsonb)) AS b(provider text,name text,version int)
            USING (provider,name,version)
            WHERE f.contract->'scenes' @> CAST(:scene AS jsonb)
            ORDER BY f.provider, f.name
        """),
            {
                "scene": json.dumps([scene]),
                "bindings": json.dumps(
                    [
                        dict(provider=p, name=n, version=v)
                        for n, (p, v) in bindings.items()
                    ]
                ),
            },
        )
        try:
            return [checked_revision(row) for row in rows.mappings()]
        except (ValueError, ConcurrencyConflictError) as exc:
            raise CapabilityError("Invalid service function registration") from exc

    async def incubation_function_revisions(self, identities):
        from app.infrastructure.workbench.service_functions import checked_revision

        result = await self.session.execute(
            text("""
            SELECT f.* FROM workbench_registry_service_functions f
            JOIN jsonb_to_recordset(CAST(:identities AS jsonb))
              AS pin(source text, entry text, version text, digest text)
              ON (f.provider,f.name,f.version::text,f.digest) =
                 (pin.source,pin.entry,pin.version,pin.digest)
            WHERE f.contract->'scenes' @> '["incubation"]'::jsonb
            ORDER BY f.provider,f.name,f.version DESC
        """),
            {"identities": json.dumps([pin.model_dump() for pin in identities])},
        )
        return [checked_revision(row) for row in result.mappings()]

    async def direct_registrations(self, owner):
        rows = await self.session.execute(
            text("""
            SELECT source_id,version,kind,name,digest FROM workbench_registry_sources
            WHERE owner_actor_id=:owner AND withdrawn_at IS NULL ORDER BY name, source_id, version DESC
        """),
            {"owner": owner},
        )
        return rows.mappings().all()

    async def direct_revision(self, owner, source_id, version, digest):
        row = (
            (
                await self.session.execute(
                    text("""
            SELECT source_id,version,kind,name,digest,content FROM workbench_registry_sources
            WHERE owner_actor_id=:owner AND source_id=:id AND version=:version AND digest=:digest AND withdrawn_at IS NULL FOR SHARE
        """),
                    {
                        "owner": owner,
                        "id": source_id,
                        "version": version,
                        "digest": digest,
                    },
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("Registered source revision not found")
        return row

    async def lock_direct_source(self, owner, source_id):
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 71))"),
            {"key": f"{owner}:{source_id}"},
        )

    async def withdraw_direct_source(self, owner, source_id, version, digest):
        await self.lock_direct_source(owner, source_id)
        params = {"owner": owner, "id": source_id}
        latest = (
            (
                await self.session.execute(
                    text("""
            SELECT version,digest,withdrawn_at FROM workbench_registry_sources
            WHERE owner_actor_id=:owner AND source_id=:id ORDER BY version DESC LIMIT 1
        """),
                    params,
                )
            )
            .mappings()
            .first()
        )
        if not latest:
            raise NotFound("Registered source not found")
        if latest["version"] != version or latest["digest"] != digest:
            raise StaleStateVersion("Registered source changed before withdrawal")
        if latest["withdrawn_at"] is None:
            await self.session.execute(
                text("""
                UPDATE workbench_registry_sources SET withdrawn_at=now()
                WHERE owner_actor_id=:owner AND source_id=:id AND withdrawn_at IS NULL
            """),
                params,
            )

    async def register_direct_revision(self, owner, source_id, version, draft):
        params = {
            "owner": owner,
            "id": source_id,
            "version": version,
            **draft.model_dump(),
            "digest": draft.content_digest(),
        }
        await self.lock_direct_source(owner, source_id)
        # A retry of any committed revision succeeds even after a newer edit.
        previous = (
            (
                await self.session.execute(
                    text("""
            SELECT source_id,version,kind,name,digest,withdrawn_at FROM workbench_registry_sources
            WHERE owner_actor_id=:owner AND source_id=:id AND version<=:version
            ORDER BY version DESC LIMIT 1
        """),
                    params,
                )
            )
            .mappings()
            .first()
        )
        if previous and previous["withdrawn_at"] is not None:
            raise StaleStateVersion("Registered source was withdrawn")
        if previous and previous["version"] == version:
            if any(previous[k] != params[k] for k in ("name", "kind", "digest")):
                raise IdempotencyConflict(
                    "This source version already has different content"
                )
            return {
                k: previous[k]
                for k in ("source_id", "version", "kind", "name", "digest")
            }
        expected = previous["version"] + 1 if previous else 1
        if version != expected or (previous and previous["kind"] != draft.kind):
            raise StaleStateVersion(
                "Expected the next revision of the same registered source"
            )
        return (
            (
                await self.session.execute(
                    text("""
            INSERT INTO workbench_registry_sources(owner_actor_id, source_id, version, kind, name, content, digest)
            VALUES (:owner,:id,:version,:kind,:name,:content,:digest)
            RETURNING source_id,version,kind,name,digest
        """),
                    params,
                )
            )
            .mappings()
            .one()
        )

    async def directory_page(self, owner, kind, query_digest, offset, limit):
        row = (
            (
                await self.session.execute(
                    text("""
            SELECT counts, jsonb_array_length(entries) AS total,
                ARRAY(SELECT entries->index FROM generate_series(
                    CAST(:offset AS integer), least(CAST(:offset AS bigint) + :limit - 1, jsonb_array_length(entries)-1)::int
                ) AS index) AS items
            FROM workbench_directory_pages WHERE owner_id=:owner AND kind=:kind
                AND query_digest=:query AND expires_at > now()"""),
                    dict(
                        owner=owner,
                        kind=kind,
                        query=query_digest,
                        offset=offset,
                        limit=limit,
                    ),
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row else None

    async def save_directory_pages(self, owner, kind, query_digest, entries, counts):
        await self.session.execute(
            text("DELETE FROM workbench_directory_pages WHERE expires_at <= now()")
        )
        await self.session.execute(
            text("""INSERT INTO workbench_directory_pages
            VALUES (:owner,:kind,:query,CAST(:entries AS jsonb),CAST(:counts AS jsonb),now()+interval '5 minutes')
            ON CONFLICT(owner_id,kind) DO UPDATE SET query_digest=EXCLUDED.query_digest, entries=EXCLUDED.entries,
                counts=EXCLUDED.counts, expires_at=EXCLUDED.expires_at"""),
            dict(
                owner=owner,
                kind=kind,
                query=query_digest,
                entries=json.dumps(entries),
                counts=json.dumps(counts),
            ),
        )

    async def registry_method_candidates(
        self,
        owner,
        limit,
        offset,
        *,
        latest=False,
        public=False,
        terms=(),
        matching=None,
        topic_refs=(),
        host_only=False,
        installed_digests=None,
        experts_only=False,
        reference=None,
        identities_only=False,
        definitions=False,
        eligible=None,
    ):
        # All metadata is projected from the admitted document. Public rows are
        # internal candidates until the market has confirmed the consumer's rights.
        selection = "DISTINCT ON (method_id)" if latest else ""
        pin = (
            "AND method_id=:id AND version=:version AND digest=:digest"
            if reference
            else ""
        )
        columns = "method_id,latest_version,digest"
        if not identities_only:
            columns += ",created_at,method_kind,title,summary"
        if reference or definitions:
            columns += ",definition"
        grant_filter = (
            """AND (method_id,version,digest) IN (
            SELECT g.method_id,g.version,g.digest FROM jsonb_to_recordset(CAST(:eligible AS jsonb))
            AS g(method_id uuid, version int, digest text))"""
            if eligible is not None
            else ""
        )
        installation_filter = (
            "AND document#>>'{draft,source,digest}' = ANY(CAST(:installed AS text[]))"
            if installed_digests is not None
            else ""
        )
        # Tokens are materialized once per frozen version. Count distinct query
        # terms so repeated words in a long declaration do not dominate.
        hit = "(capability_terms @@ to_tsquery('simple', :query))"
        score = """(SELECT count(*) FROM unnest(CAST(:terms AS text[])) AS t(word)
                           WHERE capability_terms @@ plainto_tsquery('simple', word))"""
        stable_order = "created_at DESC,method_id,latest_version DESC"
        ranked_query = f"SELECT {columns} FROM candidates"
        order = f"{score} DESC, {stable_order}" if terms else stable_order
        if topic_refs:
            topic = """(method_id,latest_version,digest) IN (
                SELECT f.method_id, f.version, f.digest FROM jsonb_to_recordset(CAST(:topics AS jsonb))
                AS f(method_id uuid, version int, digest text))"""
            # Within the same lexical score, alternate two facet hits with one
            # cross-facet candidate. Either lane can supply the rest on its own.
            ranked_query = f"""
                , scored AS (SELECT candidates.*, {score} AS relevance, ({topic}) AS in_topic FROM candidates),
                lanes AS (SELECT scored.*, row_number() OVER (
                    PARTITION BY relevance,in_topic ORDER BY {stable_order}) - 1 AS pos FROM scored)
                SELECT {columns} FROM lanes"""
            order = f"""relevance DESC, CASE WHEN in_topic THEN (pos/2)*3 + pos%2
                      ELSE pos*3+2 END, {stable_order}"""
            hit = f"({hit} OR {topic})"
        match_filter = (
            "" if matching is None else "AND " + ("" if matching else "NOT ") + hit
        )
        rows = await self.session.execute(
            text(f"""
          WITH versions AS (SELECT {selection} method_id, version AS latest_version, digest, created_at,
            CASE document#>>'{{draft,definition,procedure,kind}}'
              WHEN 'direct' THEN 'tool' WHEN 'orchestration' THEN 'expert' END AS method_kind,
            document#>>'{{draft,definition,capability_declaration,title}}' AS title,
            left(document#>>'{{draft,definition,capability_declaration,purpose}}',500) AS summary,
            document#>'{{draft,definition}}' AS definition,
            capability_terms
          FROM workbench_formal_methods WHERE caller='knowlab' {pin} {grant_filter} {installation_filter}
            AND document#>>'{{reference,digest}}' = digest
            AND document#>>'{{reference,method_id}}' = method_id::text
            AND document#>>'{{reference,version}}' = version::text
            AND (owner_actor_id=:owner OR (:public AND document->>'visibility'='public'))
            AND (NOT :require_host OR document#>'{{draft,allowed_callers}}' ? 'host')
          ORDER BY method_id,version DESC),
          candidates AS (SELECT * FROM versions WHERE (NOT :experts_only OR method_kind='expert') {match_filter})
          {ranked_query} ORDER BY {order} LIMIT :limit OFFSET :offset
        """),
            {
                "owner": owner,
                "limit": limit,
                "offset": offset,
                "public": public,
                "require_host": public or host_only,
                "installed": list(installed_digests or ()),
                "terms": list(terms),
                "topics": json.dumps(list(topic_refs)),
                "query": " | ".join(terms),
                "eligible": json.dumps(eligible),
                "experts_only": experts_only,
                **(
                    {
                        "id": reference.method_id,
                        "version": reference.version,
                        "digest": reference.digest,
                    }
                    if reference
                    else {}
                ),
            },
        )
        return [dict(r) for r in rows.mappings()]

    async def registry_inventory(self, owner):
        """Project only user-facing metadata. No credentials, file bodies or test evidence."""
        params = {"owner": owner}
        installations = await self.session.execute(
            text("""
            SELECT id AS asset_id, name, kind, revision, status, detail->>'account_label' AS account_label
            FROM workbench_capability_assets WHERE owner_actor_id=:owner ORDER BY name,id
        """),
            params,
        )
        methods = await self.session.execute(
            text("""
            SELECT document->'reference' AS reference,
                   document#>>'{draft,definition,procedure,kind}' AS procedure_kind,
                   document#>'{draft,allowed_callers}' AS allowed_callers,
                   document#>>'{draft,definition,capability_declaration,title}' AS title,
                   document#>>'{draft,source,asset_id}' AS source_asset_id,
                   CASE WHEN document#>>'{draft,source,kind}' IN ('registered_script','registered_instruction','service_function')
                        THEN (document#>>'{draft,source,version}')::int
                        ELSE (document#>>'{draft,source,asset_revision}')::int END AS source_revision,
                   document#>>'{draft,source,entry}' AS source_entry,
                   document#>>'{draft,source,kind}' AS source_kind,
                   document#>>'{draft,source,digest}' AS source_digest,
                   document#>'{validation,dependencies}' AS dependencies
            FROM workbench_formal_methods WHERE owner_actor_id=:owner ORDER BY title,method_id,version DESC
        """),
            params,
        )
        return {
            "installed": installations.mappings().all(),
            "formal": methods.mappings().all(),
        }

    async def registry_activity(self, owner):
        """Recent metadata from original records. Trial sessions are private to incubation."""
        result = await self.session.execute(
            text("""
            SELECT * FROM (
              (SELECT 'call' AS kind, c.session_id, c.id::text AS record_id,
                      CASE WHEN c.detail ? 'formal_method' OR c.detail ? 'formal_host' OR c.detail ? 'frozen' THEN 'formal'
                           WHEN c.source_asset_id IS NOT NULL AND c.origin='managed' THEN 'installed'
                           WHEN c.origin='platform' THEN 'platform' ELSE 'runtime' END AS path,
                      c.source_asset_id AS asset_id, coalesce(c.detail#>'{formal_method,reference}',
                        c.detail#>'{formal_host,reference}', c.detail#>'{frozen,reference}') AS reference,
                      coalesce(c.detail#>>'{tool,name}', c.kind) AS action, c.status, c.created_at AS at, NULL::jsonb AS source
               FROM workbench_calls c JOIN workbench_sessions s ON s.id=c.session_id
               WHERE s.owner_actor_id=:owner AND c.owner_actor_id=:owner AND coalesce(s.origin,'') <> 'knowlab-test'
                 AND c.detail->>'execution_kind' IS DISTINCT FROM 'registry_script'
               ORDER BY c.created_at DESC,c.id DESC LIMIT 40)
              UNION ALL
              (SELECT 'task',t.session_id,t.id::text,'registered_source',NULL::uuid,NULL::jsonb,
                t.pack_snapshot#>>'{source,name}',t.state,t.updated_at,t.pack_snapshot->'source'
               FROM workbench_tasks t JOIN workbench_sessions s ON s.id=t.session_id
               WHERE t.pack_source='registry-source' AND t.owner_actor_id=:owner AND s.owner_actor_id=:owner
               ORDER BY t.updated_at DESC,t.id DESC LIMIT 40)
              UNION ALL
              (SELECT 'observation', e.session_id, e.id::text, 'native', NULL::uuid, NULL::jsonb, cast(:native_items AS jsonb) ->> (e.payload#>>'{item,type}'),
                      coalesce(e.payload#>>'{item,status}', e.event_type), e.created_at, NULL::jsonb
               FROM workbench_session_events e JOIN workbench_sessions s ON s.id=e.session_id
               WHERE s.owner_actor_id=:owner AND coalesce(s.origin,'') <> 'knowlab-test'
                 AND e.event_type IN ('item/started','item/completed') AND cast(:native_items AS jsonb) ? (e.payload#>>'{item,type}')
               ORDER BY e.created_at DESC,e.id DESC LIMIT 40)
              UNION ALL
              (SELECT 'approval', a.session_id, a.id::text, 'native', NULL::uuid, NULL::jsonb, a.method, a.decision, a.created_at, NULL::jsonb
               FROM workbench_approval_log a JOIN workbench_sessions s ON s.id=a.session_id
               WHERE s.owner_actor_id=:owner AND coalesce(s.origin,'') <> 'knowlab-test'
               ORDER BY a.created_at DESC,a.id DESC LIMIT 40)
            ) activity ORDER BY at DESC,kind,record_id DESC LIMIT 40
        """),
            {
                "owner": owner,
                "native_items": json.dumps(
                    {a.item_type: a.name for a in NATIVE_ACTIONS if a.item_type}
                ),
            },
        )
        return result.mappings().all()

    async def formal_method_references(self, owner):
        result = await self.session.execute(
            text("""
            SELECT method_id::text,version,digest FROM workbench_formal_methods
            WHERE owner_actor_id=:owner ORDER BY created_at DESC,method_id,version DESC
        """),
            {"owner": owner},
        )
        return [dict(row) for row in result.mappings()]

    async def list_formal_methods(self, owner, references=None):
        sql = "SELECT m.* FROM workbench_formal_methods m WHERE m.owner_actor_id=:owner"
        params = {"owner": owner}
        if references is not None:
            sql += """ AND EXISTS (SELECT 1 FROM jsonb_to_recordset(cast(:refs AS jsonb))
                AS r(method_id uuid,version int,digest text)
                WHERE (r.method_id,r.version,r.digest)=(m.method_id,m.version,m.digest))"""
            params["refs"] = json.dumps([r.model_dump(mode="json") for r in references])
        result = await self.session.execute(
            text(sql + " ORDER BY m.created_at DESC,m.method_id,m.version DESC"), params
        )
        return [dict(row) for row in result.mappings()]

    async def list_formal_session_calls(self, owner, session_id, limit=50):
        from app.infrastructure.workbench.capability_repository import row

        if not 1 <= limit <= 50:
            raise ValueError("Formal call history limit must be between 1 and 50")
        rows = (
            (
                await self.session.execute(
                    text("""SELECT * FROM workbench_calls
              WHERE owner_actor_id=:owner AND session_id=:session_id
                AND kind='method' AND origin='method' AND call_key LIKE 'formal-session:%'
                AND parent_call_id IS NULL
              ORDER BY created_at DESC,id DESC LIMIT :limit"""),
                    {"owner": owner, "session_id": session_id, "limit": limit},
                )
            )
            .mappings()
            .all()
        )
        return [row(value) for value in rows]

    async def lock_formal_execution(self, run_id: str):
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 45))"),
            {"key": run_id},
        )

    async def seal_formal_execution(
        self, owner: str, run_id: str, result: dict, evidence: dict
    ):
        """Caller holds the admission/run locks; evidence survives task cleanup."""
        prior = await self.formal_validation(owner, run_id, lock=True)
        if prior is None:
            raise IdempotencyConflict("Formal validation disappeared")
        if prior.get("execution_evidence") is not None:
            if prior["execution_evidence"] != evidence or prior["result"] != result:
                raise IdempotencyConflict("Formal execution evidence is immutable")
            return
        await self.session.execute(
            text("""UPDATE workbench_formal_validations
              SET result=cast(:result as jsonb), execution_evidence=cast(:evidence as jsonb), updated_at=now()
              WHERE run_id=:run_id AND owner_actor_id=:owner"""),
            {
                "owner": owner,
                "run_id": run_id,
                "result": json.dumps(result),
                "evidence": json.dumps(evidence),
            },
        )

    async def formal_validation(self, owner: str, run_id: str, *, lock=False):
        row = (
            (
                await self.session.execute(
                    text(
                        "SELECT * FROM workbench_formal_validations "
                        "WHERE owner_actor_id=:o AND run_id=:id"
                        + (" FOR UPDATE" if lock else "")
                    ),
                    {"o": owner, "id": run_id},
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    async def insert_formal_validation(self, record: dict[str, Any]):
        row = (
            (
                await self.session.execute(
                    text("""
            INSERT INTO workbench_formal_validations
              (run_id,owner_actor_id,caller,request_id,request_digest,request,result)
            VALUES (:run_id,:owner_actor_id,:caller,:request_id,:request_digest,
              cast(:request as jsonb),cast(:result as jsonb))
            ON CONFLICT DO NOTHING RETURNING *
        """),
                    {
                        **record,
                        "request": json.dumps(record["request"]),
                        "result": json.dumps(record["result"]),
                    },
                )
            )
            .mappings()
            .first()
        )
        if row:
            return dict(row)
        existing = (
            (
                await self.session.execute(
                    text("""
            SELECT * FROM workbench_formal_validations WHERE owner_actor_id=:o
              AND run_id=:id
        """),
                    {
                        "o": record["owner_actor_id"],
                        "id": record["run_id"],
                        "request_id": record["request_id"],
                    },
                )
            )
            .mappings()
            .all()
        )
        if (
            len(existing) != 1
            or str(existing[0]["run_id"]) != record["run_id"]
            or existing[0]["caller"] != record["caller"]
            or existing[0]["request_digest"] != record["request_digest"]
        ):
            raise IdempotencyConflict("Validation identity was used with other content")
        return dict(existing[0])

    async def formal_method(self, owner: str, method_id: str, version: int):
        row = (
            (
                await self.session.execute(
                    text("""
            SELECT * FROM workbench_formal_methods WHERE owner_actor_id=:o
              AND method_id=:id AND version=:v
        """),
                    {"o": owner, "id": method_id, "v": version},
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    async def insert_formal_method(self, record: dict[str, Any]):
        row = (
            (
                await self.session.execute(
                    text("""
            INSERT INTO workbench_formal_methods
              (method_id,version,owner_actor_id,caller,digest,document,run_id)
            VALUES (:method_id,:version,:owner_actor_id,:caller,:digest,
              cast(:document as jsonb),:run_id)
            ON CONFLICT DO NOTHING RETURNING *
        """),
                    {**record, "document": json.dumps(record["document"])},
                )
            )
            .mappings()
            .first()
        )
        if row:
            return dict(row)
        prior = await self.formal_method(
            record["owner_actor_id"], record["method_id"], record["version"]
        )
        if (
            prior is None
            or prior["caller"] != record["caller"]
            or prior["document"] != record["document"]
        ):
            raise IdempotencyConflict("Frozen method identity is immutable")
        return prior
