"""Nested call scopes and accepted artifacts on the shared Task and call ledger.

Only leaves receive execution Attempts. Intermediate calls are real invocations,
not fabricated tasks or permission delegates. All mutations require the Task lock.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.application.workbench.method_calls import root_call
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.formal_execution import (
    dependency_for_step,
    expanded_plan,
    fixed_step_outputs,
)
from app.domain.workbench.formal_host import validate_host_call
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
    bounded_json,
)
from app.domain.workbench.formal_method_plan_generated import (
    bind_step_inputs,
    export_outputs,
    unique_plan,
    validate_plan_inputs,
    validate_step_outputs,
)
from app.domain.workbench.formal_recursive import (
    node_artifact,
    node_identity,
    node_key,
    node_slot,
)
from app.domain.workbench.method_authorization import MethodAuthorization
from app.domain.workbench.states import AttemptState


def _require(condition, detail):
    if not condition:
        raise CapabilityError(detail)


def _node(snapshot, path):
    return next(n for n in expanded_plan(snapshot).nodes if n.path == tuple(path))


async def scope_deadline(repo, task, snapshot):
    from app.domain.workbench.ordinary_expert import ConsumerPlan

    if isinstance(snapshot, ConsumerPlan):
        if snapshot.scope_hours is None:
            raise CapabilityError("Missing confirmed recursive scope duration")

        created = task["created_at"]
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        return created + timedelta(hours=snapshot.scope_hours)
    run = await repo.test_run_for_task(str(task["id"]))
    _require(run is not None, "Formal scope has no accepted test run")
    request = FormalMethodExecutionRequest.model_validate(run["request"])
    _require(
        str(run["run_id"]) == str(snapshot.run_id)
        and request.fingerprint() == snapshot.request_digest
        and run["digest"] == snapshot.request_digest.removeprefix("sha256:"),
        "Formal scope differs from its accepted request",
    )
    created = run["created_at"]
    if isinstance(created, str):
        created = datetime.fromisoformat(created)
    return created + timedelta(
        seconds=request.execution.timeout_seconds + request.execution.max_wait_seconds
    )


async def _scope(repo, task, call, method_id, deadline, parent=None, *, create=False):
    owner, session = str(task["owner_actor_id"]), str(task["session_id"])
    raw = await repo.method_authorization(owner, call["id"], lock=True)
    if raw is None and create:
        scope = MethodAuthorization(
            call_id=call["id"],
            revision=1,
            parent_call_id=parent.call_id if parent else None,
            parent_revision=parent.revision if parent else None,
            method_id=str(method_id),
            revoked=False,
            expires_at=deadline,
            grants=(),
        )
        await repo.save_method_authorization(
            owner, session, scope.model_dump(mode="json"), previous=0
        )
        await repo.append_event(
            session_id=session,
            task_id=str(task["id"]),
            kind="task",
            event_type="method/scopeCreated",
            payload={
                "authorization": scope.model_dump(mode="json"),
                "path": call["detail"].get("path", []),
                "snapshot_digest": task["pack_digest"],
            },
        )
    else:
        _require(raw is not None, "Formal ancestor execution scope is missing")
        scope = MethodAuthorization.model_validate(raw)
    _require(
        not scope.revoked
        and scope.expires_at == deadline
        and deadline > datetime.now(UTC)
        and str(scope.call_id) == str(call["id"])
        and scope.method_id == str(method_id)
        and scope.parent_call_id == (parent.call_id if parent else None)
        and (
            scope.parent_revision is None
            if parent is None
            else scope.parent_revision is not None
            and scope.parent_revision <= parent.revision
        )
        and (not scope.grants if parent is not None else True),
        "Formal ancestor execution scope expired, changed or was revoked",
    )
    return scope


async def read_artifact(
    repo, task, path, reference, result, *, attempt_id=None, exports=False
):
    _require(
        isinstance(reference, dict)
        and reference.get("name") == node_artifact(path, exports=exports)
        and type(reference.get("version")) is int
        and reference["version"] >= 1,
        "Formal accepted artifact does not identify the planned output version",
    )
    artifact = await repo.get_artifact(
        task_id=str(task["id"]), name=reference["name"], version=reference["version"]
    )
    _require(
        artifact is not None
        and artifact["kind"] == "step"
        and str(artifact["id"]) == reference.get("id")
        and artifact["digest"] == reference.get("digest")
        and (str(artifact["attempt_id"]) if artifact["attempt_id"] else None)
        == attempt_id
        and artifact["content"] == result.get("outputs")
        and digest(artifact["content"]) == result.get("outputs_digest"),
        "Formal accepted artifact changed",
    )
    assert artifact is not None
    return artifact["content"]


async def accepted_children(repo, task, snapshot, parent, parent_path, before=None):
    definition = (
        _node(snapshot, parent_path).definition
        if parent_path
        else snapshot.draft.definition
    )
    values, bindings = {}, []
    for step in unique_plan(definition):
        if step.id == before:
            break
        path = (*parent_path, step.id)
        node = _node(snapshot, path)
        call = await repo.call_by_key(
            str(task["owner_actor_id"]),
            str(task["session_id"]),
            node_key(task["id"], path),
        )
        _require(
            call is not None
            and call["parent_call_id"] == parent["id"]
            and call["digest"] == digest(node_identity(task, node))
            and call["status"] == "succeeded",
            "Formal predecessor is not accepted",
        )
        assert call is not None
        detail = call["detail"]
        _require(
            all(detail.get(k) == v for k, v in node_identity(task, node).items()),
            "Formal predecessor identity changed",
        )
        result = detail.get("result") or {}
        _require(
            result.get("outcome") == "succeeded",
            "Formal predecessor has no validated result",
        )
        ref = detail.get("output_artifact")
        value = await read_artifact(
            repo, task, path, ref, result, attempt_id=detail.get("execution_attempt_id")
        )
        if node.kind == "tool":
            attempt_id = detail.get("execution_attempt_id")
            _require(
                isinstance(attempt_id, str), "Accepted leaf has no execution Attempt"
            )
            attempt = await repo.get_attempt(attempt_id)
            refs = attempt.get("refs") or {}
            index = next(
                i
                for i, leaf in enumerate(expanded_plan(snapshot).leaf_nodes)
                if leaf.path == path
            )
            _require(
                str(attempt["task_id"]) == str(task["id"])
                and attempt["status"] == AttemptState.COMPLETED
                and attempt["step_id"] == node_slot(path)
                and attempt["step_version"] == digest(node_identity(task, node))
                and refs.get("kind") == "formal_orchestration_step"
                and refs.get("step_index") == index
                and refs.get("path") == list(path)
                and refs.get("method_call_id") == call["id"]
                and refs.get("parent_method_call_id") == parent["id"]
                and refs.get("snapshot_digest") == task["pack_digest"]
                and refs.get("formal_result") == result
                and refs.get("input_bindings") == detail.get("input_bindings")
                and refs.get("arguments_digest")
                == detail.get("arguments_digest")
                == digest(detail.get("arguments"))
                and attempt.get("output_artifacts") == [ref],
                "Formal accepted leaf differs from its completed Attempt",
            )
            _require(
                isinstance(refs.get("capability_call_id"), str),
                "Formal accepted leaf has no actual operation",
            )
            action = await repo.capability_call(
                str(task["owner_actor_id"]), refs["capability_call_id"]
            )
            validate_host_call(task, attempt, action)
            _require(
                action["status"] == "succeeded", "Formal leaf operation did not succeed"
            )
            actual = fixed_step_outputs(
                dependency_for_step(snapshot, step).draft,
                action["detail"].get("result") or {},
            )
            _require(
                validate_step_outputs(step, actual) == value,
                "Formal accepted leaf changed its actual outputs",
            )
        else:
            _require(
                detail.get("execution_attempt_id") is None,
                "Composite result cannot claim a leaf Attempt",
            )
            exports = detail.get("exports") or {}
            full = await read_artifact(
                repo, task, path, detail.get("exports_artifact"), exports, exports=True
            )
            children, _ = await accepted_children(repo, task, snapshot, call, path)
            _require(
                exports.get("outcome") == "succeeded"
                and export_outputs(node.definition, children) == full
                and validate_step_outputs(step, full) == value,
                "Formal composite outputs differ from its accepted children",
            )
        values[step.id] = validate_step_outputs(step, value)
        bindings.append(
            {
                "step_id": step.id,
                "path": list(path),
                "method_call_id": call["id"],
                **ref,
            }
        )
    return values, bindings


async def resolve_leaf(repo, task, snapshot, *, create=False):
    """Rebuild inputs from each real parent's own accepted sibling outputs."""
    leaf = expanded_plan(snapshot).leaf_nodes[int(task["current_step"])]
    owner, session = str(task["owner_actor_id"]), str(task["session_id"])
    parent = (
        await root_call(repo, task)
        if create
        else await repo.call_by_key(owner, session, "task:" + str(task["id"]))
    )
    _require(parent is not None, "Formal root call is missing")
    assert parent is not None
    scope = None
    deadline = None
    if create:
        deadline = await scope_deadline(repo, task, snapshot)
        first = await repo.call_by_key(
            owner, session, node_key(task["id"], leaf.path[:1])
        )
        scope = await _scope(
            repo,
            task,
            parent,
            snapshot.method_id,
            deadline,
            create=int(task["current_step"]) == 0 and first is None,
        )
    inputs = snapshot.arguments
    root_id = parent["id"]
    for length in range(1, len(leaf.path) + 1):
        path = leaf.path[:length]
        node = _node(snapshot, path)
        accepted, artifacts = await accepted_children(
            repo, task, snapshot, parent, node.parent_path, before=node.step.id
        )
        arguments = bind_step_inputs(node.step, inputs, accepted)
        bindings = [a for a in artifacts if a["step_id"] in node.step.depends_on]
        identity = node_identity(task, node)
        bound = {
            "arguments": arguments,
            "arguments_digest": digest(arguments),
            "input_bindings": bindings,
        }
        call = await repo.call_by_key(owner, session, node_key(task["id"], path))
        new_call = call is None
        if new_call and create:
            call = await repo.add_call(
                {
                    "id": str(uuid.uuid4()),
                    "owner_actor_id": owner,
                    "session_id": session,
                    "task_id": str(task["id"]),
                    "parent_call_id": parent["id"],
                    "kind": "method",
                    "origin": "method",
                    "call_key": node_key(task["id"], path),
                    "digest": digest(identity),
                    "status": "created",
                    "detail": {**identity, **bound},
                }
            )
            await repo.append_event(
                session_id=session,
                task_id=str(task["id"]),
                kind="task",
                event_type="method/expanded",
                payload={
                    "call_id": call["id"],
                    "parent_call_id": parent["id"],
                    **identity,
                    "arguments_digest": digest(arguments),
                },
            )
        _require(
            call is not None
            and call["kind"] == "method"
            and call["origin"] == "method"
            and call["parent_call_id"] == parent["id"]
            and call["digest"] == digest(identity)
            and all(
                call["detail"].get(k) == v for k, v in {**identity, **bound}.items()
            ),
            "Formal nested invocation or input binding changed",
        )
        assert call is not None
        if node.kind != "tool":
            validate_plan_inputs(node.definition, arguments)
            if create:
                scope = await _scope(
                    repo,
                    task,
                    call,
                    node.reference.method_id,
                    deadline,
                    scope,
                    create=new_call,
                )
        if path == leaf.path:
            return (
                call,
                arguments,
                bindings,
                {
                    "path": list(path),
                    "parent_method_call_id": parent["id"],
                    "root_method_call_id": root_id,
                },
            )
        parent, inputs = call, arguments
    raise CapabilityError("Formal plan has no leaf")


async def execution_scopes(repo, task, snapshot, *, create=False):
    """Scopes only restrict execution; intermediate scopes carry no resource grants."""
    owner, session = str(task["owner_actor_id"]), str(task["session_id"])
    leaf = expanded_plan(snapshot).leaf_nodes[int(task["current_step"])]
    deadline = await scope_deadline(repo, task, snapshot)
    root = await repo.call_by_key(owner, session, "task:" + str(task["id"]))
    _require(root is not None, "Formal root call is missing")
    assert root is not None
    scope = await _scope(repo, task, root, snapshot.method_id, deadline, create=create)
    scopes = [scope]
    previous = root
    for length in range(1, len(leaf.path)):
        path = leaf.path[:length]
        node = _node(snapshot, path)
        call = await repo.call_by_key(owner, session, node_key(task["id"], path))
        _require(
            call is not None
            and call["parent_call_id"] == previous["id"]
            and call["digest"] == digest(node_identity(task, node))
            and call["status"] in ("created", "running", "waiting"),
            "Formal ancestor call is no longer active",
        )
        scope = await _scope(
            repo, task, call, node.reference.method_id, deadline, scope, create=create
        )
        assert call is not None
        scopes.append(scope)
        previous = call
    return scopes


async def store_result(repo, task, path, result, *, exports=False):
    artifact = await repo.put_artifact(
        task_id=str(task["id"]),
        attempt_id=None,
        name=node_artifact(path, exports=exports),
        kind="step",
        content=result["outputs"],
    )
    return {k: artifact[k] for k in ("id", "name", "version", "digest")}


def _result(outputs):
    bounded_json(outputs)
    return {
        "outcome": "succeeded",
        "rejection_code": None,
        "detail": "",
        "outputs": outputs,
        "outputs_digest": digest(outputs),
    }


async def settle_ancestors(repo, task, snapshot, *, outcome, detail=""):
    """Return exports one level at a time, or stop only unfinished ancestors."""
    leaf = expanded_plan(snapshot).leaf_nodes[int(task["current_step"])]
    owner, session = str(task["owner_actor_id"]), str(task["session_id"])
    for length in reversed(range(len(leaf.path) + (outcome != "succeeded"))):
        path = leaf.path[:length]
        call = await repo.call_by_key(
            owner,
            session,
            node_key(task["id"], path) if path else "task:" + str(task["id"]),
        )
        if call is None and outcome != "succeeded":
            continue
        _require(call is not None, "Formal parent call is missing")
        assert call is not None
        if outcome != "succeeded":
            if path and call["status"] not in (
                "succeeded",
                "unknown",
                "failed",
                "cancelled",
            ):
                await repo.update_call(
                    owner,
                    call["id"],
                    "unknown"
                    if outcome == "unknown"
                    else "cancelled"
                    if outcome == "cancelled"
                    else "failed",
                    {
                        **call["detail"],
                        "result": {"outcome": outcome, "detail": detail[:2000]},
                    },
                )
            continue
        node = _node(snapshot, path) if path else None
        definition = node.definition if node else snapshot.draft.definition
        # A parent is ready only after all of its actual direct child calls finish.
        children = [
            await repo.call_by_key(owner, session, node_key(task["id"], (*path, s.id)))
            for s in unique_plan(definition)
        ]
        if any(c is None or c["status"] != "succeeded" for c in children):
            return None
        accepted, _ = await accepted_children(repo, task, snapshot, call, path)
        exports = _result(export_outputs(definition, accepted))
        export_ref = await store_result(repo, task, path, exports, exports=True)
        if node is None:
            await repo.update_call(
                owner,
                call["id"],
                call["status"],
                {**call["detail"], "exports": exports, "exports_artifact": export_ref},
            )
            return {
                "outputs": exports["outputs"],
                "outputs_digest": exports["outputs_digest"],
                "artifact": export_ref,
            }
        projected = _result(validate_step_outputs(node.step, exports["outputs"]))
        artifact = await store_result(repo, task, path, projected)
        await repo.update_call(
            owner,
            call["id"],
            "succeeded",
            {
                **call["detail"],
                "exports": exports,
                "exports_artifact": export_ref,
                "result": projected,
                "output_artifact": artifact,
                "execution_attempt_id": None,
            },
        )
        await repo.append_event(
            session_id=session,
            task_id=str(task["id"]),
            kind="task",
            event_type="method/returned",
            payload={
                "call_id": call["id"],
                "path": list(path),
                "exports_artifact": export_ref,
                "output_artifact": artifact,
                "outputs_digest": exports["outputs_digest"],
            },
        )
    return None
