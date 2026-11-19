"""Resolve private registered methods without treating registration as authority.

Historical admission is checked from its retained evidence; current source and
operation rights are checked separately. No incubation grant is returned for use.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

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
from app.domain.workbench.formal_method_generated import MethodRef, Orchestration
from app.domain.workbench.formal_model import (
    PROBE_SERVER,
    PROBE_TOOL,
    verified_probe_profile,
)
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


def _money(value):
    amount = Decimal(str(value))
    _require(amount.is_finite() and amount >= 0, "Invalid sealed model cost")
    return amount


def _token_totals(value):
    keys = ("input", "cached_input", "cache_write", "output")
    _require(
        isinstance(value, dict)
        and set(value) == {*keys, "total"}
        and all(type(value[k]) is int and value[k] >= 0 for k in (*keys, "total"))
        and value["total"] == sum(value[k] for k in keys),
        "Invalid sealed token accounting",
    )
    return value


def _hash(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _verify_model_case(request, snapshot, item, result, identities):
    """Verify the sealed collector projection, including after Task cleanup.

    The authoritative receipt binds this whole projection by digest. Event
    references are historical proof, never accepted as live invocation authority.
    """
    proof = item["model_provenance"]
    trace = result.model_trace
    _require(
        proof["validated"] is True and proof["errors"] == [] and trace is not None,
        "Model provenance is unqualified",
    )
    assert trace is not None
    invocation, isolation = proof["invocation"], proof["isolation"]
    _require(
        item["model_turn_ids"] == [trace.turn_id]
        and invocation["thread_id"] == trace.thread_id
        and invocation["turn_id"] == trace.turn_id
        and invocation["call_id"] == trace.tool_call_id
        and invocation["server"] == PROBE_SERVER
        and isinstance(invocation["session_id"], str)
        and bool(invocation["session_id"])
        and invocation["key"]
        == digest(
            [
                invocation["session_id"],
                trace.thread_id,
                trace.turn_id,
                PROBE_SERVER,
                trace.tool_call_id,
            ]
        ),
        "Model trace differs from the actual candidate invocation",
    )
    for kind, value in (
        ("session", invocation["session_id"]),
        ("thread", trace.thread_id),
        ("turn", trace.turn_id),
        ("root", str(trace.method_call_id)),
        ("scope", isolation["scope_id"]),
    ):
        _require(
            (kind, value) not in identities,
            "Model execution scope was reused across cases",
        )
        identities.add((kind, value))
    _require(
        verified_probe_profile(isolation)
        and isolation["tools"] == [PROBE_TOOL]
        and isolation["startup_features"]
        == {"multi_agent": False, "multi_agent_v2": False, "goals": False}
        and isolation["thread_id"] == trace.thread_id
        and isolation["attempt_id"] == str(result.attempt_id)
        and isolation["snapshot_digest"] == item["snapshot_digest"]
        and isinstance(isolation["scope_id"], str)
        and bool(isolation["scope_id"])
        and type(isolation["selection_revision"]) is int
        and isolation["selection_revision"] >= 0
        and isinstance(isolation["disabled_servers"], list)
        and all(
            isinstance(name, str) and name != PROBE_SERVER
            for name in isolation["disabled_servers"]
        ),
        "Model execution isolation is not the reviewed probe scope",
    )
    procedure = direct_procedure(snapshot.draft, caller="model")
    schema = {
        "type": "object",
        "properties": {name: slot.schema_ for name, slot in procedure.inputs.items()},
        "required": [
            name
            for name, slot in procedure.inputs.items()
            if slot.required or slot.on_missing != "omit"
        ],
        "additionalProperties": False,
    }
    inventory = isolation["inventory"]
    _require(
        isinstance(inventory, list) and bool(inventory),
        "Model tool inventory is missing",
    )
    names = set()
    for server in inventory:
        name = server["name"]
        _require(
            isinstance(name, str) and bool(name) and name not in names,
            "Model inventory repeats a server",
        )
        names.add(name)
        _require(
            server["resources"] == [] and server["resourceTemplates"] == [],
            "Model probe exposed resources",
        )
        _require(
            (
                server["tools"] == [{"name": PROBE_TOOL, "inputSchema": schema}]
                and not server["toolsError"]
                and server["runtimeStatus"] in (None, "connected")
            )
            if name == PROBE_SERVER
            else server["tools"] == [] and name in isolation["disabled_servers"],
            "Model inventory differs from the candidate-only scope",
        )
    _require(PROBE_SERVER in names, "Candidate tool was not exposed")
    groups = (
        (proof["turn_events"], ["turn/started", "turn/completed"]),
        (proof["call_events"], ["item/started", "item/completed"]),
        (proof["model_events"], None),
        (proof["usage"], None),
    )
    for events, expected in groups:
        _require(
            isinstance(events, list) and bool(events),
            "Model execution event evidence is missing",
        )
        if expected:
            _require(
                [event["type"] for event in events] == expected,
                "Model event lifecycle is incomplete",
            )
        for event in events:
            _require(
                isinstance(event["id"], str)
                and bool(event["id"])
                and type(event["cursor"]) is int
                and event["cursor"] >= 0
                and _hash(event["payload_digest"])
                and ("event", event["id"]) not in identities,
                "Invalid or reused model event reference",
            )
            identities.add(("event", event["id"]))
    start, end = [event["cursor"] for event in proof["turn_events"]]
    call_start, call_end = [event["cursor"] for event in proof["call_events"]]
    _require(
        start < call_start < call_end < end
        and all(
            event["type"] == "session/thread_started" and event["cursor"] < start
            for event in proof["model_events"]
        )
        and _hash(proof["result_digest"]),
        "Model events are out of order or missing the delivered result",
    )
    total = Decimal("0")
    tokens = dict.fromkeys(
        ("input", "cached_input", "cache_write", "output", "total"), 0
    )
    models, previous = set(), start
    for event in proof["usage"]:
        price = event["cost"]
        _require(
            event["type"] == "thread/tokenUsage/updated"
            and previous < event["cursor"] < end
            and price["priced"] is True
            and price["currency"] == request.execution.budget.currency
            and isinstance(price["model"], str)
            and bool(price["model"]),
            "Model usage was not priced for this run",
        )
        previous = event["cursor"]
        models.add(price["model"])
        call_tokens = _token_totals(price["call_tokens"])
        for key in tokens:
            tokens[key] += call_tokens[key]
        total += _money(price["call"])
        _require(
            total == _money(price["turn"])
            and tokens == _token_totals(price["turn_tokens"]),
            "Model usage accumulation differs",
        )
    ledger = proof["ledger"]
    _require(isinstance(ledger, list), "Model fee ledger is missing")
    for entry in ledger:
        _require(
            isinstance(entry["id"], str)
            and bool(entry["id"])
            and ("ledger", entry["id"]) not in identities,
            "Model fee ledger entry was reused",
        )
        identities.add(("ledger", entry["id"]))
    _require(
        len(models) == 1
        and total == _money(proof["cost"]) == _money(proof["budget_consumed"]["amount"])
        and tokens
        == _token_totals(proof["tokens"])
        == _token_totals(proof["budget_consumed"]["tokens"])
        and sum((_money(entry["amount"]) for entry in ledger), Decimal("0")) == total,
        "Model costs, tokens and consumed budget do not reconcile",
    )
    return total


class FormalMethodCatalog:
    def __init__(self, repo, formal_sources, operations: OperationCatalog):
        self.repo, self.formal_sources, self.operations = (
            repo,
            formal_sources,
            operations,
        )

    async def resolve_owned(
        self, owner, reference: MethodRef, *, caller: Literal["host", "model"] = "host"
    ) -> ResolvedOwnedMethod:
        """Only the current actor's exact admitted version; no market fallback."""
        async with self.repo.transaction():
            row = await self.repo.formal_method(
                owner, str(reference.method_id), reference.version
            )
            if row is None:
                raise NotFound("Private formal method not found")
            try:
                return await self._resolve(owner, reference, row, caller)
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

    async def resolve_owned_admission(
        self, owner, reference: MethodRef, *, resolved
    ) -> FrozenFormalMethod:
        """Internal dependency qualification; never expands the conversation catalog.

        ``resolved`` contains the owner's exact FrozenFormalMethod closure.
        The caller locks all sources in order and checks leaves before parents.
        """
        async with self.repo.transaction():
            row = await self.repo.formal_method(
                owner, str(reference.method_id), reference.version
            )
            if row is None:
                raise NotFound("Private formal method not found")
            try:
                frozen = FrozenFormalMethod.model_validate(row["document"])
                if not isinstance(frozen.draft.definition.procedure, Orchestration):
                    return (
                        await self.resolve_owned(owner, reference, caller="host")
                    ).frozen
                frozen, request, evidence = await self._admission(
                    owner, reference, row, orchestration=True
                )
                from app.application.workbench.formal_orchestration_admission import (
                    verify_sealed_plan,
                )

                verify_sealed_plan(
                    owner,
                    frozen,
                    request,
                    evidence,
                    resolved,
                    operations=self.operations,
                )
                return frozen
            except (
                ValueError,
                TypeError,
                KeyError,
                IndexError,
                AttributeError,
                InvalidOperation,
            ) as exc:
                raise CapabilityError(
                    "Formal expert admission evidence is invalid"
                ) from exc

    async def _admission(self, owner, reference, row, *, orchestration=False):
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
            and (
                evidence["adapter"]
                in (
                    "knowbench/formal-orchestration-host/v1",
                    "knowbench/formal-orchestration-tree-host/v1",
                )
                if orchestration
                else evidence["adapter"]
                == (
                    "knowbench/formal-mcp-caller-probe/v1"
                    if "model" in frozen.draft.allowed_callers
                    else "knowbench/formal-mcp-host/v1"
                )
            )
            and evidence["request_digest"] == request.fingerprint()
            and evidence["definition_digest"] == frozen.reference.digest
            and evidence["suite"]
            == request.execution.suite.reference().model_dump(mode="json")
            and evidence["external_provider_billing"] == "reviewed_no_incremental_fee",
            "Formal evidence does not qualify this execution path",
        )
        _require(
            execution_ref.covers_callers(frozen.draft.allowed_callers)
            and (
                evidence.get("validated_callers")
                == sorted(frozen.draft.allowed_callers)
                if "model" in frozen.draft.allowed_callers
                else evidence.get("validated_callers") in (None, ["host"])
            )
            and set(frozen.validation.checks) == GOVERNANCE_CHECKS
            and bool(frozen.validation.uncovered),
            "Caller coverage or uncovered business scope is missing",
        )
        sealed = datetime.fromisoformat(evidence["sealed_at"])
        _require(
            sealed.tzinfo is not None and sealed <= frozen.validation.completed_at,
            "Formal evidence has no valid seal time",
        )

        return frozen, request, evidence

    async def _resolve(self, owner, reference, row, caller):
        frozen, request, evidence = await self._admission(owner, reference, row)
        direct_procedure(frozen.draft, caller=caller)
        _require(
            len(frozen.draft.definition.access_requirements) == 1
            and not frozen.validation.dependencies,
            "Only one reviewed direct MCP operation is supported",
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
        seen, passed, successful_callers = set(), {}, set()
        model_cost = Decimal("0")
        identities: set[tuple[str, str]] = set()
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
                and result.caller == case.caller
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
                caller=case.caller,
                model_prompt=case.input.model_prompt,
                model_probe=request.execution.model_probe
                if case.caller == "model"
                else None,
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
            if correct and result.outcome == "succeeded":
                successful_callers.add(case.caller)
            for kind, value in (
                ("task", result.task_id),
                ("attempt", result.attempt_id),
                ("call", result.call_id),
            ):
                if value is not None:
                    identity = (kind, str(value))
                    _require(
                        identity not in identities, "Case execution identity was reused"
                    )
                    identities.add(identity)
            if case.caller == "model":
                model_cost += _verify_model_case(
                    request, snapshot, item, result, identities
                )
            else:
                _require(
                    item.get("model_turn_ids") == []
                    and not item.get("model_provenance"),
                    "Host case contains model execution",
                )
            governance = item.get("governance")
            if (
                result.rejection_code == "input_invalid"
                and result.outcome == "rejected"
            ):
                invalid = False
                try:
                    validate_values(
                        direct_procedure(frozen.draft, caller=case.caller).inputs,
                        case.input.values,
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
                and governance[
                    "formal_probe" if case.caller == "model" else "formal_host"
                ]
                == expected_host
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
                and (
                    case.caller != "model"
                    or (
                        result.model_trace is not None
                        and result.model_trace.method_call_id == auth.call_id
                    )
                )
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
            successful_callers == set(request.draft.allowed_callers)
            and all(
                passed.get(c.case_id, 0) >= c.minimum_passes
                for c in request.execution.suite.cases
            ),
            "Formal execution did not meet its frozen test thresholds",
        )

        _require(
            model_cost == _money(evidence["model_cost"])
            and (
                model_cost > 0
                if "model" in request.draft.allowed_callers
                else model_cost == 0
            )
            and model_cost < Decimal(request.execution.budget.limit),
            "Model costs do not reconcile with the sealed run budget",
        )

    async def list_owned(
        self, owner, *, caller: Literal["host", "model"] = "host"
    ) -> list[dict]:
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
                await self.resolve_owned(owner, frozen.reference, caller=caller)
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
