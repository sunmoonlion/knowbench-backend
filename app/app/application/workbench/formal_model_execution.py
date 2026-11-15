"""One real model turn proposing one approved direct candidate invocation."""

import json
from decimal import Decimal

from app.application.workbench.ledger import Ledger, _turn_open
from app.application.workbench.turn import TurnResult
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.formal_execution import (
    direct_procedure,
    snapshot_for_task,
    structured_outputs,
)
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
    bounded_json,
)
from app.domain.workbench.formal_model import PROBE_TOOL, validate_model_call
from app.domain.workbench.states import (
    ATTEMPT_TERMINAL,
    TASK_TERMINAL,
    AttemptState,
    TaskState,
)


class FormalModelExecution:
    def __init__(self, stores, driver, capabilities):
        self.stores, self.driver, self.capabilities = stores, driver, capabilities

    async def drive(self, task_id):
        async with self.stores() as repo, repo.transaction():
            task = await repo.get_task(task_id, for_update=True)
            if task["state"] in TASK_TERMINAL:
                return task["state"]
            led = Ledger(repo)
            if task.get("cancel_requested_at"):
                return (
                    await led.request_cancel(
                        task_id, owner_actor_id=str(task["owner_actor_id"])
                    )
                )["state"]
            if (
                (task.get("original_input") or {})
                .get("client_context", {})
                .get("queued_behind")
            ):
                return task["state"]
            snapshot = snapshot_for_task(task)
            if snapshot.caller != "model":
                raise CapabilityError("A model probe requires an explicit model test")
            direct_procedure(snapshot.draft, caller="model")
            run = await repo.test_run_for_task(task_id)
            if not run:
                raise CapabilityError("Model probe has no execution request")
            request = FormalMethodExecutionRequest.model_validate(run["request"])
            if request.fingerprint() != snapshot.request_digest:
                raise CapabilityError("Model probe execution request changed")
            if task["state"] == TaskState.RECEIVED:
                await led.transition(task_id, TaskState.VALIDATING)
                task = await repo.get_task(task_id)
            if task["state"] == TaskState.VALIDATING:
                await led.transition(task_id, TaskState.QUEUED)
                task = await repo.get_task(task_id)
            if task.get("active_attempt_id") or await repo.list_attempts(task_id):
                # The durable claim precedes turn/start; neither timeout nor a
                # missing returned turn id permits another model request.
                return task["state"]
            if task["state"] != TaskState.QUEUED or not task.get("thread_id"):
                return task["state"]
            budget = task["budget"]
            remaining = (
                Decimal(str(budget["limit"]))
                - Decimal(str(budget["used"]))
                - Decimal(str(budget["reserved"]))
            )
            if remaining <= 0:
                return (
                    await led.transition(
                        task_id, TaskState.FAILED, reason={"code": "budget_limit"}
                    )
                )["state"]
            opened = await led.open_attempt(
                task_id,
                step_id="direct",
                step_version=snapshot.definition_digest.removeprefix("sha256:"),
                reserve=remaining,
                meta={
                    "kind": "formal_method",
                    "snapshot_digest": task["pack_digest"],
                    "slot": "direct",
                    "formal_model_probe": {
                        "state": "claimed",
                        "snapshot_digest": task["pack_digest"],
                        "tool_name": PROBE_TOOL,
                    },
                },
            )
            attempt_id = opened["attempt_id"]
            await led.attempt_transition(attempt_id, AttemptState.RUNNING)
            thread_id = task["thread_id"]
        text = (
            "Call the candidate method exactly once with the supplied arguments. Await the owner's approval and report its observed result.\n"
            + json.dumps(
                {
                    "instruction": snapshot.model_prompt,
                    "tool": PROBE_TOOL,
                    "arguments": snapshot.arguments,
                },
                ensure_ascii=False,
            )
        )
        try:
            result = await self.driver.run_turn(
                thread_id, text, timeout=request.execution.timeout_seconds
            )
        except Exception as exc:
            result = TurnResult(
                None, None, {}, error=type(exc).__name__, platform_failure=True
            )
        return await self.finish(task_id, attempt_id, result)

    async def recover(self, task_id, *, stopped):
        async with self.stores() as repo:
            task = await repo.get_task(task_id)
            if not task.get("active_attempt_id"):
                return task["state"]
            attempt = await repo.get_attempt(str(task["active_attempt_id"]))
        return await self.finish(
            task_id,
            str(attempt["id"]),
            TurnResult(
                (attempt.get("refs") or {}).get("runtime_turn_id"),
                None,
                {},
                error="Model probe runner restarted; execution is not replayed",
                tool_stop_unconfirmed=not stopped,
            ),
            recovered=True,
            stop_confirmed=stopped,
        )

    async def finish(
        self, task_id, attempt_id, result, *, recovered=False, stop_confirmed=None
    ):
        known = result.not_started or (
            result.usage_known
            and result.completion_confirmed
            and result.cost is not None
            and not result.interrupted
            and not result.timed_out
            and not result.error
            and not recovered
        )
        stopped = (
            stop_confirmed
            if stop_confirmed is not None
            else result.not_started or result.completion_confirmed
        )
        async with self.stores() as repo, repo.transaction():
            task = await repo.get_task(task_id, for_update=True)
            attempt = await repo.get_attempt(attempt_id, for_update=True)
            refs = attempt.get("refs") or {}
            probe = {
                **refs.get("formal_model_probe", {}),
                "state": "finished" if known and stopped else "unknown",
                "turn_id": result.turn_id,
                "usage_known": known,
                "cost": str(result.cost) if result.cost is not None else None,
                "tokens": result.tokens,
                "not_started": result.not_started,
                "stop_confirmed": stopped,
                "error": result.error,
            }
            led = Ledger(repo)
            if attempt["status"] in ATTEMPT_TERMINAL:
                await repo.update_attempt(
                    attempt_id, refs={**refs, "formal_model_probe": probe}
                )
                if known:
                    await led.record_late_cost(
                        task_id,
                        attempt_id=attempt_id,
                        turn_id=result.turn_id,
                        consumed=result.cost or Decimal("0"),
                        tokens=result.tokens,
                        not_started=result.not_started,
                    )
                elif result.turn_id:
                    await led.observe_pending_usage(
                        task_id,
                        attempt_id=attempt_id,
                        turn_id=result.turn_id,
                        estimated_cost=result.cost,
                        tokens=result.tokens,
                    )
                if stopped:
                    await led.release_turn(task_id, attempt_id=attempt_id)
                return task["state"]
            outcome, code, outputs = "failed", None, {}
            detail = (
                result.error or "Model probe did not produce one verified method result"
            )
            call_id = refs.get("capability_call_id")
            if call_id:
                call = await repo.capability_call(
                    str(task["owner_actor_id"]), call_id, lock=True
                )
                validate_model_call(task, attempt, call)
                root = await repo.call_by_key(
                    str(task["owner_actor_id"]),
                    str(task["session_id"]),
                    "task:" + task_id,
                )
                if not root or root["id"] != call["parent_call_id"]:
                    raise CapabilityError("Model result has another logical parent")
                if (
                    known
                    and stopped
                    and not result.error
                    and not result.platform_failure
                ):
                    if call["status"] == "succeeded":
                        try:
                            outputs = structured_outputs(
                                snapshot_for_task(task).draft,
                                call["detail"].get("result") or {},
                            )
                            bounded_json(outputs)
                            outcome, detail = "succeeded", ""
                        except ValueError as exc:
                            detail = str(exc)
                    elif call["status"] in ("declined", "expired"):
                        outcome, code, detail = (
                            "rejected",
                            "approval_denied",
                            "Candidate operation was not approved",
                        )
                if call["status"] in ("dispatching", "unknown"):
                    outcome = "unknown"
            if not known or not stopped:
                outcome = "unknown"
            if task["state"] == TaskState.WAITING:
                await repo.cancel_pending_interactions(
                    task_id=task_id, reason="model probe turn ended"
                )
                await led.transition(
                    task_id, TaskState.QUEUED, active_interaction_id=None
                )
                task = await repo.get_task(task_id)
            if task["state"] == TaskState.QUEUED:
                await led.transition(task_id, TaskState.RUNNING)
            if attempt["status"] == AttemptState.SUSPENDED:
                await led.attempt_transition(attempt_id, AttemptState.RUNNING)
            await repo.update_attempt(
                attempt_id,
                refs={
                    **refs,
                    "formal_model_probe": probe,
                    "formal_result": {
                        "outcome": outcome,
                        "rejection_code": code,
                        "detail": detail[:2000],
                        "outputs": outputs,
                        "outputs_digest": digest(outputs),
                    },
                },
            )
            await led.attempt_transition(
                attempt_id,
                AttemptState.COMPLETED
                if outcome == "succeeded"
                else AttemptState.FAILED,
                consumed=(result.cost or Decimal("0")) if known else Decimal("0"),
                usage_pending=not known,
                pending_cost=result.cost if not known else None,
                tokens=result.tokens,
                turn_id=result.turn_id,
                failure_code=code or (None if outcome == "succeeded" else outcome),
                retryable=False,
            )
            return (
                await led.transition(
                    task_id,
                    TaskState.SUCCEEDED if outcome == "succeeded" else TaskState.FAILED,
                    reason={"code": code or outcome, "message": detail[:500]},
                    original_input=_turn_open(task, attempt_id)
                    if not stopped
                    else None,
                )
            )["state"]
