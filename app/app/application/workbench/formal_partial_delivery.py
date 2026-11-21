"""Author trial handoff of verified outputs from completed or interrupted fixed plans."""

from decimal import InvalidOperation

from app.application.workbench.formal_orchestration_evidence import verify_step_evidence
from app.domain.workbench.capabilities import digest
from app.domain.workbench.errors import WorkbenchError
from app.domain.workbench.formal_execution import orchestration_plan, recursive_plan
from app.domain.workbench.formal_method_execution_generated import fingerprint
from app.domain.workbench.formal_method_generated import Orchestration
from app.domain.workbench.formal_recursive import TREE_ADAPTER
from app.domain.workbench.states import AttemptState, TaskState


async def partial_case_delivery(
    repo, request, task, snapshot, case, repetition, result, attempts
):
    """Seal checked outputs of an incomplete flat join before trial artifacts expire."""

    graph = request.draft.definition.procedure
    if (
        not isinstance(graph, Orchestration)
        or not snapshot
        or recursive_plan(snapshot)
        or task["state"] not in (TaskState.FAILED, TaskState.CANCELLED)
        or not any(
            len(
                {
                    b.binding.step
                    for b in s.inputs.values()
                    if b.binding.source == "step"
                }
            )
            >= 2
            for s in graph.steps
        )
        or snapshot.run_id != request.run_id
        or snapshot.request_digest != request.fingerprint()
        or snapshot.case_id != case.case_id
        or snapshot.repetition != repetition
        or snapshot.draft != request.draft
        or snapshot.arguments != case.input.values
    ):
        return None
    owner = str(task["owner_actor_id"])
    if str(await repo.actor_for_subject(request.owner_subject)) != owner:
        return None
    task_id, session_id = str(task["id"]), str(task["session_id"])
    root = await repo.call_by_key(owner, session_id, "task:" + task_id)
    valid_root = (
        root
        and root["kind"] == "method"
        and root["origin"] == "method"
        and root.get("parent_call_id") is None
        and str(root["task_id"]) == task_id
        and str(root["session_id"]) == session_id
        and str(root["owner_actor_id"]) == owner
        and root["digest"] == digest(["task:" + task_id])
        and root["detail"].get("identity_basis") == "host_task"
        and root["detail"].get("task_id") == task_id
    )
    events = await repo.list_record_events(
        session_id=session_id, types=("method/permissionsConfirmed",)
    )
    plan = orchestration_plan(snapshot)
    steps, accepted, versions = [], {}, []
    by_step = {}
    for attempt in attempts:
        by_step.setdefault(attempt["step_id"], []).append(attempt)
    for index, step in enumerate(plan):
        matches = by_step.get(step.id, [])
        attempt = matches[0] if len(matches) == 1 else None
        artifact = None
        detail = ""
        if len(matches) > 1:
            state = "unverified"
        elif attempt is None:
            state = "not_run"
        elif attempt["status"] == AttemptState.COMPLETED:
            state = "unverified"
            try:
                if (
                    not valid_root
                    or not set(step.depends_on).issubset(accepted)
                    or index >= len(attempts)
                    or attempts[index] != attempt
                ):
                    raise ValueError("step_identity_mismatch")
                item = {}
                await verify_step_evidence(
                    repo,
                    owner,
                    task,
                    snapshot,
                    step,
                    index,
                    attempt,
                    attempts[index - 1] if index else None,
                    item,
                    root,
                    events,
                    accepted,
                    versions,
                )
                source = item["artifact"]
                artifact = {key: source[key] for key in ("id", "digest", "version")}
                artifact["content_digest"] = fingerprint(accepted[step.id])
                artifact["output_names"] = sorted(accepted[step.id])
                state = "accepted"
            except (
                ValueError,
                WorkbenchError,
                KeyError,
                TypeError,
                IndexError,
                AttributeError,
                InvalidOperation,
            ):
                # An unverifiable step cannot be offered, even if its attempt says completed.
                accepted.pop(step.id, None)
        else:
            actual = (attempt.get("refs") or {}).get("formal_result") or {}
            outcome = actual.get("outcome")
            detail = (actual.get("detail") or result.get("detail") or "")[:2000]
            state = (
                "unknown"
                if "unknown" in (outcome, result["outcome"])
                else "cancelled"
                if task["state"] == TaskState.CANCELLED
                else "failed"
            )
        steps.append(
            dict(
                step=step.id,
                depends_on=list(step.depends_on),
                state=state,
                artifact=artifact,
                detail=detail,
            )
        )
    missing = {
        name
        for name, export in graph.exports.items()
        if export.output not in accepted.get(export.step, {})
    }
    promises = {}
    for rule in request.draft.definition.how_to_validate:
        affected = missing.intersection(rule.outputs)
        if affected:
            promises.setdefault(rule.promise_id, set()).update(affected)
    report = {
        "steps": steps,
        "missing_promises": {
            k: {
                "description": request.draft.definition.capability_declaration.promises[
                    k
                ],
                "missing_outputs": sorted(v),
            }
            for k, v in promises.items()
        },
    }
    scope = fingerprint(
        [request.fingerprint(), task_id, case.case_id, repetition, report]
    )
    return {
        "report": {**report, "scope_digest": scope, "received": []},
        "outputs": accepted,
        "receipts": {},
    }


def verified_case_delivery(request, evidence):
    """Index completed flat-plan artifacts. Their content stays in sealed evidence."""
    graph = request.draft.definition.procedure
    case = evidence["result"]
    if (
        not isinstance(graph, Orchestration)
        or not evidence["validated"]
        or case["status"] != "passed"
        or case["outcome"] != "succeeded"
        or evidence.get("adapter") == TREE_ADAPTER
    ):
        return None
    steps = {s.id: s for s in graph.steps}
    progress = []
    for item in evidence["steps"]:
        step = steps[item["slot"]]
        progress.append(
            dict(
                step=step.id,
                depends_on=list(step.depends_on),
                state="accepted",
                detail="",
                artifact={
                    **{k: item["artifact"][k] for k in ("id", "version", "digest")},
                    "content_digest": fingerprint(item["outputs"]),
                    "output_names": sorted(item["outputs"]),
                },
            )
        )
    report = {"steps": progress, "missing_promises": {}}
    scope = fingerprint(
        [
            request.fingerprint(),
            case["task_id"],
            case["case_id"],
            case["repetition"],
            report,
        ]
    )
    return {"report": {**report, "scope_digest": scope, "received": []}, "receipts": {}}
