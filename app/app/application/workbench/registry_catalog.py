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

    async def author_versions(
        self,
        subject,
        limit,
        offset,
        *,
        all_versions=False,
        declaration=None,
        references=None,
    ):
        owner = await self.repo.actor_for_subject(subject)
        if owner is None:
            return []
        return await self.repo.registry_method_candidates(
            owner,
            limit,
            offset,
            latest=not all_versions,
            eligible=[r.model_dump(mode="json") for r in references]
            if references is not None
            else None,
            terms=search_terms(*positive_fields(declaration)) if declaration else [],
        )

    async def planning_versions(self, owner, limit, offset, *, query=None):
        terms = search_terms(query.question) if query and query.question else []
        experts_only = bool(query and query.stage == "expert")
        # Bound both the authorization request and the in-memory ranking window.
        # KnowMart pages only this actor's usable pins. Ranking stays local.
        window = (100 // limit) * limit
        start, within = divmod(offset, window)
        start *= window
        async with self.repo.transaction(independent=True):
            subject = await self.repo.subject_of_actor(owner)
            if not subject or self.market is None:
                raise CapabilityError("Formal method use qualification is unavailable")
        refs, more = await self.market.planning_window(subject, start, window)
        eligible = [ref.model_dump(mode="json") for ref in refs]
        async with self.repo.transaction():
            visible = (
                await self.repo.registry_method_candidates(
                    owner,
                    limit + 1,
                    within,
                    public=True,
                    experts_only=experts_only,
                    terms=terms,
                    eligible=eligible,
                )
                if eligible
                else []
            )
        next_page = None
        if len(visible) > limit:
            next_page = offset + limit
        elif more:
            next_page = start + window
        return {"items": visible[:limit], "next": next_page}

    async def planning_definitions(self, owner, references, *, experts_only=False):
        """Exact consumer pins for one graph frontier; resolve market rights outside SQL."""
        requested = set(references)
        if not requested:
            return {}
        async with self.repo.transaction(independent=True):
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
                refs = [plan.reference, *(d.reference for d in plan.dependencies)]
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
                or frozen.draft.source.kind not in ("mcp", "skill_script")
                or "host" not in frozen.draft.allowed_callers
            )
        ):
            raise CapabilityError("Dependency identity or host qualification changed")
        self.methods.require_supported_graph(frozen.draft)
        return frozen, grant

    async def shared_tools(
        self, subject, *, reference=None, after=None, limit=30, declaration=None
    ):
        from app.application.workbench.formal_method_catalog import FormalMethodCatalog
        from app.domain.workbench.errors import WorkbenchError

        async with self.repo.transaction(independent=True):
            owner = await self.repo.actor_for_subject(subject)
        if owner is None:
            return {"items": [], "next": None}
        if self.market is None:
            raise CapabilityError("KnowMart incubation permissions are unavailable")
        page = await self.market.incubation_tools(
            subject, reference=reference, after=after, limit=limit
        )
        ranked = []
        terms = search_terms(*positive_fields(declaration)) if declaration else []
        catalog = FormalMethodCatalog(self.repo, self.methods, self.methods.operations)
        for grant in page.items:
            async with self.repo.transaction():
                try:
                    frozen = await catalog.shared_tool(grant)
                    options, selected = await self.methods.binding_options(
                        owner, frozen
                    )
                except (WorkbenchError, ValueError):
                    continue
                if not options:
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
                if terms:
                    description = " ".join(
                        positive_fields(frozen.draft.definition.capability_declaration)
                    ).lower()
                    score = sum(term in description for term in terms)
                ranked.append((score, item))
        # KnowMart owns the cursor. Rank only the qualified items on this page.
        ranked.sort(key=lambda entry: entry[0], reverse=True)
        return {
            "items": [item for _, item in ranked],
            "next": str(page.next) if page.next else None,
        }
