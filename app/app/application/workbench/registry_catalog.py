"""Resolve scene candidates from KnowBench registrations and intersect current grants."""

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
        self, subject, limit, offset, *, all_versions=False, declaration=None
    ):
        owner = await self.repo.actor_for_subject(subject)
        if owner is None:
            return []
        return await self.repo.registry_method_candidates(
            owner,
            limit,
            offset,
            latest=not all_versions,
            terms=search_terms(*positive_fields(declaration)) if declaration else [],
        )

    async def planning_versions(self, owner, limit, offset, *, query=None):
        subject = await self.repo.subject_of_actor(owner)
        if not subject or self.market is None:
            raise CapabilityError("Formal method use qualification is unavailable")
        terms = search_terms(query.question) if query and query.question else []
        experts_only = bool(query and query.stage == "expert")
        eligible = []
        scanned = 0
        while True:
            # KnowMart sees only pins in catalog order, independent of the user's wording.
            rows = await self.repo.registry_method_candidates(
                owner,
                100,
                scanned,
                public=True,
                experts_only=experts_only,
                identities_only=True,
            )
            if rows:
                refs = [self.reference(row) for row in rows]
                allowed = await self.market.planning_formal_refs(subject, refs)
                eligible.extend(
                    ref for ref in refs if MethodRef.model_validate(ref) in allowed
                )
            if len(rows) < 100 or (not terms and len(eligible) > offset + limit):
                break
            scanned += len(rows)
        if not eligible:
            return {"items": [], "next": None}
        visible = await self.repo.registry_method_candidates(
            owner,
            limit + 1,
            offset,
            public=True,
            experts_only=experts_only,
            terms=terms,
            eligible=eligible,
        )
        return {
            "items": visible[:limit],
            "next": offset + limit if len(visible) > limit else None,
        }

    async def planning_declaration(self, owner, reference):
        from app.domain.workbench.errors import NotFound

        subject = await self.repo.subject_of_actor(owner)
        if not subject or self.market is None:
            raise CapabilityError("Formal method use qualification is unavailable")
        if reference not in await self.market.planning_formal_refs(
            subject, [reference.model_dump(mode="json")]
        ):
            raise NotFound("No available method with this reference")
        rows = await self.repo.registry_method_candidates(
            owner, 1, 0, public=True, reference=reference
        )
        if not rows:
            raise NotFound("No available method with this reference")
        try:
            definition = FormalMethodDefinition.model_validate(rows[0]["definition"])
        except ValueError as exc:
            raise CapabilityError("Invalid registered method definition") from exc
        if definition.digest() != reference.digest:
            raise CapabilityError(
                "Registered declaration differs from the requested version"
            )
        outputs = {}
        for rule in definition.how_to_validate:
            outputs.setdefault(rule.promise_id, {}).update(dict.fromkeys(rule.outputs))
        return {
            "reference": self.reference(rows[0]),
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

    async def dependency(self, owner, reference, *, shared=True):
        row = await self.repo.formal_method(
            owner, str(reference.method_id), reference.version
        )
        grant = None
        if row is None and shared and self.market is not None:
            subject = await self.repo.subject_of_actor(owner)
            page = await self.market.incubation_tools(subject, reference=reference)
            if len(page.items) == 1:
                grant = page.items[0]
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
