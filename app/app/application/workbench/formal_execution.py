"""Host-only direct dispatch within the existing Task/Attempt/Interaction ledger.

The adapter runs only after an owner's exact action approval. This driver never
starts a model turn or interprets an old expert pack, and never retries an
uncertain logical action under a new Attempt.
"""

from app.application.workbench.ledger import Ledger
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.formal_execution import (
    direct_procedure,
    snapshot_for_task,
    structured_outputs,
    validate_values,
)
from app.domain.workbench.formal_host import validate_host_call
from app.domain.workbench.states import (
    ATTEMPT_TERMINAL,
    TASK_TERMINAL,
    AttemptState,
    TaskState,
)


class FormalExecution:
    def __init__(self, stores, capabilities, *, instructions=None):
        self.instructions = instructions
        self.stores, self.capabilities = stores, capabilities

    async def drive(self, task_id: str) -> str:
        async with self.stores() as repo:
            task = await repo.get_task(task_id)
            definition = (
                (task.get("pack_snapshot") or {}).get("draft", {}).get("definition", {})
            )
            procedure = definition.get("procedure", {})
        if procedure.get("kind") == "orchestration":
            from app.application.workbench.formal_orchestration_execution import (
                FormalOrchestrationExecution,
            )

            return await FormalOrchestrationExecution(
                self.stores, self.capabilities
            ).drive(task_id)
        if (procedure.get("implementation") or {}).get("kind") == "skill_instruction":
            # The conductor observes the Task. Only the runner owns the dedicated execution connection.
            if self.instructions is not None:
                await self.instructions.drive(task_id)
                async with self.stores() as repo:
                    return (await repo.get_task(task_id))["state"]
            return task["state"]
        async with self.stores() as repo, repo.transaction():
            led = Ledger(repo)
            task = await repo.get_task(task_id, for_update=True)
            if task["state"] in TASK_TERMINAL:
                return task["state"]
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
            if task["state"] == TaskState.RECEIVED:
                await led.transition(task_id, TaskState.VALIDATING)
                task = await repo.get_task(task_id)
            if task["state"] == TaskState.VALIDATING:
                try:
                    snapshot = snapshot_for_task(task)
                    direct_procedure(snapshot.draft)
                except ValueError as exc:
                    return (
                        await led.transition(
                            task_id,
                            TaskState.REJECTED,
                            reason={
                                "code": "unsupported_formal_execution",
                                "message": str(exc),
                            },
                        )
                    )["state"]
                await led.transition(task_id, TaskState.QUEUED)
                task = await repo.get_task(task_id)
            snapshot = snapshot_for_task(task)
            active = task.get("active_attempt_id")
            if active is None:
                # A prior terminal Attempt cannot be replayed as a new operation.
                attempts = await repo.list_attempts(task_id)
                if attempts:
                    return (
                        await led.transition(
                            task_id,
                            TaskState.FAILED,
                            reason={"code": "formal_attempt_already_settled"},
                        )
                    )["state"]
                if task["state"] != TaskState.QUEUED:
                    return task["state"]
                opened = await led.open_attempt(
                    task_id,
                    step_id="direct",
                    step_version=snapshot.definition_digest.removeprefix("sha256:"),
                    meta={
                        "kind": "formal_method",
                        "snapshot_digest": task["pack_digest"],
                        "slot": "direct",
                    },
                )
                active = opened["attempt_id"]
                await led.attempt_transition(str(active), AttemptState.RUNNING)
            attempt = await repo.get_attempt(str(active), for_update=True)
            refs = attempt.get("refs") or {}
            if refs.get("snapshot_digest") != task["pack_digest"]:
                raise CapabilityError(
                    "Formal attempt differs from its immutable snapshot"
                )
            call_id = refs.get("capability_call_id")
            owner = str(task["owner_actor_id"])
        # No database transaction spans execution or adapter I/O.
        if call_id is None:
            try:
                validate_values(
                    direct_procedure(snapshot.draft).inputs, snapshot.arguments
                )
            except ValueError as exc:
                return await self._finish(
                    task_id, str(active), "rejected", "input_invalid", detail=str(exc)
                )
            if self.capabilities is None:
                return await self._finish(
                    task_id,
                    str(active),
                    "failed",
                    detail="Formal execution service is unavailable",
                )
            try:
                async with self.stores() as repo:
                    call = await self.capabilities(repo).request_host_call(
                        owner, task_id, str(active)
                    )
            except CapabilityError as exc:
                return await self._finish(
                    task_id,
                    str(active),
                    "rejected",
                    "authorization_denied",
                    detail=str(exc),
                )
            call_id = str(call["id"])
            async with self.stores() as repo, repo.transaction():
                await repo.get_task(task_id, for_update=True)
                attempt = await repo.get_attempt(str(active), for_update=True)
                await repo.update_attempt(
                    str(active),
                    refs={**(attempt.get("refs") or {}), "capability_call_id": call_id},
                )
        async with self.stores() as repo:
            await repo.recover_calls(owner, str(task["session_id"]))
            call = await repo.capability_call(owner, call_id)
            state = call["status"]
            task = await repo.get_task(task_id)
            attempt = await repo.get_attempt(str(active))
            validate_host_call(task, attempt, call)
            if state in ("awaiting_approval", "dispatching"):
                return task["state"]
            if task["state"] in TASK_TERMINAL:
                return task["state"]
        if state == "succeeded":
            try:
                outputs = structured_outputs(
                    snapshot.draft, call["detail"].get("result") or {}
                )
            except ValueError as exc:
                return await self._finish(
                    task_id, str(active), "failed", detail=str(exc), observed_call=call
                )
            return await self._finish(
                task_id, str(active), "succeeded", outputs=outputs, observed_call=call
            )
        if state in ("declined", "expired", "cancelled"):
            return await self._finish(
                task_id,
                str(active),
                "rejected",
                "approval_denied",
                detail=f"Action {state}",
                observed_call=call,
            )
        return await self._finish(
            task_id,
            str(active),
            "unknown" if state == "unknown" else "failed",
            detail=f"Action {state}; it was not replayed",
            observed_call=call,
        )

    async def _finish(
        self,
        task_id,
        attempt_id,
        outcome,
        rejection_code=None,
        *,
        outputs=None,
        detail="",
        observed_call=None,
    ):
        async with self.stores() as repo, repo.transaction():
            task = await repo.get_task(task_id, for_update=True)
            attempt = await repo.get_attempt(attempt_id, for_update=True)
            if task["state"] in TASK_TERMINAL or attempt["status"] in ATTEMPT_TERMINAL:
                return task["state"]
            if str(task.get("active_attempt_id")) != attempt_id:
                raise CapabilityError("Formal attempt is no longer active")
            linked = (attempt.get("refs") or {}).get("capability_call_id")
            if observed_call is not None:
                if linked != observed_call["id"]:
                    raise CapabilityError(
                        "Formal result no longer belongs to this Attempt"
                    )
                current_call = await repo.capability_call(
                    str(task["owner_actor_id"]), linked, lock=True
                )
                validate_host_call(task, attempt, current_call)
                if current_call["status"] != observed_call["status"] or digest(
                    current_call["detail"].get("result")
                ) != digest(observed_call["detail"].get("result")):
                    # A late adapter receipt may replace an uncertain observation.
                    # Re-read it on the next driver pass instead of settling stale facts.
                    return task["state"]
                root = await repo.call_by_key(
                    str(task["owner_actor_id"]),
                    str(task["session_id"]),
                    "task:" + task_id,
                )
                if not root or current_call["parent_call_id"] != root["id"]:
                    raise CapabilityError("Formal result has another logical parent")
            elif linked:
                # Another driver already created the durable action. Its receipt,
                # not an earlier pre-dispatch failure, owns the final outcome.
                return task["state"]
            led = Ledger(repo)
            if task["state"] == TaskState.WAITING:
                await repo.cancel_pending_interactions(
                    task_id=task_id, reason="formal action settled before an answer"
                )
                await led.transition(
                    task_id,
                    TaskState.QUEUED,
                    reason={"formal_action_settled": True},
                    active_interaction_id=None,
                )
                task = await repo.get_task(task_id)
            if task["state"] == TaskState.QUEUED:
                await led.transition(task_id, TaskState.RUNNING)
            if attempt["status"] == AttemptState.SUSPENDED:
                await led.attempt_transition(attempt_id, AttemptState.RUNNING)
            result = {
                "outcome": outcome,
                "rejection_code": rejection_code,
                "detail": detail[:2000],
                "outputs": outputs or {},
                "outputs_digest": digest(outputs or {}),
            }
            await repo.update_attempt(
                attempt_id,
                refs={**(attempt.get("refs") or {}), "formal_result": result},
            )
            await led.attempt_transition(
                attempt_id,
                AttemptState.COMPLETED
                if outcome == "succeeded"
                else AttemptState.FAILED,
                failure_code=rejection_code
                or (None if outcome == "succeeded" else outcome),
                retryable=False,
            )
            # Zero here is the observed absence of model turns, not a statement
            # about a remote provider's own billing.
            return (
                await led.transition(
                    task_id,
                    TaskState.SUCCEEDED if outcome == "succeeded" else TaskState.FAILED,
                    reason={"code": rejection_code or outcome, "message": detail[:500]},
                )
            )["state"]
