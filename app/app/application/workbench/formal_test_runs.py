"""Formal verification consumes the existing test-run, task and call ledgers.

Only this runtime completion path seals evidence; no client-supplied pass result
is accepted. Cleanup may remove temporary tasks after their minimal evidence
has been retained with the admission record.
"""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, cast

from app.application.workbench.formal_evidence import collect_case_evidence
from app.application.workbench.formal_execution import FormalExecution
from app.application.workbench.formal_orchestration import resolve_plan
from app.application.workbench.interactions import cancel_task
from app.application.workbench.ledger import Ledger
from app.application.workbench.session_service import SessionService
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import (
    Forbidden,
    IdempotencyConflict,
    NotFound,
    WorkbenchError,
)
from app.domain.workbench.formal_adapter import live_registration
from app.domain.workbench.formal_execution import (
    FORMAL_RUNTIME,
    FormalTaskSnapshot,
    PinnedFormalMethod,
    case_key,
    recursive_plan,
    snapshot_for_task,
)
from app.domain.workbench.formal_method_admission_generated import (
    CONTRACT,
    GOVERNANCE_CHECKS,
    Check,
    FormalMethodValidationReceipt,
    FormalMethodValidationResult,
)
from app.domain.workbench.formal_method_execution_generated import (
    FormalExecutionCaseResult,
    FormalMethodExecutionRequest,
    FormalMethodExecutionResult,
    evaluate_assertions,
    fingerprint,
)
from app.domain.workbench.formal_method_generated import Orchestration
from app.domain.workbench.models import HandoverRequest
from app.domain.workbench.states import TASK_TERMINAL, TaskState

ACTIVE = ("accepted", "running", "waiting")


def formal_run(run):
    return (run.get("request") or {}).get("contract") == FORMAL_RUNTIME


def expanded(request):
    return [
        (case, n)
        for case in request.execution.suite.cases
        for n in range(1, case.repetitions + 1)
    ]


def initial_case(case, repetition, row=None):
    row = row or {}
    return dict(
        **({"caller": case.caller} if case.caller != "host" else {}),
        case_id=case.case_id,
        repetition=repetition,
        status="pending",
        outcome="pending",
        rejection_code=None,
        task_id=str(row["task_id"]) if row.get("task_id") else None,
        attempt_id=None,
        call_id=None,
        assertions=[],
        detail="",
    )


class FormalExecutions:
    def __init__(self, repo, capabilities, *, now=None):
        self.repo, self.capabilities = repo, capabilities
        self.formal = capabilities.formal_sources
        self.now = now or (lambda: datetime.now(UTC))

    async def reviewed_policy(self, owner, request):
        source = request.draft.source
        if source is None:
            raise CapabilityError(
                "Formal execution requires a reviewed implementation source"
            )
        asset = await self.repo.capability_asset(owner, str(source.asset_id), lock=True)
        registration = live_registration(request.draft, asset)
        if source.kind == "skill_script" and self.capabilities.script_executor is None:
            raise CapabilityError("Isolated script execution is unavailable")
        return self.capabilities.operations.formal_policy(registration)

    async def load(self, subject, run_id, caller, *, lock=False):
        owner = await self.formal._owner(subject)
        run = await self.repo.get_test_run(run_id, for_update=lock)
        if (
            not run
            or not formal_run(run)
            or run["owner_subject"] != subject
            or run["caller"] != caller
        ):
            raise NotFound("Formal execution not found")
        request = FormalMethodExecutionRequest.model_validate(run["request"])
        if run["digest"] != request.fingerprint().removeprefix("sha256:"):
            raise CapabilityError("Formal test request changed")
        return owner, run, request

    async def accept(self, request, *, caller):
        run_id = str(request.run_id)
        async with self.repo.transaction():
            await self.repo.lock_formal_execution(run_id)
            prior = await self.repo.get_test_run(run_id, for_update=True)
            if prior:
                await self.load(request.owner_subject, run_id, caller)
                if prior["request"] != request.model_dump(mode="json", by_alias=True):
                    raise IdempotencyConflict(
                        "Execution identity was used with other content"
                    )
            else:
                owner = await self.formal._owner(request.owner_subject)
                envs = await self.repo.list_environments(owner_actor_id=owner)
                if not any(
                    str(e["id"]) == request.runner.environment_id
                    and request.runner.workspace_root in (e.get("roots") or [])
                    for e in envs
                ):
                    raise CapabilityError(
                        "The execution environment or workspace is not the author's"
                    )
                if (
                    not isinstance(request.draft.definition.procedure, Orchestration)
                    and len(request.draft.definition.access_requirements) != 1
                ):
                    raise CapabilityError(
                        "Direct MCP execution currently requires one exact access requirement"
                    )
                if Decimal(request.execution.budget.limit) < Decimal("0.000001") * len(
                    expanded(request)
                ):
                    raise CapabilityError(
                        "Budget is too small for the number of test repetitions"
                    )
                admission = await self.formal.validate(
                    request.admission_request(), caller=caller
                )
                if (
                    admission["status"] != "pending"
                    or "source" in admission["missing_checks"]
                ):
                    raise CapabilityError(
                        "Execution requires a licensed source and a pending admission"
                    )
                if isinstance(request.draft.definition.procedure, Orchestration):
                    await resolve_plan(
                        self.capabilities, owner, request.draft, request.method_id
                    )
                else:
                    await self.reviewed_policy(owner, request)
                await self.repo.insert_test_run(
                    dict(
                        run_id=run_id,
                        caller=caller,
                        owner_subject=request.owner_subject,
                        digest=request.fingerprint().removeprefix("sha256:"),
                        request=request.model_dump(mode="json", by_alias=True),
                        budget_limit=Decimal(request.execution.budget.limit),
                        timeout_seconds=request.execution.timeout_seconds,
                        max_wait_seconds=request.execution.max_wait_seconds,
                    ),
                    [
                        dict(case_id=case_key(c.case_id, n), position=i)
                        for i, (c, n) in enumerate(expanded(request))
                    ],
                )
        return await self.status(request.owner_subject, run_id, caller=caller)

    async def status(self, subject, run_id, *, caller):
        owner, run, request = await self.load(subject, run_id, caller)
        admission = await self.formal.validation(subject, run_id, caller=caller)
        rows = {r["case_id"]: r for r in await self.repo.list_test_cases(run_id)}
        cases, actions = [], []
        for case, n in expanded(request):
            row = rows[case_key(case.case_id, n)]
            result = row.get("result") or initial_case(case, n, row)
            if not row.get("result") and row.get("task_id"):
                task = await self.repo.get_task(str(row["task_id"]))
                attempts = await self.repo.list_attempts(str(task["id"]))
                result = {
                    **result,
                    "status": "waiting"
                    if task["state"] == TaskState.WAITING
                    else "running",
                }
                if attempts:
                    attempt = attempts[-1]
                    result["attempt_id"] = str(attempt["id"])
                    call_id = (attempt.get("refs") or {}).get("capability_call_id")
                    result["call_id"] = call_id
                    if call_id and run["status"] in ACTIVE:
                        call = await self.repo.capability_call(owner, call_id)
                        if call["status"] == "awaiting_approval" and call.get(
                            "interaction_id"
                        ):
                            interaction = await self.repo.get_interaction(
                                str(call["interaction_id"])
                            )
                            if (
                                interaction["status"] == "pending"
                                and interaction["expires_at"] > self.now()
                                and task["state"] not in TASK_TERMINAL
                            ):
                                prompt = interaction["prompt"]
                                actions.append(
                                    dict(
                                        call_id=call_id,
                                        case_id=case.case_id,
                                        repetition=n,
                                        interaction_id=str(interaction["id"]),
                                        token=call["detail"]["approval_token"],
                                        subject_digest=call["digest"],
                                        expires_at=interaction["expires_at"],
                                        title=prompt["title"],
                                        question=prompt["question"],
                                        subject=prompt.get("subject") or {},
                                    )
                                )
            cases.append(result)
        state = {"accepted": "pending", "error": "failed", "timeout": "failed"}.get(
            run["status"], run["status"]
        )
        if state in ("pending", "running", "waiting") and (
            actions or run.get("blocked_reason")
        ):
            state = "waiting"
        return FormalMethodExecutionResult.model_validate(
            dict(
                contract=FORMAL_RUNTIME,
                run_id=run_id,
                request_id=request.request_id,
                method_id=request.method_id,
                request_digest=request.fingerprint(),
                suite=request.execution.suite.reference(),
                status=state,
                admission=admission,
                cases=cases,
                spent=str(await self.repo.test_run_spent(run_id)),
                currency=request.execution.budget.currency,
                actions=actions,
            )
        ).model_dump(mode="json", by_alias=True)

    async def decide(self, subject, run_id, call_id, body, *, caller):
        owner, run, request = await self.load(subject, run_id, caller)
        if body.owner_subject != subject or run["status"] not in ACTIVE:
            raise Forbidden("This execution cannot accept an action decision")
        row = next(
            (
                r
                for r in await self.repo.list_test_cases(run_id)
                if r["case_id"] == case_key(body.case_id, body.repetition)
            ),
            None,
        )
        call = await self.repo.capability_call(owner, call_id)
        if (
            not row
            or not row.get("task_id")
            or str(call.get("task_id")) != str(row["task_id"])
            or not (
                call["detail"].get("formal_host") or call["detail"].get("formal_probe")
            )
        ):
            raise Forbidden("Action does not belong to this execution case")
        await self.capabilities.decide(owner, call_id, body)
        return await self.status(subject, run_id, caller=caller)

    async def cancel(self, subject, run_id, *, caller):
        async with self.repo.transaction():
            await self.repo.lock_formal_execution(run_id)
            owner, run, request = await self.load(subject, run_id, caller, lock=True)
            if run["status"] in ACTIVE:
                for row in await self.repo.list_test_cases(run_id):
                    if row.get("task_id"):
                        await cancel_task(
                            self.repo, str(row["task_id"]), owner_actor_id=owner
                        )
                await self.finish(
                    run,
                    request,
                    forced="cancelled",
                    detail="Cancelled by the author; dispatched external effects are not rolled back",
                )
        return await self.status(subject, run_id, caller=caller)

    async def record_case(self, run, request, case, n, row):
        task = await self.repo.get_task(str(row["task_id"]), for_update=True)
        if task["state"] in (TaskState.CANCELLED, TaskState.FAILED):
            from app.application.workbench.formal_recursive_runtime import (
                settle_ancestors,
            )

            try:
                snapshot = snapshot_for_task(task)
            except (ValueError, TypeError):
                # Invalid snapshots remain unqualified evidence; do not infer a
                # call tree or fabricate closure facts from a corrupt definition.
                snapshot = None
            if snapshot is not None and recursive_plan(snapshot):
                await settle_ancestors(
                    self.repo,
                    task,
                    snapshot,
                    outcome="cancelled"
                    if task["state"] == TaskState.CANCELLED
                    else "failed",
                    detail="Root task terminated before the complete tree returned",
                )
        attempts = await self.repo.list_attempts(str(task["id"]))
        attempt = attempts[-1] if attempts else None
        refs = (attempt or {}).get("refs") or {}
        actual: dict[str, Any] = refs.get("formal_result") or {}
        if not actual and refs.get("capability_call_id"):
            call = await self.repo.capability_call(
                str(task["owner_actor_id"]), refs["capability_call_id"]
            )
            if call["status"] in ("dispatching", "unknown", "succeeded"):
                actual = {
                    "outcome": "unknown",
                    "detail": "Execution stopped before the external result was settled; consult the retained call. External effects are not rolled back.",
                }
        outcome = actual.get("outcome") or (
            "cancelled" if task["state"] == TaskState.CANCELLED else "failed"
        )
        if (
            isinstance(request.draft.definition.procedure, Orchestration)
            and outcome == "succeeded"
        ):
            exports = refs.get("formal_exports")
            if task["state"] != TaskState.SUCCEEDED or not exports:
                outcome = "failed"
                actual = {"detail": "The expert did not complete its declared exports"}
            else:
                actual = {**actual, **exports}
        assertions = (
            evaluate_assertions(case, actual.get("outputs") or {})
            if outcome == "succeeded"
            else ()
        )
        passed = outcome == case.expected.outcome and (
            all(a.passed for a in assertions)
            if outcome == "succeeded"
            else actual.get("rejection_code") == case.expected.rejection_code
        )
        result = {
            **initial_case(case, n, row),
            "status": "passed"
            if passed
            else "cancelled"
            if outcome == "cancelled"
            else "failed",
            "outcome": outcome,
            "rejection_code": actual.get("rejection_code"),
            "attempt_id": str(attempt["id"]) if attempt else None,
            "call_id": refs.get("capability_call_id"),
            "assertions": [a.model_dump(mode="json") for a in assertions],
            "detail": actual.get("detail")
            or (task.get("rejection") or {}).get("message")
            or "",
        }
        if case.caller == "model" and refs.get("capability_call_id"):
            call = await self.repo.capability_call(
                str(task["owner_actor_id"]), refs["capability_call_id"]
            )
            invocation = call["detail"].get("invocation") or {}
            if all(
                invocation.get(k) for k in ("thread_id", "turn_id", "call_id")
            ) and call.get("parent_call_id"):
                result["model_trace"] = {
                    "thread_id": invocation["thread_id"],
                    "turn_id": invocation["turn_id"],
                    "tool_call_id": invocation["call_id"],
                    "method_call_id": str(call["parent_call_id"]),
                }
        result = FormalExecutionCaseResult.model_validate(result).model_dump(
            mode="json"
        )
        await self.repo.update_test_case(
            str(run["run_id"]),
            row["case_id"],
            result=result,
            outputs=actual.get("outputs") or {},
        )

    async def finish(self, run, request, *, forced=None, detail=""):
        """Caller serializes this run. Retained evidence is sealed only once."""
        run_id, owner = (
            str(run["run_id"]),
            await self.formal._owner(request.owner_subject),
        )
        rows = {r["case_id"]: r for r in await self.repo.list_test_cases(run_id)}
        results, evidence_cases = [], []
        for case, n in expanded(request):
            row = rows[case_key(case.case_id, n)]
            if not row.get("result"):
                if row.get("task_id"):
                    task = await self.repo.get_task(str(row["task_id"]))
                    if forced and task["state"] not in TASK_TERMINAL:
                        await cancel_task(
                            self.repo, str(task["id"]), owner_actor_id=owner
                        )
                    await self.record_case(run, request, case, n, row)
                    row = next(
                        r
                        for r in await self.repo.list_test_cases(run_id)
                        if r["case_id"] == row["case_id"]
                    )
                else:
                    result = {
                        **initial_case(case, n),
                        "status": "cancelled" if forced == "cancelled" else "failed",
                        "outcome": "cancelled" if forced == "cancelled" else "failed",
                        "detail": detail,
                    }
                    await self.repo.update_test_case(
                        run_id, row["case_id"], result=result
                    )
                    row["result"] = result
            result = row["result"]
            results.append(result)
            evidence_cases.append(
                await collect_case_evidence(self.repo, request, case, n, result)
            )
        thresholds = all(
            sum(r["status"] == "passed" for r in results if r["case_id"] == c.case_id)
            >= c.minimum_passes
            for c in request.execution.suite.cases
        )
        known = all(
            r["outcome"] not in ("pending", "unknown", "cancelled") for r in results
        )
        governed = all(e["validated"] for e in evidence_cases)
        leaves = ()
        try:
            licensed = await self.formal._source(
                owner, request.owner_subject, request.draft.source
            )
            if isinstance(request.draft.definition.procedure, Orchestration):
                leaves = await resolve_plan(
                    self.capabilities, owner, request.draft, request.method_id
                )
                policies = {}
                for leaf in leaves:
                    if leaf.draft.source is None:
                        continue
                    policy = await self.reviewed_policy(owner, leaf)
                    policies[fingerprint(leaf.reference.model_dump(mode="json"))] = (
                        policy.policy_digest
                    )
                licensed = licensed and all(
                    isinstance(step.get("governance"), dict)
                    and isinstance(step["governance"].get("operation"), dict)
                    and step["governance"]["operation"].get("policy_digest")
                    == policies.get(fingerprint(step.get("reference")))
                    for evidence_case in evidence_cases
                    for step in evidence_case.get("steps", [])
                )
            else:
                policy = await self.reviewed_policy(owner, request)
                licensed = licensed and all(
                    not e["governance"]
                    or e["governance"]["operation"]["policy_digest"]
                    == policy.policy_digest
                    for e in evidence_cases
                )
        except WorkbenchError:
            licensed = False
        passed = (
            not forced
            and thresholds
            and known
            and governed
            and licensed
            and all(
                any(
                    r["status"] == "passed"
                    and r["outcome"] == "succeeded"
                    and r.get("caller", "host") == caller
                    for r in results
                )
                for caller in request.draft.allowed_callers
            )
        )
        state = (
            "passed" if passed else "cancelled" if forced == "cancelled" else "failed"
        )
        from app.application.workbench.script_budget import task_script_cost

        script_cases = (
            any(
                leaf.draft.source and leaf.draft.source.kind == "skill_script"
                for leaf in leaves
            )
            if isinstance(request.draft.definition.procedure, Orchestration)
            else bool(
                request.draft.source and request.draft.source.kind == "skill_script"
            )
        )
        script_total = Decimal(0)
        for row in rows.values():
            if row.get("task_id"):
                script_total += await task_script_cost(self.repo, row["task_id"])
        script_cases = script_cases or bool(script_total)
        evidence = dict(
            contract=FORMAL_RUNTIME,
            request_digest=request.fingerprint(),
            suite=request.execution.suite.reference().model_dump(mode="json"),
            definition_digest=request.draft.definition.digest(),
            adapter="knowbench/formal-orchestration-tree-host/v1"
            if any(
                e.get("adapter") == "knowbench/formal-orchestration-tree-host/v1"
                for e in evidence_cases
            )
            else "knowbench/formal-orchestration-host/v1"
            if isinstance(request.draft.definition.procedure, Orchestration)
            else "knowbench/formal-mcp-caller-probe/v1"
            if "model" in request.draft.allowed_callers
            else "knowbench/formal-script-host/v1"
            if script_cases
            else "knowbench/formal-mcp-host/v1",
            cases=evidence_cases,
            model_cost=str(await self.repo.test_run_spent(run_id) - script_total),
            **({"script_cost": str(script_total)} if script_cases else {}),
            external_provider_billing=(
                "metered_scripts" if script_cases else "reviewed_no_incremental_fee"
            )
            if licensed
            else "unverified",
            sealed_at=self.now().isoformat(),
        )
        if "model" in request.draft.allowed_callers:
            evidence["validated_callers"] = (
                sorted(request.draft.allowed_callers) if passed else []
            )
        review = (
            self.formal.business_review(
                request.owner_subject,
                request.method_id,
                request.draft,
                request.execution.suite.digest,
            )
            if passed and all(r["status"] == "passed" for r in results)
            else None
        )
        if review is not None:
            evidence["business_review_digest"] = review.fingerprint
        receipt = None
        if passed:
            receipt = FormalMethodValidationReceipt.model_validate(
                {
                    **request.admission_request().model_dump(
                        mode="json", by_alias=True, exclude={"draft"}
                    ),
                    "definition_digest": request.draft.definition.digest(),
                    "source_digest": request.draft.source.fingerprint()
                    if request.draft.source
                    else None,
                    "checks": sorted(
                        GOVERNANCE_CHECKS | ({"business"} if review else set())
                    ),
                    "uncovered": []
                    if review
                    else [
                        {
                            "scenario": "Real-service business effectiveness beyond the supplied assertions",
                            "reason": "This run checks typed outputs and governance, not every natural-language business rule or supplier scenario",
                            "scope": (
                                "Private use within the tested definition, sources and approved container tariffs"
                                if script_cases
                                else "Private use within the tested definition and source; only adapters reviewed as having no incremental provider fees are admitted"
                            ),
                        }
                    ],
                    "completed_at": self.now(),
                    "execution": dict(
                        **(
                            {"validated_callers": sorted(request.draft.allowed_callers)}
                            if "model" in request.draft.allowed_callers
                            else {}
                        ),
                        contract=FORMAL_RUNTIME,
                        request_digest=request.fingerprint(),
                        suite_id=request.execution.suite.suite_id,
                        suite_version=request.execution.suite.version,
                        suite_digest=request.execution.suite.digest,
                        evidence_digest=fingerprint(evidence),
                    ),
                }
            )
        admission = FormalMethodValidationResult(
            contract=CONTRACT,
            run_id=request.run_id,
            status=state,
            receipt=receipt,
            missing_checks=()
            if passed
            else tuple(
                cast(Check, check)
                for check in sorted(GOVERNANCE_CHECKS - {"definition", "dependencies"})
            ),
            details=(
                "Test results and the declared validation scope are recorded in the receipt"
                if passed
                else detail
                or (
                    "Execution did not satisfy its test thresholds and required governance evidence; "
                    + "; ".join(
                        f"{e['result']['case_id']}:{','.join(e['errors'])}"
                        for e in evidence_cases
                        if e["errors"]
                    )
                )[:2000],
            ),
        )
        await self.repo.seal_formal_execution(
            owner, run_id, admission.model_dump(mode="json"), evidence
        )
        await self.repo.update_test_run(
            run_id,
            status=state,
            finished_at=self.now(),
            cleanup_after=self.now() + timedelta(days=7),
        )
        await self.repo.reconcile_test_run_spent(run_id)


async def advance_formal(conductor, run_id):
    """Called by the existing conductor, using its account and queue lifecycle."""
    factory = conductor.formal_capabilities
    if factory is None:
        return
    task_id = None
    async with conductor.stores() as repo, repo.transaction():
        await repo.lock_formal_execution(run_id)
        run = await repo.get_test_run(run_id, for_update=True)
        if not run or run["status"] not in ACTIVE:
            return
        svc = FormalExecutions(repo, factory(repo), now=conductor.now)
        request = FormalMethodExecutionRequest.model_validate(run["request"])
        now = conductor.now()
        if (
            (now - run["created_at"]).total_seconds()
            > request.execution.timeout_seconds + request.execution.max_wait_seconds
        ):
            await svc.finish(
                run,
                request,
                forced="failed",
                detail="Execution and waiting deadline exceeded",
            )
            return
        rows = await repo.list_test_cases(run_id)
        current = next((r for r in rows if not r.get("result")), None)
        task = None
        if current and current.get("task_id"):
            task_id = str(current["task_id"])
            task = await repo.get_task(task_id)
            waiting = task["state"] == TaskState.WAITING or bool(
                (task.get("original_input") or {})
                .get("client_context", {})
                .get("queued_behind")
            )
            attempts = await repo.list_attempts(task_id)
            refs = (attempts[-1].get("refs") or {}) if attempts else {}
            call_id = refs.get("capability_call_id")
            elapsed_wait = int(current["waited_seconds"] or 0)
            if (
                isinstance(request.draft.definition.procedure, Orchestration)
                and call_id
            ):
                # Sum each real approval interval once across completed steps.
                # Replacing the current call must not erase earlier waiting time.
                total_wait = timedelta()
                seen_calls = set()
                for prior in attempts:
                    prior_call = (prior.get("refs") or {}).get("capability_call_id")
                    if not prior_call or prior_call in seen_calls:
                        continue
                    seen_calls.add(prior_call)
                    observed = await repo.capability_call(
                        str(task["owner_actor_id"]), prior_call
                    )
                    if observed.get("interaction_id"):
                        interaction = await repo.get_interaction(
                            str(observed["interaction_id"])
                        )
                        until = min(now, interaction["consumed_at"] or now)
                        total_wait += max(
                            timedelta(), until - interaction["created_at"]
                        )
                # Keep sub-second intervals until after the complete sum. Taking
                # int per step lets many short waits evade the run's wait limit.
                elapsed_wait = int(total_wait.total_seconds())
            elif call_id:
                call = await repo.capability_call(str(task["owner_actor_id"]), call_id)
                if call.get("interaction_id"):
                    interaction = await repo.get_interaction(
                        str(call["interaction_id"])
                    )
                    until = min(now, interaction["consumed_at"] or now)
                    elapsed_wait = max(
                        0, int((until - interaction["created_at"]).total_seconds())
                    )
            elif waiting and current["waiting_since"] is not None:
                elapsed_wait += max(
                    0, int((now - current["waiting_since"]).total_seconds())
                )
            delta = elapsed_wait - int(current["waited_seconds"] or 0)
            await repo.update_test_run(
                run_id, waited_seconds=run["waited_seconds"] + delta
            )
            await repo.update_test_case(
                run_id,
                current["case_id"],
                waited_seconds=elapsed_wait,
                waiting_since=now if waiting else None,
            )
            run = await repo.get_test_run(run_id)
        waited = int(run["waited_seconds"] or 0)
        expired = (
            waited > request.execution.max_wait_seconds
            or (
                run["started_at"] is None
                and (now - run["created_at"]).total_seconds()
                > request.execution.max_wait_seconds
            )
            or (
                run["started_at"] is not None
                and (now - run["started_at"]).total_seconds() - waited
                > request.execution.timeout_seconds
            )
        )
        if expired:
            await svc.finish(
                run,
                request,
                forced="failed",
                detail="Execution or approval waiting deadline exceeded",
            )
            return
        if current is None:
            await svc.finish(run, request)
            return
        case, repetition = expanded(request)[current["position"]]
        if task and task["state"] in TASK_TERMINAL:
            current = next(
                r
                for r in await repo.list_test_cases(run_id)
                if r["case_id"] == current["case_id"]
            )
            await svc.record_case(run, request, case, repetition, current)
            return
        try:
            account = await conductor._account(repo, "formal-tests/" + run_id, request)
            project = await repo.get_project(account.project_id)
            if project.get("purpose") != "knowlab-test":
                raise CapabilityError("A formal test cannot reuse a non-test project")
        except WorkbenchError as exc:
            await svc.finish(run, request, forced="failed", detail=str(exc)[:1000])
            return
        if not account.online or await repo.current_sandbox(account.actor_id) is None:
            await repo.update_test_run(
                run_id, blocked_reason="execution_environment_unavailable"
            )
            return
        if run["started_at"] is None:
            await repo.update_test_run(
                run_id, status="running", started_at=now, blocked_reason=None
            )
        elif run.get("blocked_reason"):
            await repo.update_test_run(run_id, blocked_reason=None)
        if current.get("task_id"):
            task_id = str(current["task_id"])
        else:
            dependencies = ()
            if isinstance(request.draft.definition.procedure, Orchestration):
                try:
                    dependencies = await resolve_plan(
                        factory(repo),
                        account.actor_id,
                        request.draft,
                        request.method_id,
                    )
                except WorkbenchError as exc:
                    await svc.finish(
                        run, request, forced="failed", detail=str(exc)[:1000]
                    )
                    return
            session = await SessionService(repo).start(
                owner_actor_id=account.actor_id,
                kind="expert",
                project_id=account.project_id,
                title=f"方法验证 {case.case_id} · {repetition}",
                origin="knowlab-test",
            )
            snapshot = FormalTaskSnapshot(
                runtime_contract=FORMAL_RUNTIME,
                run_id=request.run_id,
                case_id=case.case_id,
                repetition=repetition,
                request_digest=request.fingerprint(),
                method_id=request.method_id,
                draft=request.draft,
                arguments=case.input.values,
                dependencies=dependencies,
                definition_digest=request.draft.definition.digest(),
                caller=case.caller,
                model_prompt=case.input.model_prompt,
                model_probe=request.execution.model_probe
                if case.caller == "model"
                else None,
            )
            per_case = (Decimal(request.execution.budget.limit) / len(rows)).quantize(
                Decimal("0.000001"), rounding=ROUND_DOWN
            )
            if per_case <= 0:
                raise CapabilityError(
                    "Budget is too small for the number of test repetitions"
                )
            made = await Ledger(repo).handover(
                HandoverRequest(
                    idempotency_key=f"formal-{run_id}-{current['position']}",
                    session_id=session["session_id"],
                    profile_id=f"formal-test:{request.run_id}",
                    original_input=case.execution_input(),
                    budget_limit=per_case,
                    budget_currency=request.execution.budget.currency,
                    client_context={
                        "entry": "formal-method-execution",
                        "run_id": run_id,
                    },
                ),
                owner_actor_id=account.actor_id,
                snapshot=PinnedFormalMethod(snapshot, str(request.revision)),
            )
            task_id = made["task_id"]
            await repo.update_test_case(
                run_id,
                current["case_id"],
                task_id=task_id,
                session_id=session["session_id"],
                project_id=account.project_id,
            )
            if case.caller == "model" and not made.get("queued_behind"):
                talk = await repo.get_session(session["session_id"])
                await repo.enqueue_command(
                    session_id=session["session_id"],
                    sandbox_id=str(talk["sandbox_id"]),
                    kind="task.drive",
                    payload={"task_id": task_id},
                )
    if task_id and case.caller == "host":
        await FormalExecution(conductor.stores, factory).drive(task_id)
