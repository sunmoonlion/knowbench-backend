"""Trusted source resolution, validation intake and immutable method admission.

Installation is not incubation. Intake records unmet checks rather than inventing
successful runtime evidence. Only persisted, complete runtime receipts admit a
KnowLab frozen version; the service caller cannot supply its own pass result.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.application.workbench.registry_catalog import SceneRegistry
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.errors import Forbidden, NotFound, RuntimeCapabilityGap
from app.domain.workbench.formal_execution import validate_values
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
from app.domain.workbench.formal_method_execution_generated import (
    bounded_json,
    fingerprint,
)
from app.domain.workbench.formal_method_generated import Orchestration
from app.domain.workbench.skill_package import (
    delivery_files,
    skill_manifest_digest,
    verified_instruction,
)
from app.domain.workbench.source_licenses import SourceIdentity, SourceLicenseCatalog
from app.domain.workbench.tool_operations import OperationCatalog
from app.domain.workbench.tool_registry import ToolRegistry, connection_source


class FormalMethods:
    def __init__(
        self,
        repo,
        *,
        operations=(),
        market=None,
        script_tariff=None,
        source_licenses=(),
        business_reviews=(),
        now: Callable[[], datetime] | None = None,
    ):
        self.repo = repo
        self.registry = SceneRegistry(self, repo)
        self.market = market
        self.operations = OperationCatalog(
            tuple(operations), script_tariff=script_tariff
        )
        self.source_licenses = SourceLicenseCatalog(tuple(source_licenses))
        self.business_reviews = tuple(business_reviews)
        self.now = now or (lambda: datetime.now(UTC))

    @staticmethod
    def require_supported_graph(draft):
        graph = draft.definition.procedure
        if isinstance(graph, Orchestration) and graph.collaboration:
            raise RuntimeCapabilityGap("Runner lacks isolated collaboration execution")

    def business_review(self, subject, method_id, draft, suite_digest):
        if draft.source is None:
            return None
        key = (
            subject,
            method_id,
            draft.definition.digest(),
            draft.source.fingerprint(),
            suite_digest,
        )
        matches = [
            r
            for r in self.business_reviews
            if (
                r.subject,
                r.method_id,
                r.definition_digest,
                r.source_digest,
                r.suite_digest,
            )
            == key
            and self.now() < r.expires_at
        ]
        return matches[0] if len(matches) == 1 else None

    async def _owner(self, subject: str) -> str:
        actor = await self.repo.actor_for_subject(subject)
        if actor is None:
            raise NotFound("The author has not connected a workbench account")
        return actor

    def _sources(
        self, asset: dict[str, Any], subject: str, *, now: datetime
    ) -> list[FormalMethodSource]:
        if asset["status"] != "active":
            return []
        detail = asset["detail"]
        common = {
            "asset_id": asset["id"],
            "asset_revision": asset["revision"],
            # Installation detail and author assertions never grant rights.
            "license_basis": "pending",
        }
        if asset["kind"] == "connection":
            if connection_source(asset) != "mcp":
                return []
            result = []
            catalog = ToolRegistry.snapshot(
                "mcp", detail["url"], detail.get("tools", [])
            )
            for registered in catalog.registrations():
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
                        entry=registered.name,
                    )
                )
            return self._licensed_sources(result, subject, now=now)
        if asset["kind"] != "skill":
            return []
        fingerprint = skill_manifest_digest(detail["files"])
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
            if file["path"].endswith((".py", ".sh", ".js", ".mjs", ".cjs")):
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
        return self._licensed_sources(entries, subject, now=now)

    def _licensed_sources(self, sources, subject: str, *, now: datetime):
        result = []
        for source in sources:
            identity = SourceIdentity.model_validate(
                source.model_dump(
                    include={"kind", "source", "version", "digest", "entry"}
                )
            )
            record = self.source_licenses.executable(subject, identity, now=now)
            if record is not None:
                source = source.model_copy(
                    update={
                        "license_basis": record.basis,
                        "delivery_terms": record.delivery_terms
                        if record.rights.deliver
                        else None,
                    }
                )
            result.append(source)
        return result

    async def sources(self, subject: str) -> dict:
        return await self.registry.sources(subject)

    async def _source(
        self, owner: str, subject: str, source: FormalMethodSource | None
    ) -> bool:
        if source is None:
            return True
        if source.kind in ("registered_script", "registered_instruction"):
            await self.registry.check_direct_source(owner, source)
            if source.kind == "registered_script":
                from app.domain.workbench.script_contract_generated import ScriptCommand

                try:
                    ScriptCommand(path=source.entry)
                except ValueError as exc:
                    raise CapabilityError(
                        "Registered entry is not a supported script path"
                    ) from exc
                return True
            raise RuntimeCapabilityGap(
                "Direct source is registered; its trial executor is not bound"
            )
        if source.asset_id is None:
            # Platform implementations need their own reviewed source records;
            # being built in does not exempt them from incubation.
            raise CapabilityError(
                "Platform implementation has no incubation source record"
            )
        asset = await self._installed_source(owner, subject, source)
        if source.license_basis == "pending":
            return False
        if source.kind == "skill_instruction":
            await self._instruction_text(owner, asset)
        return True

    async def _installed_source(self, owner, subject, source):
        asset = await self.repo.capability_asset(owner, str(source.asset_id), lock=True)
        if source not in self._sources(asset, subject, now=self.now()):
            raise CapabilityError(
                "Installed source changed, is unavailable or belongs to another author"
            )
        return asset

    async def _instruction_text(self, owner, asset):
        data = await self.repo.skill_file(owner, str(asset["id"]), "SKILL.md")
        return verified_instruction(asset["detail"]["files"], data)

    async def prepare_instruction_trial(self, subject, run_id, arguments, *, caller):
        """Resolve a retained, single-turn host draft and its inputs for a local rehearsal."""
        async with self.repo.transaction():
            owner = await self._owner(subject)
            row = await self.repo.formal_validation(owner, str(run_id), lock=True)
            if row is None or row["caller"] != caller:
                raise NotFound("Formal method validation run not found")
            req = FormalMethodValidationRequest.model_validate(row["request"])
            source = req.draft.source
            definition = req.draft.definition
            step = definition.procedure
            if (
                row["result"].get("status") != "pending"
                or str(req.run_id) != str(run_id)
                or req.owner_subject != subject
                or digest(row["request"]) != row["request_digest"]
                or source is None
                or source.kind != "skill_instruction"
                or source.license_basis == "pending"
                or req.draft.allowed_callers != ("host",)
                or step.kind != "direct"
                or step.max_model_turns != 1
                or req.dependencies
                or definition.access_requirements
                or definition.what_requires_approval
                or step.implementation.requirement_ids
            ):
                raise CapabilityError(
                    "The trial needs a licensed single-turn instruction with no tools"
                )
            asset = await self._installed_source(owner, subject, source)
            if asset["detail"].get("tool_requirements") or asset["detail"].get(
                "dependencies"
            ):
                raise CapabilityError(
                    "The Skill requires tools outside this instruction trial"
                )
            bounded_json(arguments)
            validate_values(step.inputs, arguments)
            return req, await self._instruction_text(owner, asset)

    async def dependency(self, owner, reference, *, shared=True):
        return await self.registry.dependency(owner, reference, shared=shared)

    async def binding_options(self, owner, frozen):
        from app.domain.workbench.formal_execution import same_implementation

        subject = await self.repo.subject_of_actor(owner)
        options = {}
        for asset in await self.repo.capability_assets(owner):
            for source in self._sources(asset, subject, now=self.now()):
                if source.license_basis != "pending" and same_implementation(
                    frozen.draft.source, source
                ):
                    options[source.asset_id] = (
                        source,
                        asset["name"],
                        asset["detail"].get("account_label"),
                    )
        preferred = await self.repo.incubation_asset(owner, frozen.reference)
        selected = preferred
        if selected is None and len(options) == 1:
            selected = next(iter(options))
        return options, selected if selected in options else None

    async def bind_tool(self, owner, frozen, grant):
        from app.domain.workbench.formal_execution import IncubationBinding

        options, selected = await self.binding_options(owner, frozen)
        if selected is None:
            raise CapabilityError(
                "Select a matching licensed installation for this tool"
            )
        return IncubationBinding(
            reference=frozen.reference,
            listing_id=grant.listing_id,
            source=options[selected][0],
        )

    async def choose_incubation_asset(self, subject, reference, asset_id, revision):
        from app.application.workbench.formal_method_catalog import FormalMethodCatalog

        async with self.repo.transaction():
            owner = await self._owner(subject)
            frozen, grant = await self.dependency(owner, reference)
            if not grant:
                raise Forbidden("A shared-tool permission is required")
            await FormalMethodCatalog(self.repo, self, self.operations).shared_tool(
                grant
            )
            options, _ = await self.binding_options(owner, frozen)
            if (
                asset_id not in options
                or options[asset_id][0].asset_revision != revision
            ):
                raise CapabilityError("The selected installation changed")
            await self.repo.select_incubation_asset(owner, reference, asset_id)
        return {
            "reference": reference.model_dump(mode="json"),
            "asset_id": str(asset_id),
            "revision": revision,
        }

    async def shared_tools(self, subject, *, reference=None, after=None, limit=30):
        return await self.registry.shared_tools(
            subject, reference=reference, after=after, limit=limit
        )

    async def _compile(self, owner: str, method_id, definition, *, documents=None):
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
                    frozen, _ = await self.dependency(
                        owner, ref, shared=documents is None
                    )
                    if documents is not None:
                        documents.append(frozen)
                    resolved[key] = FrozenMethod(ref, frozen.draft.definition)
                    await collect(frozen.draft.definition, (*path, ref.method_id))

        await collect(definition)
        try:
            return compile_method(method_id, definition, resolved)
        except ValueError as exc:
            raise CapabilityError(str(exc)) from exc

    async def check_delivery_source(self, owner, frozen, run=None, *, market=True):
        source = frozen.draft.source
        if market:
            if frozen.visibility != "public" or not frozen.validation.execution:
                raise Forbidden("Version has no public validation")
            run = run or await self.repo.formal_validation(
                owner, str(frozen.validation.run_id)
            )
            review = self.business_review(
                frozen.owner_subject,
                frozen.reference.method_id,
                frozen.draft,
                frozen.validation.execution.suite_digest,
            )
            evidence = (run or {}).get("execution_evidence") or {}
            if (
                not review
                or evidence.get("business_review_digest") != review.fingerprint
                or fingerprint(evidence) != frozen.validation.execution.evidence_digest
            ):
                raise Forbidden("Business coverage review is missing or has changed")
        if source is None:
            return
        if source.kind == "registered_script":
            raise Forbidden("Owner-registered scripts have no reviewed delivery rights")
        if not await self._source(owner, frozen.owner_subject, source):
            raise Forbidden("Source is unavailable")
        identity = SourceIdentity.model_validate(
            source.model_dump(include={"kind", "source", "version", "digest", "entry"})
        )
        record = self.source_licenses.executable(
            frozen.owner_subject, identity, now=self.now()
        )
        if (
            not record
            or not record.rights.deliver
            or (source.kind.startswith("skill_") and not source.delivery_terms)
        ):
            raise Forbidden("Source has no reviewed delivery right")

    async def self_review_outputs(self, subject, reference):
        """Return only verified test outputs to the authenticated incubator on author submission."""
        import json

        async with self.repo.transaction():
            owner = await self._owner(subject)
            row = await self.repo.formal_method(
                owner, str(reference.method_id), reference.version
            )
            if not row or row["caller"] != "knowlab":
                raise NotFound("Registered version not found")
            frozen = FrozenFormalMethod.model_validate(row["document"])
            if frozen.reference != reference or frozen.owner_subject != subject:
                raise Forbidden("Different registered version")
            run = await self.repo.formal_validation(
                owner, str(frozen.validation.run_id)
            )
            evidence = (run or {}).get("execution_evidence")
            execution = frozen.validation.execution
            samples = []
            size = 0
            complete = bool(
                execution
                and evidence
                and fingerprint(evidence) == execution.evidence_digest
            )
            if complete and evidence is not None:
                for case in evidence["cases"]:
                    result = case["result"]
                    if not case["validated"] or result["status"] != "passed":
                        complete = False
                        break
                    if result["outcome"] == "rejected":
                        continue
                    key = f"{result['case_id']}:{result['repetition']}"
                    outputs = case.get("outputs")
                    if outputs is None:
                        outputs = await self.repo.get_test_case_outputs(
                            str(frozen.validation.run_id), key
                        )
                    if not isinstance(outputs, dict) or digest(outputs) != case.get(
                        "outputs_digest"
                    ):
                        complete = False
                        break
                    artifacts = [outputs]
                    for step in case.get("steps", []):
                        artifacts.extend(
                            step[k] for k in ("outputs", "method_outputs") if k in step
                        )
                    sample = {"steps": [{"artifact": v} for v in artifacts]}
                    size += len(json.dumps(sample, ensure_ascii=False).encode())
                    if size > 1024 * 1024:
                        complete = False
                        break
                    samples.append(sample)
            return {
                "owner_subject": subject,
                "reference": reference.model_dump(mode="json"),
                "run_id": str(frozen.validation.run_id),
                "samples": samples if complete else [],
            }

    async def delivery(self, subject, reference, *, market, skill=None):
        async with self.repo.transaction():
            owner = await self._owner(subject)
            row = await self.repo.formal_method(
                owner, str(reference.method_id), reference.version
            )
            if not row or row["caller"] != "knowlab":
                raise NotFound("Formal method not found")
            root = FrozenFormalMethod.model_validate(row["document"])
            if root.reference != reference or root.owner_subject != subject:
                raise Forbidden("Different frozen version")
            documents = [root]
            await self._compile(
                owner, reference.method_id, root.draft.definition, documents=documents
            )
            locks = []
            selected = None
            for method in sorted(
                documents,
                key=lambda m: str(m.draft.source.asset_id) if m.draft.source else "",
            ):
                if method.owner_subject != subject:
                    raise Forbidden("Dependency is owned by another author")
                await self.check_delivery_source(owner, method, market=market)
            for method in sorted(
                documents, key=lambda m: (m.reference.method_id, m.reference.version)
            ):
                source = method.draft.source
                locks.append(
                    {
                        "reference": method.reference.model_dump(mode="json"),
                        "source_digest": source.fingerprint() if source else None,
                    }
                )
                if (
                    method.reference == skill
                    and source
                    and source.kind.startswith("skill_")
                ):
                    selected = source
            result = {
                "contract": "knowbench/formal-method-delivery/v1",
                "owner_subject": subject,
                "reference": reference.model_dump(mode="json"),
                "delivery_digest": "sha256:" + digest(locks),
                "title": root.draft.definition.capability_declaration.title,
                "purpose": root.draft.definition.capability_declaration.purpose,
                "skill_dependencies": [
                    {
                        "method": m.reference.model_dump(mode="json"),
                        "version": m.draft.source.version,
                        "manifest_digest": m.draft.source.digest,
                        "terms": m.draft.source.delivery_terms.model_dump(mode="json"),
                    }
                    for m in documents
                    if m.draft.source and m.draft.source.delivery_terms
                ],
            }
            if skill is not None:
                if not market or selected is None or not selected.delivery_terms:
                    raise NotFound("Skill is not part of the frozen method")
                asset_id = str(selected.asset_id)
                asset = await self.repo.capability_asset(owner, asset_id, lock=True)
                files = asset["detail"]["files"]
                contents = await self.repo.skill_package_files(owner, asset_id)
                result["package"] = {
                    "method": skill.model_dump(mode="json"),
                    "version": selected.version,
                    "manifest_digest": selected.digest,
                    "terms": selected.delivery_terms.model_dump(mode="json"),
                    "files": delivery_files(files, contents),
                }
            return result

    async def validate(self, request: FormalMethodValidationRequest, *, caller: str):
        self.require_supported_graph(request.draft)
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
            licensed = await self._source(
                owner, request.owner_subject, request.draft.source
            )
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
        self.require_supported_graph(frozen.draft)
        if (
            isinstance(frozen.draft.definition.procedure, Orchestration)
            and frozen.visibility != "private"
        ):
            raise Forbidden(
                "Formal expert execution currently qualifies private use only"
            )
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
            if frozen.validation.execution is not None:
                evidence = run.get("execution_evidence")
                if not evidence or (
                    fingerprint(evidence) != frozen.validation.execution.evidence_digest
                    or evidence.get("request_digest")
                    != frozen.validation.execution.request_digest
                ):
                    raise Forbidden(
                        "Execution evidence is missing or differs from the receipt"
                    )
            if (
                frozen.draft.source
                and frozen.draft.source.kind == "registered_script"
                and (run.get("execution_evidence") or {}).get("adapter")
                != "knowbench/formal-script-host/v1"
            ):
                raise Forbidden(
                    "Registered script methods require verified script execution"
                )
            if frozen.draft.source and frozen.draft.source.kind == "skill_instruction":
                if (
                    frozen.visibility != "private"
                    or frozen.validation.execution is None
                    or (run.get("execution_evidence") or {}).get("adapter")
                    != "knowbench/formal-instruction-host/v1"
                ):
                    raise Forbidden(
                        "Instruction registration requires private single-turn execution evidence"
                    )
            if isinstance(frozen.draft.definition.procedure, Orchestration):
                from app.application.workbench.formal_orchestration import (
                    resolve_owned_plan,
                )

                if frozen.validation.execution is None or (
                    run.get("execution_evidence") or {}
                ).get("adapter") not in (
                    "knowbench/formal-orchestration-host/v1",
                    "knowbench/formal-orchestration-tree-host/v1",
                ):
                    raise Forbidden(
                        "Formal experts require actual orchestration execution evidence"
                    )
                dependencies = await resolve_owned_plan(
                    self.repo,
                    self,
                    self.operations,
                    owner,
                    frozen.draft,
                    frozen.reference.method_id,
                )
                if frozen.visibility == "public" and any(
                    dep.draft.source and dep.draft.source.kind == "skill_instruction"
                    for dep in dependencies
                ):
                    raise Forbidden(
                        "An expert using a private instruction requires private registration"
                    )
                if frozen.visibility == "public" and any(
                    dep.owner_subject != frozen.owner_subject for dep in dependencies
                ):
                    raise Forbidden(
                        "Incubation permission does not grant public distribution"
                    )
            if not await self._source(owner, frozen.owner_subject, frozen.draft.source):
                raise Forbidden("Source execution rights have not been verified")
            compilation = await self._compile(
                owner, frozen.reference.method_id, frozen.draft.definition
            )
            if set(compilation.dependencies) != set(frozen.validation.dependencies):
                raise Forbidden("Frozen dependencies differ from validation")
            if frozen.visibility == "public":
                # KnowMart independently qualifies listing and acquisition.
                await self.check_delivery_source(owner, frozen, run)
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
