"""Trusted source resolution, validation intake and immutable method admission.

Installation is not incubation. Intake records unmet checks rather than inventing
successful runtime evidence. Only persisted, complete runtime receipts admit a
KnowLab frozen version; the service caller cannot supply its own pass result.
"""

from typing import Any

from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.errors import Forbidden, NotFound
from app.domain.workbench.formal_method_admission_generated import (
    CONTRACT,
    GOVERNANCE_CHECKS,
    FormalMethodSource,
    FormalMethodValidationRequest,
    FormalMethodValidationResult,
    FrozenFormalMethod,
)
from app.domain.workbench.formal_method_compiler_generated import (
    FrozenMethod,
    compile_method,
)
from app.domain.workbench.formal_method_generated import Orchestration
from app.domain.workbench.tool_operations import OperationCatalog
from app.domain.workbench.tool_registry import ToolRegistration


class FormalMethods:
    def __init__(self, repo, *, operations=()):
        self.repo = repo
        self.operations = OperationCatalog(tuple(operations))

    async def _owner(self, subject: str) -> str:
        actor = await self.repo.actor_for_subject(subject)
        if actor is None:
            raise NotFound("The author has not connected a workbench account")
        return actor

    def _sources(self, asset: dict[str, Any]) -> list[FormalMethodSource]:
        if asset["status"] != "active":
            return []
        detail = asset["detail"]
        common = {
            "asset_id": asset["id"],
            "asset_revision": asset["revision"],
            # A missing license remains a missing check. This label is never
            # accepted as licensing evidence or a redistribution grant.
            "license_basis": detail.get("execution_license_basis") or "pending",
        }
        if asset["kind"] == "connection":
            result = []
            for tool in detail.get("tools", []):
                registered = ToolRegistration.discover("mcp", detail["url"], tool)
                try:
                    policy = self.operations.require(registered)
                except CapabilityError:
                    continue
                result.append(
                    FormalMethodSource(
                        **common,
                        kind="mcp",
                        source=detail["url"],
                        version=policy.version,
                        digest="sha256:" + registered.descriptor_digest,
                        entry=tool["name"],
                    )
                )
            return result
        if asset["kind"] != "skill":
            return []
        fingerprint = detail["digest"]
        if not fingerprint.startswith("sha256:"):
            fingerprint = "sha256:" + fingerprint
        source = "skill:" + str(asset["id"])
        entries = [
            FormalMethodSource(
                **common,
                kind="skill_instruction",
                source=source,
                version=detail["version"],
                digest=fingerprint,
                entry="SKILL.md",
            )
        ]
        for file in detail.get("files", []):
            if file["path"].endswith((".py", ".sh", ".js", ".mjs")):
                entries.append(
                    FormalMethodSource(
                        **common,
                        kind="skill_script",
                        source=source,
                        version=detail["version"],
                        digest=fingerprint,
                        entry=file["path"],
                    )
                )
        return entries

    async def sources(self, subject: str) -> dict:
        async with self.repo.transaction():
            owner = await self._owner(subject)
            assets = await self.repo.capability_assets(owner)
            return {
                "items": [
                    s.model_dump(mode="json") for a in assets for s in self._sources(a)
                ]
            }

    async def _source(self, owner: str, source: FormalMethodSource | None) -> bool:
        if source is None:
            return True
        if source.asset_id is None:
            # Platform implementations need their own reviewed source records;
            # being built in does not exempt them from incubation.
            raise CapabilityError(
                "Platform implementation has no incubation source record"
            )
        asset = await self.repo.capability_asset(owner, str(source.asset_id), lock=True)
        if source not in self._sources(asset):
            raise CapabilityError(
                "Installed source changed, is unavailable or belongs to another author"
            )
        return source.license_basis != "pending"

    async def _compile(self, owner: str, method_id, definition):
        resolved: dict = {}

        async def collect(current, path=()):
            if len(path) >= 16 or len(resolved) > 1000:
                raise CapabilityError("Method dependency expansion limit")
            if not isinstance(current.procedure, Orchestration):
                return
            for step in current.procedure.steps:
                for ref in step.candidates:
                    if ref.method_id in path or ref.method_id == method_id:
                        raise CapabilityError("Cyclic formal method dependency")
                    key = (ref.method_id, ref.version)
                    if key in resolved:
                        if resolved[key].reference != ref:
                            raise CapabilityError(
                                "One frozen version has conflicting digests"
                            )
                        continue
                    row = await self.repo.formal_method(
                        owner, str(ref.method_id), ref.version
                    )
                    if row is None:
                        raise CapabilityError(
                            "Dependency has no admitted version for this author"
                        )
                    frozen = FrozenFormalMethod.model_validate(row["document"])
                    if frozen.reference != ref:
                        raise CapabilityError("Dependency changed")
                    resolved[key] = FrozenMethod(ref, frozen.draft.definition)
                    await collect(frozen.draft.definition, (*path, ref.method_id))

        await collect(definition)
        try:
            return compile_method(method_id, definition, resolved)
        except ValueError as exc:
            raise CapabilityError(str(exc)) from exc

    async def validate(self, request: FormalMethodValidationRequest, *, caller: str):
        async with self.repo.transaction():
            owner = await self._owner(request.owner_subject)
            prior = await self.repo.formal_validation(
                owner, str(request.run_id), lock=True
            )
            document = request.model_dump(mode="json", by_alias=True)
            fingerprint = digest(document)
            if prior is not None:
                if prior["caller"] != caller or prior["request_digest"] != fingerprint:
                    raise CapabilityError(
                        "Validation run identity belongs to another request"
                    )
                return FormalMethodValidationResult.model_validate(
                    prior["result"]
                ).model_dump(mode="json")
            licensed = await self._source(owner, request.draft.source)
            compilation = await self._compile(
                owner, request.method_id, request.draft.definition
            )
            if set(request.dependencies) != set(compilation.dependencies):
                raise CapabilityError("Validation must include every frozen candidate")
            checks = {"definition", "dependencies"}
            if licensed:
                checks.add("source")
            result = FormalMethodValidationResult.model_validate(
                {
                    "contract": CONTRACT,
                    "run_id": request.run_id,
                    "status": "pending",
                    "receipt": None,
                    "missing_checks": sorted(GOVERNANCE_CHECKS - checks),
                    "details": [
                        "Definition and frozen dependencies checked; runtime governance and outcome evidence is still required."
                    ]
                    + (
                        []
                        if licensed
                        else ["Source execution rights have not been verified."]
                    ),
                }
            )
            row = await self.repo.insert_formal_validation(
                {
                    "run_id": str(request.run_id),
                    "owner_actor_id": owner,
                    "caller": caller,
                    "request_id": str(request.request_id),
                    "request_digest": fingerprint,
                    "request": document,
                    "result": result.model_dump(mode="json"),
                }
            )
            return row["result"]

    async def validation(self, subject: str, run_id: str, *, caller: str):
        async with self.repo.transaction():
            owner = await self._owner(subject)
            row = await self.repo.formal_validation(owner, run_id)
            if row is None or row["caller"] != caller:
                raise NotFound("Validation run not found")
            return FormalMethodValidationResult.model_validate(
                row["result"]
            ).model_dump(mode="json")

    async def register(self, frozen: FrozenFormalMethod, *, caller: str):
        async with self.repo.transaction():
            owner = await self._owner(frozen.owner_subject)
            existing = await self.repo.formal_method(
                owner, str(frozen.reference.method_id), frozen.reference.version
            )
            if existing is not None:
                if existing["caller"] != caller or existing[
                    "document"
                ] != frozen.model_dump(mode="json", by_alias=True):
                    raise Forbidden("Frozen method identity is immutable")
                return {
                    "reference": frozen.reference.model_dump(mode="json"),
                    "owner_subject": frozen.owner_subject,
                    "status": "registered",
                }
            run = await self.repo.formal_validation(
                owner, str(frozen.validation.run_id), lock=True
            )
            if run is None or run["caller"] != caller:
                raise NotFound("Validation run not found")
            request = FormalMethodValidationRequest.model_validate(run["request"])
            result = FormalMethodValidationResult.model_validate(run["result"])
            if (
                result.status != "passed"
                or result.receipt != frozen.validation
                or request.draft != frozen.draft
                or request.method_id != frozen.reference.method_id
                or request.owner_subject != frozen.owner_subject
                or request.request_id != frozen.validation.request_id
                or request.workdraft_id != frozen.validation.workdraft_id
                or request.revision != frozen.validation.revision
                or request.run_id != frozen.validation.run_id
                or frozen.reference.digest != request.draft.definition.digest()
            ):
                raise Forbidden(
                    "Frozen method does not match a completed trusted validation"
                )
            if not await self._source(owner, frozen.draft.source):
                raise Forbidden("Source execution rights have not been verified")
            compilation = await self._compile(
                owner, frozen.reference.method_id, frozen.draft.definition
            )
            if set(compilation.dependencies) != set(frozen.validation.dependencies):
                raise Forbidden("Frozen dependencies differ from validation")
            if frozen.visibility == "public":
                # Publication qualification belongs to KnowMart; this path has no
                # trusted publication receipt and must not invent public rights.
                raise Forbidden(
                    "Public method registration requires publication qualification"
                )
            await self.repo.insert_formal_method(
                {
                    "method_id": str(frozen.reference.method_id),
                    "version": frozen.reference.version,
                    "owner_actor_id": owner,
                    "caller": caller,
                    "digest": frozen.reference.digest,
                    "document": frozen.model_dump(mode="json", by_alias=True),
                    "run_id": str(frozen.validation.run_id),
                }
            )
            return {
                "reference": frozen.reference.model_dump(mode="json"),
                "owner_subject": frozen.owner_subject,
                "status": "registered",
            }

    async def registration(
        self, subject: str, method_id: str, version: int, *, caller: str
    ):
        async with self.repo.transaction():
            owner = await self._owner(subject)
            row = await self.repo.formal_method(owner, method_id, version)
            if row is None or row["caller"] != caller:
                raise NotFound("Formal method registration not found")
            frozen = FrozenFormalMethod.model_validate(row["document"])
            return {
                "reference": frozen.reference.model_dump(mode="json"),
                "owner_subject": subject,
                "status": "registered",
            }
