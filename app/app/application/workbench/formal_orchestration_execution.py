"""Deterministic formal steps on the existing Task/Attempt/Artifact ledger."""

import uuid

from app.application.workbench.ledger import Ledger
from app.application.workbench.method_calls import root_call
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.formal_execution import (
    dependency_for_step,
    orchestration_plan,
    snapshot_for_task,
    structured_outputs,
)
from app.domain.workbench.formal_host import validate_host_call
from app.domain.workbench.formal_method_execution_generated import bounded_json
from app.domain.workbench.formal_method_plan_generated import (
    bind_step_inputs,
    export_outputs,
    validate_plan_inputs,
    validate_step_outputs,
)
from app.domain.workbench.states import (
    ATTEMPT_TERMINAL,
    TASK_TERMINAL,
    AttemptState,
    TaskState,
)


def step_call_key(task_id, slot):
    return "formal-method-step:" + task_id + ":" + slot


def step_identity(task, step, index):
    return {
        "identity_basis": "formal_dependency",
        "task_id": str(task["id"]),
        "snapshot_digest": task["pack_digest"],
        "slot": step.id,
        "step_index": index,
        "reference": step.candidates[0].model_dump(mode="json"),
    }


async def accepted_outputs(repo, task, snapshot, before):
    """Read exact accepted artifact versions, never a newer artifact with the same name."""
    steps = orchestration_plan(snapshot)
    attempts = await repo.list_attempts(str(task["id"]))
    outputs, artifacts = {}, []
    for index, step in enumerate(steps[:before]):
        matches = [a for a in attempts if (a.get("refs") or {}).get("slot") == step.id]
        if len(matches) != 1:
            raise CapabilityError(
                "Formal dependency does not have one accepted Attempt"
            )
        attempt = matches[0]
        refs = attempt.get("refs") or {}
        result = refs.get("formal_result") or {}
        frozen = dependency_for_step(snapshot, step)
        versions = attempt.get("output_artifacts") or []
        if (
            attempt["status"] != AttemptState.COMPLETED
            or refs.get("kind") != "formal_orchestration_step"
            or refs.get("snapshot_digest") != task["pack_digest"]
            or refs.get("step_index") != index
            or refs.get("reference") != frozen.reference.model_dump(mode="json")
            or result.get("outcome") != "succeeded"
            or len(versions) != 1
        ):
            raise CapabilityError("Formal dependency is not an accepted step result")
        version = versions[0]
        artifact = await repo.get_artifact(
            task_id=str(task["id"]),
            name="formal-step:" + step.id,
            version=version["version"],
        )
        if (
            artifact is None
            or str(artifact["id"]) != version["id"]
            or str(artifact["attempt_id"]) != str(attempt["id"])
            or artifact["name"] != version["name"]
            or artifact["digest"] != version["digest"]
            or digest(artifact["content"]) != result.get("outputs_digest")
            or artifact["content"] != result.get("outputs")
        ):
            raise CapabilityError("Formal accepted artifact changed")
        value = validate_step_outputs(step, artifact["content"])
        bounded_json(value)
        outputs[step.id] = value
        artifacts.append({"step_id": step.id, **version})
    return outputs, artifacts


async def step_context(repo, task, attempt):
    """Resolve the active leaf from the frozen root, not from caller-controlled refs."""
    snapshot = snapshot_for_task(task)
    steps = orchestration_plan(snapshot)
    index = int(task["current_step"])
    if not 0 <= index < len(steps):
        raise CapabilityError("Formal step cursor is outside its plan")
    step = steps[index]
    frozen = dependency_for_step(snapshot, step)
    refs = attempt.get("refs") or {}
    if (
        str(attempt["task_id"]) != str(task["id"])
        or refs.get("kind") != "formal_orchestration_step"
        or refs.get("slot") != step.id
        or refs.get("step_index") != index
        or refs.get("snapshot_digest") != task["pack_digest"]
        or refs.get("reference") != frozen.reference.model_dump(mode="json")
        or attempt["step_id"] != step.id
        or attempt["step_version"] != digest(step_identity(task, step, index))
    ):
        raise CapabilityError("Formal step Attempt differs from its pinned plan")
    outputs, artifacts = await accepted_outputs(repo, task, snapshot, index)
    inputs = bind_step_inputs(step, snapshot.arguments, outputs)
    bindings = [a for a in artifacts if a["step_id"] in step.depends_on]
    if refs.get("input_bindings") != bindings or refs.get("arguments_digest") != digest(
        inputs
    ):
        raise CapabilityError("Formal step input artifact binding changed")
    return snapshot, step, frozen, inputs


class FormalOrchestrationExecution:
    def __init__(self, stores, capabilities):
        self.stores, self.capabilities = stores, capabilities

    async def drive(self, task_id):
        # Each pass advances at most the declared step count. I/O never runs
        # inside the Task transaction and a pending action returns to the caller.
        for _ in range(101):
            async with self.stores() as repo, repo.transaction():
                task = await repo.get_task(task_id, for_update=True)
                led = Ledger(repo)
                if task["state"] in TASK_TERMINAL:
                    return task["state"]
                if task.get("cancel_requested_at"):
                    return (
                        await led.request_cancel(
                            task_id, owner_actor_id=str(task["owner_actor_id"])
                        )
                    )["state"]
                if (
                    task.get("original_input", {})
                    .get("client_context", {})
                    .get("queued_behind")
                ):
                    return task["state"]
                snapshot = snapshot_for_task(task)
                steps = orchestration_plan(snapshot)
                if task["state"] == TaskState.RECEIVED:
                    await led.transition(task_id, TaskState.VALIDATING)
                    task = await repo.get_task(task_id)
                if task["state"] == TaskState.VALIDATING:
                    try:
                        validate_plan_inputs(
                            snapshot.draft.definition, snapshot.arguments
                        )
                    except ValueError as exc:
                        return (
                            await led.transition(
                                task_id,
                                TaskState.REJECTED,
                                reason={"code": "input_invalid", "message": str(exc)},
                            )
                        )["state"]
                    await led.transition(task_id, TaskState.QUEUED)
                    task = await repo.get_task(task_id)
                index = int(task["current_step"])
                if not 0 <= index < len(steps):
                    raise CapabilityError("Formal step cursor is outside its plan")
                step = steps[index]
                active = task.get("active_attempt_id")
                if active is None:
                    attempts = await repo.list_attempts(task_id)
                    if any(
                        (a.get("refs") or {}).get("slot") == step.id for a in attempts
                    ):
                        return (
                            await led.transition(
                                task_id,
                                TaskState.FAILED,
                                reason={"code": "formal_step_already_settled"},
                            )
                        )["state"]
                    previous_outputs, artifacts = await accepted_outputs(
                        repo, task, snapshot, index
                    )
                    arguments = bind_step_inputs(
                        step, snapshot.arguments, previous_outputs
                    )
                    root = await root_call(repo, task)
                    identity = step_identity(task, step, index)
                    child = await repo.add_call(
                        {
                            "id": str(uuid.uuid4()),
                            "owner_actor_id": str(task["owner_actor_id"]),
                            "session_id": str(task["session_id"]),
                            "task_id": task_id,
                            "parent_call_id": root["id"],
                            "kind": "method",
                            "origin": "method",
                            "call_key": step_call_key(task_id, step.id),
                            "digest": digest(identity),
                            "status": "created",
                            "detail": identity,
                        }
                    )
                    if (
                        child["digest"] != digest(identity)
                        or child["parent_call_id"] != root["id"]
                    ):
                        raise CapabilityError("Formal child method identity changed")
                    opened = await led.open_attempt(
                        task_id,
                        step_id=step.id,
                        step_version=digest(identity),
                        meta={
                            "kind": "formal_orchestration_step",
                            "snapshot_digest": task["pack_digest"],
                            "slot": step.id,
                            "step_index": index,
                            "reference": step.candidates[0].model_dump(mode="json"),
                            "method_call_id": child["id"],
                            "arguments_digest": digest(arguments),
                            "input_bindings": [
                                a for a in artifacts if a["step_id"] in step.depends_on
                            ],
                        },
                    )
                    active = opened["attempt_id"]
                    await led.attempt_transition(str(active), AttemptState.RUNNING)
                    task = await repo.get_task(task_id)
                attempt = await repo.get_attempt(str(active), for_update=True)
                await step_context(repo, task, attempt)
                call_id = (attempt.get("refs") or {}).get("capability_call_id")
                owner = str(task["owner_actor_id"])
            if call_id is None:
                try:
                    if self.capabilities is None:
                        raise CapabilityError("Formal execution service is unavailable")
                    async with self.stores() as repo:
                        call = await self.capabilities(repo).request_host_call(
                            owner, task_id, str(active)
                        )
                    call_id = call["id"]
                except (CapabilityError, ValueError) as exc:
                    return await self.finish(
                        task_id,
                        str(active),
                        "rejected",
                        rejection_code="authorization_denied",
                        detail=str(exc),
                    )
            async with self.stores() as repo:
                await repo.recover_calls(owner, str(task["session_id"]))
                call = await repo.capability_call(owner, call_id)
                task = await repo.get_task(task_id)
                attempt = await repo.get_attempt(str(active))
                if attempt["status"] in ATTEMPT_TERMINAL or str(
                    task.get("active_attempt_id")
                ) != str(active):
                    # Another driver already settled this observed receipt.
                    # The next pass owns the new step, never this stale Attempt.
                    return task["state"]
                validate_host_call(task, attempt, call)
                status = call["status"]
                if status == "awaiting_approval":
                    denial = await self.pending_denial(task_id, str(active), call_id)
                    if denial is not None:
                        return await self.finish(
                            task_id,
                            str(active),
                            "rejected",
                            rejection_code="authorization_denied",
                            detail=denial[1],
                            observed=denial[0],
                        )
                    return task["state"]
                if status == "dispatching" or task["state"] in TASK_TERMINAL:
                    return task["state"]
                if status == "succeeded":
                    _, step, frozen, _ = await step_context(repo, task, attempt)
                    try:
                        outputs = validate_step_outputs(
                            step,
                            structured_outputs(
                                frozen.draft, call["detail"].get("result") or {}
                            ),
                        )
                        bounded_json(outputs)
                    except (ValueError, TypeError, RecursionError) as exc:
                        return await self.finish(
                            task_id,
                            str(active),
                            "failed",
                            detail=str(exc),
                            observed=call,
                        )
                else:
                    outputs = None
            outcome = (
                "succeeded"
                if status == "succeeded"
                else "rejected"
                if status in ("declined", "expired")
                else "unknown"
                if status == "unknown"
                else "failed"
            )
            state = await self.finish(
                task_id,
                str(active),
                outcome,
                outputs=outputs,
                rejection_code="approval_denied" if outcome == "rejected" else None,
                observed=call,
            )
            if state in TASK_TERMINAL or state == TaskState.WAITING:
                return state
        raise CapabilityError("Formal step expansion exceeded its bound")

    async def pending_denial(self, task_id, attempt_id, call_id):
        """Expire invalid authority only while the exact action is undispatched."""
        from app.application.workbench.formal_host_calls import FormalHostCalls

        async with self.stores() as repo, repo.transaction():
            task = await repo.get_task(task_id, for_update=True)
            if (
                task["state"] in TASK_TERMINAL
                or str(task.get("active_attempt_id")) != attempt_id
            ):
                return None
            owner = str(task["owner_actor_id"])
            call = await repo.capability_call(owner, call_id, lock=True)
            if call["status"] != "awaiting_approval" or str(
                task.get("active_interaction_id")
            ) != str(call["interaction_id"]):
                return None
            try:
                if self.capabilities is None:
                    raise CapabilityError("Formal execution service is unavailable")
                await FormalHostCalls(self.capabilities(repo)).context(
                    owner, task_id, attempt_id
                )
            except (CapabilityError, ValueError) as exc:
                await repo.cancel_pending_interactions(
                    task_id=task_id, reason="formal dependency authority unavailable"
                )
                await repo.update_call(owner, call_id, "expired", call["detail"])
                return {**call, "status": "expired"}, str(exc)
            return None

    async def finish(
        self,
        task_id,
        attempt_id,
        outcome,
        *,
        outputs=None,
        rejection_code=None,
        detail="",
        observed=None,
    ):
        async with self.stores() as repo, repo.transaction():
            task = await repo.get_task(task_id, for_update=True)
            attempt = await repo.get_attempt(attempt_id, for_update=True)
            if task["state"] in TASK_TERMINAL or attempt["status"] in ATTEMPT_TERMINAL:
                return task["state"]
            if str(task.get("active_attempt_id")) != attempt_id:
                raise CapabilityError("Formal step is no longer active")
            snapshot, step, _, _ = await step_context(repo, task, attempt)
            refs = attempt.get("refs") or {}
            linked = refs.get("capability_call_id")
            if observed is not None:
                if linked != observed["id"]:
                    raise CapabilityError("Formal step result changed invocation")
                current = await repo.capability_call(
                    str(task["owner_actor_id"]), linked, lock=True
                )
                validate_host_call(task, attempt, current)
                if current["status"] != observed["status"] or digest(
                    current["detail"].get("result")
                ) != digest(observed["detail"].get("result")):
                    return task["state"]
            elif linked:
                return task["state"]
            led = Ledger(repo)
            if task["state"] == TaskState.WAITING:
                await repo.cancel_pending_interactions(
                    task_id=task_id, reason="formal step settled"
                )
                await led.transition(
                    task_id, TaskState.QUEUED, active_interaction_id=None
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
            versions = []
            if outcome == "succeeded":
                artifact = await repo.put_artifact(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    name="formal-step:" + step.id,
                    kind="step",
                    content=outputs,
                )
                versions = [
                    {key: artifact[key] for key in ("id", "name", "version", "digest")}
                ]
            await repo.update_attempt(
                attempt_id, refs={**refs, "formal_result": result}
            )
            await led.attempt_transition(
                attempt_id,
                AttemptState.COMPLETED
                if outcome == "succeeded"
                else AttemptState.FAILED,
                failure_code=rejection_code
                or (None if outcome == "succeeded" else outcome),
                retryable=False,
                output_artifacts=versions,
            )
            child = await repo.call_by_key(
                str(task["owner_actor_id"]),
                str(task["session_id"]),
                step_call_key(task_id, step.id),
            )
            if child is None or child["id"] != refs.get("method_call_id"):
                raise CapabilityError("Formal child method is missing")
            await repo.update_call(
                str(task["owner_actor_id"]),
                child["id"],
                "succeeded"
                if outcome == "succeeded"
                else "unknown"
                if outcome == "unknown"
                else "failed",
                {**child["detail"], "result": result},
            )
            task = await repo.get_task(task_id)
            if outcome != "succeeded":
                return (
                    await led.transition(
                        task_id,
                        TaskState.FAILED,
                        reason={
                            "code": rejection_code or outcome,
                            "message": detail[:500],
                        },
                    )
                )["state"]
            index = int(task["current_step"]) + 1
            if index == len(orchestration_plan(snapshot)):
                accepted, _ = await accepted_outputs(repo, task, snapshot, index)
                exported = export_outputs(snapshot.draft.definition, accepted)
                bounded_json(exported)
                await repo.update_attempt(
                    attempt_id,
                    refs={
                        **refs,
                        "formal_result": result,
                        "formal_exports": {
                            "outputs": exported,
                            "outputs_digest": digest(exported),
                        },
                    },
                )
                return (await led.transition(task_id, TaskState.SUCCEEDED))["state"]
            await repo.cas_task(
                task_id, expected_version=task["state_version"], current_step=index
            )
            return (await led.transition(task_id, TaskState.QUEUED))["state"]
