"""Resolve private registered methods without treating registration as authority.

Historical admission is checked from its retained evidence; current source and
operation rights are checked separately. No incubation grant is returned for use.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.application.workbench.tool_registry import registered_call
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.errors import NotFound, WorkbenchError
from app.domain.workbench.formal_execution import (
    FORMAL_RUNTIME,
    FormalTaskSnapshot,
    direct_procedure,
    validate_values,
)
from app.domain.workbench.formal_method_admission_generated import (
    GOVERNANCE_CHECKS,
    FormalMethodValidationRequest,
    FormalMethodValidationResult,
    FrozenFormalMethod,
    UncoveredScenario,
)
from app.domain.workbench.formal_method_execution_generated import (
    FormalExecutionCaseResult,
    FormalMethodExecutionRequest,
    fingerprint,
)
from app.domain.workbench.formal_method_generated import MethodRef
from app.domain.workbench.method_authorization import MethodAuthorization
from app.domain.workbench.tool_operations import McpOperation, OperationCatalog
from app.domain.workbench.tool_registry import ToolRegistration


@dataclass(frozen=True)
class ResolvedOwnedMethod:
    frozen: FrozenFormalMethod
    asset: dict[str, Any]
    registration: ToolRegistration
    policy: McpOperation
    uncovered: tuple[UncoveredScenario, ...]


def _require(condition, issue):
    if not condition:
        raise CapabilityError(issue)


class FormalMethodCatalog:
    def __init__(self, repo, formal_sources, operations: OperationCatalog):
        self.repo, self.formal_sources, self.operations = (
            repo,
            formal_sources,
            operations,
        )

    async def resolve_owned(self, owner, reference: MethodRef) -> ResolvedOwnedMethod:
        """Only the current actor's exact admitted version; no market fallback."""
        async with self.repo.transaction():
            row = await self.repo.formal_method(
                owner, str(reference.method_id), reference.version
            )
            if row is None:
                raise NotFound("Private formal method not found")
            try:
                return await self._resolve(owner, reference, row)
            except (
                ValueError,
                TypeError,
                KeyError,
                IndexError,
                AttributeError,
                InvalidOperation,
            ) as exc:
                raise CapabilityError(
                    "Formal method admission evidence is invalid"
                ) from exc

    async def _resolve(self, owner, reference, row):
        frozen = FrozenFormalMethod.model_validate(row["document"])
        _require(
            str(row["owner_actor_id"]) == str(owner)
            and row["caller"] == "knowlab"
            and frozen.reference == reference
            and row["digest"] == reference.digest
            and str(row["method_id"]) == str(reference.method_id)
            and row["version"] == reference.version
            and await self.repo.actor_for_subject(frozen.owner_subject) == str(owner)
            and str(row["run_id"]) == str(frozen.validation.run_id),
            "Formal method identity does not match its author registration",
        )
        _require(
            frozen.visibility == "private",
            "This entry supports only the author's private methods",
        )
        direct_procedure(frozen.draft)
        _require(
            len(frozen.draft.definition.access_requirements) == 1
            and not frozen.validation.dependencies,
            "Only one reviewed direct MCP operation is supported",
        )
        execution_ref = frozen.validation.execution
        _require(
            execution_ref is not None,
            "Formal method has no real execution admission evidence",
        )
        assert execution_ref is not None
        run_id = str(frozen.validation.run_id)
        admission_row = await self.repo.formal_validation(owner, run_id)
        _require(
            admission_row is not None and admission_row["caller"] == "knowlab",
            "Formal method admission is unavailable",
        )
        assert admission_row is not None
        admission = FormalMethodValidationRequest.model_validate(
            admission_row["request"]
        )
        result = FormalMethodValidationResult.model_validate(admission_row["result"])
        _require(
            result.status == "passed"
            and result.receipt == frozen.validation
            and admission.draft == frozen.draft
            and admission.owner_subject == frozen.owner_subject
            and admission.method_id == reference.method_id
            and admission.run_id == frozen.validation.run_id
            and admission.request_id == frozen.validation.request_id
            and admission.workdraft_id == frozen.validation.workdraft_id
            and admission.revision == frozen.validation.revision
            and admission_row["request_digest"]
            == digest(admission.model_dump(mode="json", by_alias=True)),
            "Formal method differs from its completed admission",
        )
        execution_row = await self.repo.get_test_run(run_id)
        _require(
            execution_row is not None
            and execution_row["caller"] == "knowlab"
            and execution_row["owner_subject"] == frozen.owner_subject
            and execution_row["status"] == "passed",
            "Formal method has no completed execution run",
        )
        assert execution_row is not None
        request = FormalMethodExecutionRequest.model_validate(execution_row["request"])
        _require(
            request.admission_request() == admission
            and request.run_id == frozen.validation.run_id
            and execution_ref.request_digest == request.fingerprint()
            and execution_row["digest"] == request.fingerprint().removeprefix("sha256:")
            and (
                execution_ref.suite_id,
                execution_ref.suite_version,
                execution_ref.suite_digest,
            )
            == (
                request.execution.suite.suite_id,
                request.execution.suite.version,
                request.execution.suite.digest,
            ),
            "Formal execution request or test suite changed",
        )
        evidence = admission_row.get("execution_evidence")
        _require(
            isinstance(evidence, dict)
            and fingerprint(evidence) == execution_ref.evidence_digest,
            "Formal execution evidence is missing or changed",
        )
        _require(
            evidence["contract"] == FORMAL_RUNTIME
            and evidence["adapter"] == "knowbench/formal-mcp-host/v1"
            and evidence["request_digest"] == request.fingerprint()
            and evidence["definition_digest"] == frozen.reference.digest
            and evidence["suite"]
            == request.execution.suite.reference().model_dump(mode="json")
            and evidence["external_provider_billing"] == "reviewed_no_incremental_fee",
            "Formal evidence does not qualify this execution path",
        )
        _require(
            Decimal(evidence["model_cost"]) == 0
            and set(frozen.validation.checks) == GOVERNANCE_CHECKS
            and bool(frozen.validation.uncovered),
            "This adapter requires zero model use and explicit uncovered business scope",
        )
        sealed = datetime.fromisoformat(evidence["sealed_at"])
        _require(
            sealed.tzinfo is not None and sealed <= frozen.validation.completed_at,
            "Formal evidence has no valid seal time",
        )

        source = frozen.draft.source
        _require(
            source is not None and source.kind == "mcp" and source.asset_id is not None,
            "Formal method has no pinned MCP source",
        )
        assert source is not None
        _require(
            await self.formal_sources._source(owner, frozen.owner_subject, source),
            "Source execution rights are unavailable",
        )
        asset = await self.repo.capability_asset(owner, str(source.asset_id), lock=True)
        tool = next(
            (t for t in asset["detail"].get("tools", []) if t["name"] == source.entry),
            None,
        )
        _require(tool is not None, "The registered MCP implementation is unavailable")
        registration = registered_call(asset, {"tool": tool})
        policy = self.operations.require(registration)
        _require(
            policy.formal_execution_cost is not None,
            "The adapter has no reviewed evidence of zero incremental fees",
        )
        self._verify_cases(owner, frozen, request, evidence, registration, policy)
        return ResolvedOwnedMethod(
            frozen, asset, registration, policy, frozen.validation.uncovered
        )

    @staticmethod
    def _verify_cases(owner, frozen, request, evidence, registration, policy):
        cases = {
            (c.case_id, n): c
            for c in request.execution.suite.cases
            for n in range(1, c.repetitions + 1)
        }
        retained = evidence["cases"]
        _require(
            isinstance(retained, list) and len(retained) == len(cases),
            "Formal evidence does not cover every case repetition",
        )
        seen, passed, business_success = set(), {}, False
        source = frozen.draft.source
        assert source is not None
        for item in retained:
            result = FormalExecutionCaseResult.model_validate(item["result"])
            key = (result.case_id, result.repetition)
            _require(
                key in cases and key not in seen,
                "Formal evidence repeats or replaces a case",
            )
            seen.add(key)
            case = cases[key]
            _require(
                item.get("validated") is True
                and item.get("errors") == []
                and item.get("model_turn_ids") == []
                and result.status in ("passed", "failed")
                and result.outcome in ("succeeded", "failed", "rejected")
                and result.task_id is not None
                and result.attempt_id is not None,
                "A required execution or governance check is missing",
            )
            snapshot = FormalTaskSnapshot(
                runtime_contract=FORMAL_RUNTIME,
                run_id=request.run_id,
                case_id=case.case_id,
                repetition=result.repetition,
                request_digest=request.fingerprint(),
                method_id=request.method_id,
                draft=request.draft,
                arguments=case.input.values,
                definition_digest=request.draft.definition.digest(),
            )
            _require(
                item["snapshot_digest"] == digest(snapshot.document()),
                "Case snapshot differs from its frozen request",
            )
            _require(
                isinstance(item.get("outputs_digest"), str)
                and len(item["outputs_digest"]) == 64,
                "Case output evidence is missing",
            )
            if result.outcome == "succeeded":
                _require(
                    [a.index for a in result.assertions]
                    == list(range(len(case.expected.assertions))),
                    "Output assertion coverage changed",
                )
            else:
                _require(
                    not result.assertions, "Unexecuted output assertions cannot pass"
                )
            correct = result.outcome == case.expected.outcome and (
                all(a.passed for a in result.assertions)
                if result.outcome == "succeeded"
                else result.rejection_code == case.expected.rejection_code
            )
            _require(
                (result.status == "passed") == correct,
                "Case verdict differs from its frozen assertions",
            )
            passed[result.case_id] = passed.get(result.case_id, 0) + int(correct)
            business_success |= correct and result.outcome == "succeeded"
            governance = item.get("governance")
            if (
                result.rejection_code == "input_invalid"
                and result.outcome == "rejected"
            ):
                invalid = False
                try:
                    validate_values(
                        direct_procedure(frozen.draft).inputs, case.input.values
                    )
                except ValueError:
                    invalid = True
                _require(
                    invalid and governance is None and result.call_id is None,
                    "Input refusal evidence is not reproducible",
                )
                continue
            _require(
                isinstance(governance, dict) and result.call_id is not None,
                "Real operation governance evidence is missing",
            )
            expected_host = {
                "task_id": str(result.task_id),
                "attempt_id": str(result.attempt_id),
                "snapshot_digest": item["snapshot_digest"],
                "slot": "direct",
                "method_id": str(frozen.reference.method_id),
                "run_id": str(request.run_id),
                "case_id": case.case_id,
                "repetition": result.repetition,
                "source_digest": source.fingerprint(),
            }
            operation = policy.resolve(case.input.values)
            _require(
                governance["call_id"] == str(result.call_id)
                and governance["formal_host"] == expected_host
                and governance["input_digest"] == digest(case.input.values)
                and governance["registration_id"] == registration.tool_id
                and governance["registration_digest"] == registration.descriptor_digest
                and governance["operation"] == operation,
                "Current operation policy differs from the validated operation",
            )
            approval = governance["approval"]
            if result.outcome == "rejected":
                _require(
                    result.rejection_code == "approval_denied"
                    and governance["approved"] is False
                    and governance["status"] in ("declined", "expired")
                    and approval["decision"] != "accept",
                    "Action refusal evidence is incomplete",
                )
                continue
            _require(
                governance["approved"] is True
                and governance["direct_grants"] is True
                and approval["decision"] == "accept"
                and approval["responded_by"] == str(owner)
                and governance["interaction_status"] == "consumed"
                and (
                    governance["status"] == "succeeded"
                    if result.outcome == "succeeded"
                    else governance["status"] in ("succeeded", "failed")
                ),
                "The operation lacks exact approved execution evidence",
            )
            auth = MethodAuthorization.model_validate(governance["authorization"])
            _require(
                digest(auth.model_dump(mode="json"))
                == governance["authorization_digest"]
                and auth.revision == governance["authorization_revision"]
                and not auth.revoked
                and auth.parent_call_id is None
                and auth.method_id == str(frozen.reference.method_id)
                and len(auth.grants) == 1,
                "The retained authorization is invalid",
            )
            grant = auth.grants[0]
            declared = frozen.draft.definition.access_requirements[0].model_dump(
                mode="json"
            )
            exact = {
                **declared,
                "operations": [operation["operation"]],
                "resources": operation["resources"],
                "data_transfer": {
                    **declared["data_transfer"],
                    "argument_fields": sorted(case.input.values),
                },
            }
            approved_at = datetime.fromisoformat(approval["consumed_at"])
            _require(
                not grant.delegable
                and grant.recipient_call_id == auth.call_id
                and str(grant.connection_id) == str(source.asset_id)
                and grant.connection_revision == source.asset_revision
                and grant.operation_digest == policy.policy_digest
                and grant.confirmation_context == item["snapshot_digest"]
                and grant.method_ids == (str(frozen.reference.method_id),)
                and grant.requirement.model_dump(mode="json") == exact
                and approved_at.tzinfo is not None
                and approved_at <= frozen.validation.completed_at
                and approved_at < grant.expires_at
                and grant.expires_at
                == auth.expires_at
                == datetime.fromisoformat(approval["expires_at"]),
                "The retained grant does not cover the tested action",
            )
        _require(
            business_success
            and all(
                passed.get(c.case_id, 0) >= c.minimum_passes
                for c in request.execution.suite.cases
            ),
            "Formal execution did not meet its frozen test thresholds",
        )

    async def list_owned(self, owner) -> list[dict]:
        async with self.repo.transaction():
            rows = await self.repo.list_formal_methods(owner)
        items = []
        for row in rows:
            try:
                frozen = FrozenFormalMethod.model_validate(row["document"])
            except (ValueError, TypeError, KeyError):
                continue
            # Never disclose a document whose stored actor and subject disagree.
            async with self.repo.transaction():
                if await self.repo.actor_for_subject(frozen.owner_subject) != str(
                    owner
                ):
                    continue
            issue = None
            try:
                await self.resolve_owned(owner, frozen.reference)
            except WorkbenchError as exc:
                issue = exc.message[:2000]
            definition = frozen.draft.definition
            interfaces = definition.interfaces()
            items.append(
                {
                    "reference": frozen.reference.model_dump(mode="json"),
                    "title": definition.capability_declaration.title[:20000],
                    "purpose": definition.capability_declaration.purpose,
                    "inputs": {
                        key: value["slot"]
                        for key, value in interfaces["inputs"].items()
                    },
                    "outputs": {
                        key: value["slot"]
                        for key, value in interfaces["outputs"].items()
                    },
                    "access_requirements": [
                        r.model_dump(mode="json", by_alias=True)
                        for r in definition.access_requirements
                    ],
                    "uncovered": [
                        u.model_dump(mode="json") for u in frozen.validation.uncovered
                    ],
                    "available": issue is None,
                    "issue": issue,
                }
            )
        return items
