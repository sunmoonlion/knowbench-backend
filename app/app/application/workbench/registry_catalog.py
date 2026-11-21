"""Resolve scene candidates from KnowBench registrations and intersect current grants."""

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.formal_method_admission_generated import (
    FormalMethodSource,
    FrozenFormalMethod,
)
from app.domain.workbench.formal_method_generated import MethodRef


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
            return {"items": [src.model_dump(mode="json") for src in candidates]}

    async def author_versions(self, subject, limit, offset, *, all_versions=False):
        owner = await self.repo.actor_for_subject(subject)
        if owner is None:
            return []
        return await self.repo.registry_method_candidates(
            owner, limit, offset, latest=not all_versions
        )

    async def planning_versions(self, owner, limit, offset):
        subject = await self.repo.subject_of_actor(owner)
        if not subject or self.market is None:
            raise CapabilityError("Formal method use qualification is unavailable")
        visible = []
        scanned = 0
        while len(visible) <= offset + limit:
            rows = await self.repo.registry_method_candidates(
                owner, 100, scanned, public=True
            )
            if rows:
                refs = [self.reference(r) for r in rows]
                allowed = await self.market.planning_formal_refs(subject, refs)
                visible.extend(
                    row
                    for row, ref in zip(rows, refs, strict=True)
                    if MethodRef.model_validate(ref) in allowed
                )
            if len(rows) < 100:
                break
            scanned += len(rows)
        return {
            "items": visible[offset : offset + limit],
            "next": offset + limit if len(visible) > offset + limit else None,
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

    async def shared_tools(self, subject, *, reference=None, after=None, limit=30):
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
        items = []
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
                items.append(item)
        return {"items": items, "next": str(page.next) if page.next else None}
