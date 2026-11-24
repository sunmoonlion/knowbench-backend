"""Resolve scene candidates from KnowBench registrations and intersect current grants."""

from uuid import UUID

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.formal_method_admission_generated import (
    FormalMethodSource,
    FrozenFormalMethod,
)
from app.domain.workbench.formal_method_generated import (
    FormalMethodDefinition,
    MethodRef,
)
from app.domain.workbench.method_search import positive_fields, search_terms


class SceneRegistry:
    def __init__(self, methods, repo):
        self.methods = methods
        self.repo = repo

    @property
    def market(self):
        return self.methods.market

    @staticmethod
    def direct_source(row):
        return FormalMethodSource(
            asset_id=None,
            asset_revision=None,
            kind="registered_" + row["kind"],
            source=str(row["source_id"]),
            version=str(row["version"]),
            digest=row["digest"],
            entry=row["name"],
            license_basis="owner",
        )

    async def local_tool_source(self, owner, subject, impl):
        """Bind the frozen script or instruction to this consumer's exact licensed source."""
        if impl.kind not in (
            "registered_script",
            "registered_instruction",
            "skill_script",
            "skill_instruction",
        ):
            raise CapabilityError("Expected a script or instruction source")
        if impl.kind.startswith("registered_"):
            source = FormalMethodSource(
                **impl.model_dump(
                    include={"kind", "source", "entry", "version", "digest"}
                ),
                asset_id=None,
                asset_revision=None,
                license_basis="owner",
            )
        else:
            asset = await self.repo.capability_asset(
                owner, str(UUID(impl.source.removeprefix("skill:"))), lock=True
            )
            if asset["kind"] != "skill":
                raise CapabilityError("This installed Skill is unavailable")
            source = next(
                (
                    s
                    for s in self.methods._sources(
                        asset, subject, now=self.methods.now()
                    )
                    if all(
                        getattr(impl, k) == getattr(s, k)
                        for k in ("kind", "source", "version", "entry", "digest")
                    )
                ),
                None,
            )
            if source is None:
                raise CapabilityError("The selected Skill entry has changed")
        if source.license_basis == "pending" or not await self.methods._source(
            owner, subject, source
        ):
            raise CapabilityError("Missing execution license for the selected source")
        return source

    async def check_direct_source(self, owner, source):
        row = await self.repo.direct_revision(
            owner, source.source, int(source.version), source.digest
        )
        if self.direct_source(row) != source:
            raise CapabilityError(
                "Direct implementation differs from its registered revision"
            )

        from app.domain.workbench.registry_sources import SourceRevision

        return SourceRevision.from_row(row)

    @staticmethod
    def registered_asset(source, revision):
        return {
            "kind": source.kind,
            "id": str(revision.source_id),
            "revision": revision.version,
            "name": revision.name,
            "status": "active",
            "detail": {},
        }

    async def execution_asset(self, owner, source):
        if source.kind == "service_function":
            from app.domain.workbench.source_licenses import SourceIdentity

            pin = SourceIdentity(
                kind=source.kind,
                source=source.source,
                entry=source.entry,
                version=source.version,
                digest=source.digest,
            )
            rows = await self.repo.incubation_function_revisions([pin])
            if len(rows) != 1 or "step" not in rows[0].contract.scenes:
                raise CapabilityError(
                    "Function has no matching executable registration"
                )
            return {
                "kind": source.kind,
                "id": source.digest,
                "revision": int(source.version),
                "name": source.source + "/" + source.entry,
                "status": "active",
                "detail": {},
                "function": rows[0],
            }
        if source.is_registered:
            revision = await self.check_direct_source(owner, source)
            return self.registered_asset(source, revision)
        return await self.repo.capability_asset(owner, str(source.asset_id), lock=True)

    async def sources(self, subject):
        async with self.repo.transaction():
            owner = await self.methods._owner(subject)
            assets = await self.repo.capability_assets(owner)
            now = self.methods.now()
            candidates = [
                src
                for asset in assets
                for src in self.methods._sources(asset, subject, now=now)
            ]
            candidates.extend(
                self.direct_source(row)
                for row in await self.repo.direct_registrations(owner)
            )
            licenses = self.methods.source_licenses.incubation_functions(
                subject, now=now
            )
            if licenses:
                bases = {record.source.key(): record.basis for record in licenses}
                for revision in await self.repo.incubation_function_revisions(
                    [r.source for r in licenses]
                ):
                    key = (
                        "service_function",
                        revision.provider,
                        str(revision.version),
                        revision.digest,
                        revision.name,
                    )
                    candidates.append(
                        FormalMethodSource(
                            asset_id=None,
                            asset_revision=None,
                            kind="service_function",
                            source=revision.provider,
                            version=str(revision.version),
                            digest=revision.digest,
                            entry=revision.name,
                            license_basis=bases[key],
                        )
                    )
            return {"items": [src.model_dump(mode="json") for src in candidates]}

    async def category_matches(self, subject, terms):
        from app.domain.workbench.errors import KnowbotUnavailable

        # Market facets are optional discovery metadata, never a use decision.
        lookup = getattr(self.market, "category_matches", None)
        if not terms or lookup is None:
            return []
        try:
            return await lookup(subject, terms)
        except KnowbotUnavailable:
            return []

    async def author_versions(
        self,
        subject,
        limit,
        offset,
        *,
        all_versions=False,
        declaration=None,
        goal=None,
        references=None,
    ):
        terms = search_terms(
            goal or "", *(positive_fields(declaration) if declaration else ())
        )
        topics = await self.category_matches(subject, terms) if goal else []
        owner = await self.repo.actor_for_subject(subject)
        if owner is None:
            return []
        return await self.repo.registry_method_candidates(
            owner,
            limit,
            offset,
            latest=not all_versions,
            host_only=bool(goal),
            eligible=[r.model_dump(mode="json") for r in references]
            if references is not None
            else None,
            terms=terms,
            topic_refs=topics,
        )

    async def planning_versions(
        self, owner, limit, offset, *, query=None, locked=False
    ):
        terms = (
            search_terms(query.question, query.goal or "")
            if query and query.question
            else []
        )
        experts_only = bool(query and query.stage == "expert")
        async with self.repo.transaction(independent=not locked):
            subject = await self.repo.subject_of_actor(owner)
            if not subject or self.market is None:
                raise CapabilityError("Formal method use qualification is unavailable")

        if not terms:
            # Browsing retains a bounded market window and its continuation.
            window = (100 // limit) * limit
            start = offset // window * window
            refs, more = await self.market.planning_window(subject, start, window)
            async with self.repo.transaction():
                rows = await self.repo.registry_method_candidates(
                    owner,
                    limit + 1,
                    offset - start,
                    public=True,
                    experts_only=experts_only,
                    eligible=[r.model_dump(mode="json") for r in refs],
                )
            cursor = (
                offset + limit
                if len(rows) > limit
                else start + window
                if more
                else None
            )
            return {"items": rows[:limit], "next": cursor}

        topics = [] if experts_only else await self.category_matches(subject, terms)
        refs, more = await self.market.planning_window(subject, 0, 100)
        if topics:
            # Blend categories only within a qualified set so other subjects'
            # versions cannot affect how many slots each category receives.
            while more:
                page, more = await self.market.planning_window(subject, len(refs), 100)
                refs.extend(page)
        if more:
            # Exact-grant checks on indexed hits usually avoid shipping the full
            # grant set. Only authorized hits count toward the caller's page.
            hits = []
            index_offset = 0
            while len(hits) < offset + limit + 1:
                async with self.repo.transaction():
                    batch = await self.repo.registry_method_candidates(
                        owner,
                        100,
                        index_offset,
                        public=True,
                        experts_only=experts_only,
                        terms=terms,
                        topic_refs=topics,
                        matching=True,
                    )
                if not batch:
                    break
                pins = [MethodRef.model_validate(self.reference(r)) for r in batch]
                allowed = await self.market.planning_formal_refs(
                    subject, [r.model_dump(mode="json") for r in pins]
                )
                hits.extend(
                    row for row, ref in zip(batch, pins, strict=True) if ref in allowed
                )
                if len(batch) < 100:
                    break
                index_offset += len(batch)
            if len(hits) > offset + limit:
                return {"items": hits[offset : offset + limit], "next": offset + limit}
            # Fill any remaining places from authorized non-hits. Advance the
            # market cursor only until this page is full, even if the directory
            # contains thousands of unrelated granted versions.
            result = hits[offset : offset + limit + 1]
            skip = max(offset - len(hits), 0)
            cursor = 0
            while True:
                async with self.repo.transaction():
                    extras = await self.repo.registry_method_candidates(
                        owner,
                        len(refs),
                        0,
                        terms=terms,
                        topic_refs=topics,
                        matching=False,
                        public=True,
                        experts_only=experts_only,
                        eligible=[r.model_dump(mode="json") for r in refs],
                    )
                result.extend(extras[skip : skip + limit + 1 - len(result)])
                skip = max(0, skip - len(extras))
                if len(result) > limit or not more:
                    return {
                        "items": result[:limit],
                        "next": offset + limit if len(result) > limit else None,
                    }
                cursor += len(refs)
                refs, more = await self.market.planning_window(subject, cursor, 100)
        async with self.repo.transaction():
            rows = await self.repo.registry_method_candidates(
                owner,
                limit + 1,
                offset,
                public=True,
                experts_only=experts_only,
                terms=terms,
                topic_refs=topics,
                eligible=[r.model_dump(mode="json") for r in refs],
            )
        return {
            "items": rows[:limit],
            "next": offset + limit if len(rows) > limit else None,
        }

    async def planning_definitions(
        self, owner, references, *, experts_only=False, locked=False
    ):
        """Exact consumer pins for one graph frontier; resolve market rights outside SQL."""
        requested = set(references)
        if not requested:
            return {}
        async with self.repo.transaction(independent=not locked):
            subject = await self.repo.subject_of_actor(owner)
        if not subject or self.market is None:
            raise CapabilityError("Formal method use qualification is unavailable")
        allowed = requested.intersection(
            await self.market.planning_formal_refs(
                subject, [r.model_dump(mode="json") for r in requested]
            )
        )
        return await self.local_definitions(owner, allowed, experts_only=experts_only)

    async def local_definitions(self, owner, allowed, *, experts_only=False):
        """Caller supplies exact pins that have already passed consumer authorization."""
        if not allowed:
            return {}
        async with self.repo.transaction():
            rows = await self.repo.registry_method_candidates(
                owner,
                len(allowed),
                0,
                public=True,
                experts_only=experts_only,
                definitions=True,
                identities_only=True,
                eligible=[r.model_dump(mode="json") for r in allowed],
            )
        result = {}
        for row in rows:
            ref = MethodRef.model_validate(self.reference(row))
            try:
                definition = FormalMethodDefinition.model_validate(row["definition"])
            except ValueError as exc:
                raise CapabilityError("Invalid registered method definition") from exc
            if ref not in allowed or definition.digest() != ref.digest:
                raise CapabilityError(
                    "Registered definition differs from its qualified version"
                )
            result[ref] = definition
        return result

    async def planning_definition(self, owner, reference, *, experts_only=False):
        from app.domain.workbench.errors import NotFound

        result = await self.planning_definitions(
            owner, [reference], experts_only=experts_only
        )
        if reference not in result:
            raise NotFound("No available method with this reference")
        return result[reference]

    async def planning_declaration(self, owner, reference, *, experts_only=False):
        definition = await self.planning_definition(
            owner, reference, experts_only=experts_only
        )
        return self.public_declaration(reference, definition)

    @staticmethod
    def public_declaration(reference, definition):
        outputs = {}
        for rule in definition.how_to_validate:
            outputs.setdefault(rule.promise_id, {}).update(dict.fromkeys(rule.outputs))
        return {
            "reference": reference.model_dump(mode="json"),
            "promise_outputs": {
                promise: list(names) for promise, names in outputs.items()
            },
            "declaration": definition.capability_declaration.model_dump(mode="json"),
        }

    @staticmethod
    def reference(row):
        return {
            "method_id": str(row["method_id"]),
            "version": row["latest_version"],
            "digest": row["digest"],
        }

    async def shared_grant(self, subject, reference):
        if not subject or self.market is None:
            return None
        page = await self.market.incubation_tools(subject, reference=reference)
        if page.subject == subject and len(page.items) == 1:
            grant = page.items[0]
            if grant.reference == reference:
                return grant
        return None

    async def _read_grants(self, subject, refs):
        grants = {}
        for ref in refs:
            grant = await self.shared_grant(subject, ref)
            if grant is not None:
                grants[subject, ref] = grant
        return grants

    async def request_grants(self, subject, refs):
        """Resolve only non-owned pins of this request before the caller takes locks."""
        if not refs or self.market is None:
            return {}
        from app.domain.workbench.formal_method_generated import Orchestration

        # The admission contract pins the immediate children. An owned child
        # expert may depend on other authors' tools several levels below it.
        missing = []
        seen = set()
        pending = set(refs)
        depth = 0
        async with self.repo.transaction(independent=True):
            owner = await self.methods._owner(subject)
            while pending:
                depth += 1
                if depth > 16:
                    raise CapabilityError("Formal dependency depth limit")
                seen.update(pending)
                if len(seen) > 1000:
                    raise CapabilityError("Formal dependency closure limit")
                rows = await self.repo.registry_method_candidates(
                    owner,
                    len(pending),
                    0,
                    eligible=[ref.model_dump(mode="json") for ref in pending],
                    definitions=True,
                    identities_only=True,
                )
                children = set()
                for row in rows:
                    pending.remove(MethodRef.model_validate(self.reference(row)))
                    definition = FormalMethodDefinition.model_validate(
                        row["definition"]
                    )
                    if isinstance(definition.procedure, Orchestration):
                        for step in definition.procedure.steps:
                            children.update(step.candidates)
                missing.extend(pending)
                pending = children - seen
        return await self._read_grants(subject, missing)

    async def task_grants(self, task_id, *, owner=None, replay_attempt=None):
        """Use only shared pins already bound to this exact execution snapshot."""
        from app.domain.workbench.formal_execution import snapshot_for_task

        if self.market is None:
            return {}
        async with self.repo.transaction(independent=True):
            task = await self.repo.get_task(task_id, owner_actor_id=owner)
            if task.get("pack_source") == "formal-expert":
                from app.domain.workbench.ordinary_expert import ConsumerPlan

                plan = ConsumerPlan.model_validate(task["pack_snapshot"])
                refs = [d.reference for d in plan.dependencies]
                if not plan.temporary:
                    refs.append(plan.reference)
                subject = await self.repo.subject_of_actor(str(task["owner_actor_id"]))
            else:
                refs = []
            if not refs and not (task.get("pack_snapshot") or {}).get("bindings"):
                return {}
            if not refs:
                if replay_attempt:
                    attempt = await self.repo.get_attempt(replay_attempt)
                    if str(attempt["task_id"]) == str(task_id) and (
                        attempt.get("refs") or {}
                    ).get("capability_call_id"):
                        return {}
                snapshot = snapshot_for_task(task)
                subject = await self.repo.subject_of_actor(str(task["owner_actor_id"]))
        if refs:
            if not subject:
                return {}
            eligible = await self.market.planning_formal_refs(
                subject, [r.model_dump(mode="json") for r in refs]
            )
            return {"ordinary_refs": eligible}
        return await self._read_grants(
            subject, (binding.reference for binding in snapshot.bindings)
        )

    async def dependency(self, owner, reference, *, shared=True, grants=None):
        row = await self.repo.formal_method(
            owner, str(reference.method_id), reference.version
        )
        grant = None
        if row is None and shared and grants:
            subject = await self.repo.subject_of_actor(owner)
            grant = grants.get((subject, reference))
            if grant:
                publisher = await self.repo.actor_for_subject(grant.owner_subject)
                if publisher:
                    row = await self.repo.formal_method(
                        publisher, str(reference.method_id), reference.version
                    )
        if not row or row["caller"] != "knowlab" or row["digest"] != reference.digest:
            raise CapabilityError("Dependency has no accessible registration")
        frozen = FrozenFormalMethod.model_validate(row["document"])
        if frozen.reference != reference or (
            grant
            and (
                frozen.owner_subject != grant.owner_subject
                or frozen.visibility != "public"
                or frozen.draft.source is None
                or frozen.draft.source.kind not in ("mcp", "a2a", "skill_script")
                or "host" not in frozen.draft.allowed_callers
            )
        ):
            raise CapabilityError("Dependency identity or host qualification changed")
        self.methods.require_supported_graph(frozen.draft)
        return frozen, grant

    async def shared_tools(
        self,
        subject,
        *,
        reference=None,
        after=None,
        limit=30,
        declaration=None,
        goal=None,
    ):
        from app.application.workbench.formal_method_catalog import FormalMethodCatalog
        from app.domain.workbench.errors import WorkbenchError

        async with self.repo.transaction(independent=True):
            owner = await self.repo.actor_for_subject(subject)
        if owner is None:
            return {"items": [], "next": None}
        if self.market is None:
            raise CapabilityError("KnowMart incubation permissions are unavailable")
        if goal and (reference or after):
            raise CapabilityError(
                "A planning search starts from its own ranked directory"
            )
        terms = search_terms(
            goal or "", *(positive_fields(declaration) if declaration else ())
        )
        search = {"topic_terms": terms} if goal else {}
        installed_digests = None
        if goal:
            async with self.repo.transaction():
                installed_digests = {
                    source.digest
                    for asset in await self.repo.capability_assets(owner)
                    for source in self.methods._sources(
                        asset, subject, now=self.methods.now()
                    )
                    if source.kind in ("mcp", "a2a", "skill_script")
                    and source.license_basis != "pending"
                }
            if not installed_digests:
                return {"items": [], "next": None, "partial": False}
        page = await self.market.incubation_tools(
            subject,
            reference=reference,
            after=after,
            limit=100 if goal else limit,
            **search,
        )
        grants = list(page.items)
        if goal:
            # This is a planning snapshot. Browse and exact reads continue using
            # the market cursor. Collect grants only, never all method documents.
            while page.next:
                cursor = page.next
                page = await self.market.incubation_tools(
                    subject, after=str(cursor), limit=100, **search
                )
                if page.next and page.next <= cursor:
                    raise CapabilityError("Incubation directory cursor did not advance")
                grants.extend(page.items)
            by_ref = {grant.reference: grant for grant in grants}
            async with self.repo.transaction():
                rows = await self.repo.registry_method_candidates(
                    owner,
                    len(by_ref),
                    0,
                    terms=terms,
                    public=True,
                    identities_only=True,
                    installed_digests=installed_digests,
                    eligible=[r.model_dump(mode="json") for r in by_ref],
                    topic_refs=[
                        r.model_dump(mode="json")
                        for r, g in by_ref.items()
                        if g.topic_hit
                    ],
                )
            grants = [by_ref[MethodRef.model_validate(self.reference(r))] for r in rows]
        ranked = []
        catalog = FormalMethodCatalog(self.repo, self.methods, self.methods.operations)
        for grant in grants:
            async with self.repo.transaction():
                try:
                    frozen = await catalog.shared_tool(grant)
                    options, selected = await self.methods.binding_options(
                        owner, frozen
                    )
                except (WorkbenchError, ValueError):
                    continue
                if not options or (goal and not selected):
                    continue
                item = {
                    "reference": frozen.reference.model_dump(mode="json"),
                    "scope": grant.scope,
                    "title": frozen.draft.definition.capability_declaration.title,
                }
                if reference:
                    item["definition"] = frozen.draft.definition.model_dump(
                        mode="json", by_alias=True
                    )
                if reference or goal or selected:
                    item["installations"] = [
                        {
                            "asset_id": str(key),
                            "revision": value.asset_revision,
                            "name": name,
                            "account": account,
                        }
                        for key, (value, name, account) in options.items()
                    ]
                    item["selected"] = str(selected) if selected else None
                score = 0
                if terms and not goal:
                    description = " ".join(
                        positive_fields(frozen.draft.definition.capability_declaration)
                    ).lower()
                    score = sum(term in description for term in terms)
                ranked.append((score, item))
                if goal and len(ranked) > limit:
                    break
        # Python sorting is stable when the global SQL ranking already applies.
        ranked.sort(key=lambda entry: entry[0], reverse=True)
        return {
            "items": [item for _, item in ranked[:limit]],
            **({"partial": len(ranked) > limit} if goal else {}),
            "next": str(page.next) if page.next else None,
        }
