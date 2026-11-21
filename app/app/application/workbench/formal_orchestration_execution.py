"""Deterministic formal steps on the existing Task/Attempt/Artifact ledger."""

import uuid
from decimal import Decimal

from app.application.workbench.ledger import Ledger
from app.application.workbench.method_calls import root_call
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.formal_execution import (
    dependency_for_step,
    execution_slot,
    execution_steps,
    expanded_plan,
    fixed_step_outputs,
    orchestration_plan,
    recursive_plan,
    snapshot_for_task,
)
from app.domain.workbench.formal_host import validate_host_call
from app.domain.workbench.formal_method_execution_generated import bounded_json
from app.domain.workbench.formal_method_plan_generated import (
    bind_step_inputs,
    export_outputs,
    validate_plan_inputs,
    validate_step_outputs,
)
from app.domain.workbench.formal_recursive import node_artifact, node_key
from app.domain.workbench.states import (
    ATTEMPT_TERMINAL,
    TASK_TERMINAL,
    AttemptState,
    TaskState,
)


def step_call_key(task_id, slot):
    return "formal-method-step:" + task_id + ":" + slot


def step_identity(task, step, index):
    snapshot = snapshot_for_task(task)
    if recursive_plan(snapshot):
        from app.domain.workbench.formal_recursive import node_identity

        return node_identity(task, expanded_plan(snapshot).leaf_nodes[index])
    return {
        "identity_basis": "formal_dependency",
        "task_id": str(task["id"]),
        "snapshot_digest": task["pack_digest"],
        "slot": step.id,
        "step_index": index,
        "reference": step.candidates[0].model_dump(mode="json"),
    }


async def save_step_output(repo, task_id, attempt_id, name, outputs):
    artifact = await repo.put_artifact(
        task_id=task_id, attempt_id=attempt_id, name=name, kind="step", content=outputs
    )
    return {key: artifact[key] for key in ("id", "name", "version", "digest")}


async def read_step_output(repo, task_id, attempt_id, name, versions, result):
    """Read the accepted version even when a newer artifact shares its name."""
    if len(versions) != 1:
        raise CapabilityError("Formal dependency has no exact step artifact version")
    version = versions[0]
    if (
        not isinstance(version, dict)
        or type(version.get("version")) is not int
        or version["version"] < 1
        or version.get("name") != name
    ):
        raise CapabilityError("Formal dependency has no exact step artifact version")
    artifact = await repo.get_artifact(
        task_id=task_id, name=name, version=version["version"]
    )
    if (
        artifact is None
        or artifact["kind"] != "step"
        or str(artifact["task_id"]) != str(task_id)
        or str(artifact["attempt_id"]) != str(attempt_id)
        or str(artifact["id"]) != version.get("id")
        or any(artifact[k] != version.get(k) for k in ("name", "version", "digest"))
        or digest(artifact["content"]) != result.get("outputs_digest")
        or artifact["content"] != result.get("outputs")
    ):
        raise CapabilityError("Formal accepted artifact changed")
    return artifact["content"]


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
        content = await read_step_output(
            repo,
            str(task["id"]),
            str(attempt["id"]),
            "formal-step:" + step.id,
            versions,
            result,
        )
        version = versions[0]
        value = validate_step_outputs(step, content)
        bounded_json(value)
        outputs[step.id] = value
        artifacts.append({"step_id": step.id, **version})
    return outputs, artifacts


async def step_context(repo, task, attempt):
    """Resolve the active leaf from the frozen root, not from caller-controlled refs."""
    snapshot = snapshot_for_task(task)
    steps = execution_steps(snapshot)
    index = int(task["current_step"])
    if not 0 <= index < len(steps):
        raise CapabilityError("Formal step cursor is outside its plan")
    step = steps[index]
    frozen = dependency_for_step(snapshot, step)
    refs = attempt.get("refs") or {}
    slot = execution_slot(snapshot, index)
    if (
        str(attempt["task_id"]) != str(task["id"])
        or refs.get("kind") != "formal_orchestration_step"
        or refs.get("slot") != slot
        or refs.get("step_index") != index
        or refs.get("snapshot_digest") != task["pack_digest"]
        or refs.get("reference") != frozen.reference.model_dump(mode="json")
        or attempt["step_id"] != slot
        or attempt["step_version"] != digest(step_identity(task, step, index))
    ):
        raise CapabilityError("Formal step Attempt differs from its pinned plan")
    if recursive_plan(snapshot):
        from app.application.workbench.formal_recursive_runtime import resolve_leaf

        call, inputs, bindings, extra = await resolve_leaf(repo, task, snapshot)
        if call["id"] != refs.get("method_call_id") or any(
            refs.get(k) != v for k, v in extra.items()
        ):
            raise CapabilityError("Formal nested Attempt changed invocation")
    else:
        outputs, artifacts = await accepted_outputs(repo, task, snapshot, index)
        inputs = bind_step_inputs(step, snapshot.arguments, outputs)
        bindings = [a for a in artifacts if a["step_id"] in step.depends_on]
    if refs.get("input_bindings") != bindings or refs.get("arguments_digest") != digest(
        inputs
    ):
        raise CapabilityError("Formal step input artifact binding changed")
    return snapshot, step, frozen, inputs


class FormalOrchestrationExecution:
    def __init__(self, stores, capabilities, *, instructions=None):
        self.instructions = instructions
        self.stores, self.capabilities = stores, capabilities

    async def drive(self, task_id):
        from app.application.workbench.formal_recursive_runtime import settle_ancestors

        # Each pass advances at most the declared step count. I/O never runs
        # inside the Task transaction and a pending action returns to the caller.
        for _ in range(1001):
            async with self.stores() as repo, repo.transaction():
                task = await repo.get_task(task_id, for_update=True)
                led = Ledger(repo)
                if task["state"] in TASK_TERMINAL:
                    if task["state"] in (TaskState.FAILED, TaskState.CANCELLED):
                        snapshot = snapshot_for_task(task)
                        if recursive_plan(snapshot):
                            await settle_ancestors(
                                repo,
                                task,
                                snapshot,
                                outcome="cancelled"
                                if task["state"] == TaskState.CANCELLED
                                else "failed",
                            )
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
                steps = execution_steps(snapshot)
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
                slot = execution_slot(snapshot, index)
                active = task.get("active_attempt_id")
                if active is None:
                    attempts = await repo.list_attempts(task_id)
                    if any((a.get("refs") or {}).get("slot") == slot for a in attempts):
                        return (
                            await led.transition(
                                task_id,
                                TaskState.FAILED,
                                reason={"code": "formal_step_already_settled"},
                            )
                        )["state"]
                    extra = {}
                    identity = step_identity(task, step, index)
                    if recursive_plan(snapshot):
                        from app.application.workbench.formal_recursive_runtime import (
                            execution_scopes,
                            resolve_leaf,
                        )

                        try:
                            child, arguments, bindings, extra = await resolve_leaf(
                                repo, task, snapshot, create=True
                            )
                            await execution_scopes(repo, task, snapshot)
                        except (CapabilityError, ValueError) as exc:
                            await settle_ancestors(
                                repo,
                                task,
                                snapshot,
                                outcome="rejected",
                                detail=str(exc),
                            )
                            return (
                                await led.transition(
                                    task_id,
                                    TaskState.FAILED,
                                    reason={
                                        "code": "authorization_denied",
                                        "message": str(exc),
                                    },
                                )
                            )["state"]
                    else:
                        previous_outputs, artifacts = await accepted_outputs(
                            repo, task, snapshot, index
                        )
                        arguments = bind_step_inputs(
                            step, snapshot.arguments, previous_outputs
                        )
                        bindings = [
                            a for a in artifacts if a["step_id"] in step.depends_on
                        ]
                        root = await root_call(repo, task)
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
                            raise CapabilityError(
                                "Formal child method identity changed"
                            )
                    instruction_meta = {}
                    reserve = Decimal(0)
                    frozen = dependency_for_step(snapshot, step)
                    if frozen.draft.source and frozen.draft.source.is_instruction:
                        from app.domain.workbench.models import Budget

                        try:
                            caps = (
                                self.capabilities(repo) if self.capabilities else None
                            )
                            if not caps or not caps.instruction_service:
                                raise CapabilityError(
                                    "Isolated instruction execution is unavailable"
                                )
                            owner = str(task["owner_actor_id"])
                            svc = await caps.instruction_service(
                                caps.formal_sources, owner
                            )
                            quote = await svc.route_quote(owner, task, "knowlab")
                            reserve = Decimal(quote["maximum"])
                            if (
                                reserve
                                > Budget.model_validate(task["budget"]).available
                            ):
                                raise CapabilityError(
                                    "Insufficient remaining case budget for this instruction"
                                )
                            instruction_meta = {
                                "instruction_unit": {
                                    "pending": True,
                                    "scope": "attempt",
                                },
                                "instruction_quote": quote,
                                "model": quote["model"],
                                "model_provider": quote["provider"],
                            }
                        except (CapabilityError, ValueError) as exc:
                            await repo.update_call(
                                str(task["owner_actor_id"]),
                                child["id"],
                                "failed",
                                {**child["detail"], "error": str(exc)},
                            )
                            if recursive_plan(snapshot):
                                await settle_ancestors(
                                    repo,
                                    task,
                                    snapshot,
                                    outcome="failed",
                                    detail=str(exc),
                                )
                            return (
                                await led.transition(
                                    task_id,
                                    TaskState.FAILED,
                                    reason={
                                        "code": "instruction_unavailable",
                                        "message": str(exc),
                                    },
                                )
                            )["state"]
                    opened = await led.open_attempt(
                        task_id,
                        step_id=slot,
                        reserve=reserve,
                        input_artifact_versions=bindings,
                        step_version=digest(identity),
                        meta={
                            "kind": "formal_orchestration_step",
                            "snapshot_digest": task["pack_digest"],
                            "slot": slot,
                            "step_index": index,
                            "reference": step.candidates[0].model_dump(mode="json"),
                            "method_call_id": child["id"],
                            "arguments_digest": digest(arguments),
                            "input_bindings": bindings,
                            **extra,
                            **instruction_meta,
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
                instruction_ready = (
                    status == "dispatching"
                    and call["detail"].get("execution_kind") == "formal_instruction"
                    and self.instructions is not None
                )
                if task["state"] in TASK_TERMINAL or (
                    status == "dispatching" and not instruction_ready
                ):
                    return task["state"]
                if status == "succeeded":
                    _, step, frozen, _ = await step_context(repo, task, attempt)
                    try:
                        outputs = validate_step_outputs(
                            step,
                            fixed_step_outputs(
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
            if instruction_ready:
                # The model round trip must not occupy the polling repository connection.
                assert self.instructions is not None
                await self.instructions.drive(task_id, leaf_attempt_id=str(active))
                async with self.stores() as repo:
                    after = await repo.capability_call(owner, call_id)
                    if after["status"] == "dispatching":
                        return (await repo.get_task(task_id))["state"]
                continue
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
                name = (
                    "formal-step:" + step.id
                    if not recursive_plan(snapshot)
                    else node_artifact(refs["path"])
                )
                versions = [
                    await save_step_output(repo, task_id, attempt_id, name, outputs)
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
                step_call_key(task_id, step.id)
                if not recursive_plan(snapshot)
                else node_key(task_id, refs["path"]),
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
                {
                    **child["detail"],
                    "result": result,
                    **(
                        {
                            "output_artifact": versions[0],
                            "execution_attempt_id": attempt_id,
                        }
                        if recursive_plan(snapshot) and versions
                        else {}
                    ),
                },
            )
            task = await repo.get_task(task_id)
            if outcome != "succeeded":
                if recursive_plan(snapshot):
                    from app.application.workbench.formal_recursive_runtime import (
                        settle_ancestors,
                    )

                    await settle_ancestors(
                        repo, task, snapshot, outcome=outcome, detail=detail
                    )
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
            if recursive_plan(snapshot):
                from app.application.workbench.formal_recursive_runtime import (
                    settle_ancestors,
                )

                exports = await settle_ancestors(
                    repo, task, snapshot, outcome="succeeded"
                )
                if exports is not None:
                    if index != len(execution_steps(snapshot)):
                        raise CapabilityError(
                            "Formal root returned before all leaf attempts"
                        )
                    await repo.update_attempt(
                        attempt_id,
                        refs={
                            **refs,
                            "formal_result": result,
                            "formal_exports": exports,
                        },
                    )
                    return (await led.transition(task_id, TaskState.SUCCEEDED))["state"]
            elif index == len(orchestration_plan(snapshot)):
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
