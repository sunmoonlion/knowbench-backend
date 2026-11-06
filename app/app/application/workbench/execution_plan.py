"""One task-local execution view and exact input binding for dynamic methods.

The catalog cache remains author-only. This module reads accepted plan receipts
and existing Attempt/Artifact history; it owns no alternative state machine.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.catalog import pack_for_task
from app.application.workbench.plan_receipts import (
    catalog_for_task,
    read_plan,
    read_plan_version,
)
from app.domain.workbench.errors import (
    MethodInputInvalidated,
    MethodPlanInvalid,
    StaleStateVersion,
)
from app.domain.workbench.execution_plan import (
    ExecutionPlan,
    catalog_header,
    project_plan,
)
from app.domain.workbench.fixed_plan import fixed_pack_digest
from app.domain.workbench.method_plan import FixedMethodPlan
from app.domain.workbench.states import ATTEMPT_TERMINAL, AttemptState, TaskState, Wheel

if TYPE_CHECKING:
    from app.application.workbench.ledger import Ledger


def is_dynamic(task: dict[str, Any]) -> bool:
    snapshot = task.get("pack_snapshot")
    # Unknown future runtimes must also fail closed rather than fall back to a
    # similarly named built-in expert.
    return isinstance(snapshot, dict) and "runtime_contract" in snapshot


async def execution_for_task(
    repo: WorkbenchStore, task: dict[str, Any]
) -> ExecutionPlan | None:
    if not is_dynamic(task):
        pack = pack_for_task(task)
        if pack is None:
            return None
        if task.get("accepted_plan_version") is None:
            return ExecutionPlan(pack)  # 旧路（SDD 0050 开关关着，或开之前受理的）
        receipt = await read_plan(repo, task)
        assert receipt is not None
        return ExecutionPlan(pack, receipt)
    catalog = catalog_for_task(task)
    receipt = await read_plan(repo, task)
    if receipt is None:
        raise MethodPlanInvalid("dynamic execution requires an accepted task plan")
    try:
        return project_plan(catalog, receipt, profile_id=str(task["profile_id"]))
    except ValueError as exc:
        raise MethodPlanInvalid(str(exc)) from None


async def display_for_task(repo: WorkbenchStore, task: dict[str, Any]):
    """Read-only display can show the author's identity before a plan exists.

    Dispatch still requires execution_for_task and an accepted receipt.
    """
    if is_dynamic(task) and task.get("accepted_plan_version") is None:
        return catalog_header(
            catalog_for_task(task), profile_id=str(task["profile_id"])
        )
    view = await execution_for_task(repo, task)
    return view.pack if view else None


async def current_artifacts(repo: WorkbenchStore, task: dict[str, Any]):
    """Current accepted results for dossiers/reports; history remains separate."""
    if is_dynamic(task):
        if task.get("accepted_plan_version") is None:
            return {}
        view = await execution_for_task(repo, task)
        assert view is not None
        return {
            a["name"]: a for a in (await accepted_results(repo, task, view)).values()
        }
    return {
        a["name"]: a
        for a in await repo.list_artifacts_with_content(str(task["id"]))
        if a["kind"] == "step"
    }


async def final_results(repo: WorkbenchStore, task: dict[str, Any]):
    """Stable author final names resolve through the exact accepted plan.

    Never pick an instance by its name/order or substitute a latest artifact.
    This is a projection for the existing test predicates, not a new evaluator.
    """
    if not is_dynamic(task) or task.get("accepted_plan_version") is None:
        return {}
    view = await execution_for_task(repo, task)
    assert view is not None and view.receipt is not None
    results = await accepted_results(repo, task, view)
    return {
        method: results[instance]
        for method, instance in view.receipt.proposal.final_outputs.items()
        if instance in results
    }


async def test_method_results(repo: WorkbenchStore, task: dict[str, Any]):
    """v2 test projection. Only unique selected instances with current lineage.

    Missing, unselected or ambiguous methods remain absent and fail assertions;
    final methods retain their explicit final_outputs binding.
    """
    if not is_dynamic(task) or task.get("accepted_plan_version") is None:
        return {}
    view = await execution_for_task(repo, task)
    assert view is not None and view.receipt is not None
    results = await accepted_results(repo, task, view)
    instances: dict[str, list[str]] = {}
    for step in view.receipt.proposal.steps:
        instances.setdefault(step.method_id, []).append(step.instance_id)
    selected = {
        method: results[ids[0]]
        for method, ids in instances.items()
        if len(ids) == 1 and ids[0] in results
    }
    for method, instance in view.receipt.proposal.final_outputs.items():
        if instance in results:
            selected[method] = results[instance]
    return selected


def _inputs(
    view: ExecutionPlan,
    index: int,
    results: dict[str, dict[str, Any]],
    *,
    dispatch: bool = False,
) -> list[dict[str, Any]]:
    assert view.receipt is not None
    step = view.receipt.proposal.steps[index]
    refs = []
    for binding in step.inputs:
        artifact = results.get(binding.instance_id)
        if artifact is None:
            raise MethodPlanInvalid("input instance has no current accepted result")
        if binding.version is not None and artifact["version"] != binding.version:
            if dispatch:
                raise MethodInputInvalidated(
                    "pinned input was invalidated by rework",
                    plan_version=view.receipt.version,
                )
            raise MethodPlanInvalid("pinned input was invalidated by rework")
        refs.append(
            {
                "slot": binding.slot,
                "instance_id": binding.instance_id,
                "name": view.output_of(binding.instance_id),
                "version": artifact["version"],
                "id": str(artifact["id"]),
                "digest": artifact["digest"],
            }
        )
    return refs


def _meta(view: ExecutionPlan, index: int) -> dict[str, Any]:
    assert view.receipt is not None
    step = view.receipt.proposal.steps[index]
    return {
        "kind": "method_step",
        "plan_version": view.receipt.version,
        "plan_digest": view.receipt.proposal.digest(),
        "pack_digest": view.receipt.pack_digest,
        "instance_id": step.instance_id,
        "method_id": step.method_id,
        "method_version": view.pack.workflow[index].step_version,
        "tools": list(step.tools),
        "step_index": index,
    }


async def accepted_results(
    repo: WorkbenchStore,
    task: dict[str, Any],
    view: ExecutionPlan,
) -> dict[str, dict[str, Any]]:
    """Resolve only the completed prefix through successful attempt receipts.

    A newer unrelated Artifact never supersedes accepted output. On rewind the
    suffix immediately disappears; later consumers must match the new upstream
    lineage before their old output can be considered current again.
    """
    if view.receipt is None:
        raise MethodPlanInvalid("method results require a dynamic task plan")
    pinned = (
        fixed_pack_digest(task.get("pack_digest"), view.pack)
        if isinstance(view.receipt.proposal, FixedMethodPlan)
        else task.get("pack_digest")
    )
    if (
        view.receipt.task_id != str(task["id"])
        or view.receipt.pack_digest != pinned
        or view.receipt.version != task.get("accepted_plan_version")
    ):
        raise MethodPlanInvalid(
            "execution view belongs to another task or plan version"
        )
    cursor = int(task["current_step"])
    if not 0 <= cursor <= len(view.pack.workflow):
        raise MethodPlanInvalid("execution cursor is outside the accepted plan")
    attempts = await repo.list_attempts(str(task["id"]))
    receipts = {view.receipt.version: view.receipt}
    results: dict[str, dict[str, Any]] = {}
    for index, planned in enumerate(view.receipt.proposal.steps[:cursor]):
        matching = [
            a
            for a in attempts
            if a.get("step_id") == planned.instance_id and a["role"] == "execute"
        ]
        if not matching or matching[-1]["status"] != AttemptState.COMPLETED:
            raise MethodPlanInvalid(
                "completed instance has no successful latest attempt"
            )
        attempt = matching[-1]
        if attempt["step_version"] != view.pack.workflow[index].step_version:
            raise MethodPlanInvalid("method attempt version differs")
        refs = attempt.get("refs") or {}
        version = refs.get("plan_version")
        if type(version) is not int or not 1 <= version <= view.receipt.version:
            raise MethodPlanInvalid("method attempt does not pin an accepted plan")
        if version not in receipts:
            receipts[version] = await read_plan_version(repo, task, version)
        previous = receipts[version]
        if (
            index >= len(previous.proposal.steps)
            or previous.proposal.steps[index] != planned
        ):
            raise MethodPlanInvalid(
                "method attempt belongs to a different plan instance"
            )
        expected = _meta(view, index) | {
            "plan_version": version,
            "plan_digest": previous.proposal.digest(),
        }
        if any(refs.get(key) != value for key, value in expected.items()):
            raise MethodPlanInvalid("method attempt identity or permissions differ")
        if attempt["input_artifact_versions"] != _inputs(view, index, results):
            raise MethodPlanInvalid("accepted output has stale input lineage")
        outputs = attempt["output_artifacts"]
        if len(outputs) != 1:
            raise MethodPlanInvalid("method attempt must accept exactly one output")
        output = outputs[0]
        if (
            output.get("name") != view.output_of(planned.instance_id)
            or type(output.get("version")) is not int
            or output["version"] < 1
        ):
            raise MethodPlanInvalid("method output reference is invalid")
        artifact = await repo.get_artifact(
            task_id=str(task["id"]), name=output["name"], version=output["version"]
        )
        if (
            artifact is None
            or artifact["kind"] != "step"
            or str(artifact["id"]) != str(output.get("id"))
            or str(artifact["attempt_id"]) != str(attempt["id"])
        ):
            raise MethodPlanInvalid(
                "method output does not belong to its successful attempt"
            )
        results[planned.instance_id] = artifact
    return results


async def open_method_step(
    ledger: Ledger, task_id: str, *, expected_version: int
) -> dict[str, Any]:
    """Bind inputs and reserve/open the existing Attempt under one task lock."""
    repo = ledger.repo
    async with repo.transaction():
        task = await repo.get_task(task_id, for_update=True)
        if task["state_version"] != expected_version:
            raise StaleStateVersion(
                "task changed before method dispatch", task_id=task_id
            )
        if (
            task["state"] not in (TaskState.QUEUED, TaskState.RUNNING)
            or task.get("cancel_requested_at")
            or task.get("active_attempt_id")
            or task.get("active_interaction_id")
        ):
            raise MethodPlanInvalid(
                "method dispatch requires an idle execution boundary"
            )
        session = await repo.get_session(str(task["session_id"]))
        if (
            session["wheel"] != Wheel.advisor
            or str(session["active_task_id"]) != task_id
        ):
            raise MethodPlanInvalid("task does not hold the session")
        if any(
            a["status"] not in ATTEMPT_TERMINAL
            for a in await repo.list_attempts(task_id)
        ):
            raise MethodPlanInvalid("another attempt is still unsettled")
        view = await execution_for_task(repo, task)
        if view is None or view.receipt is None:
            raise MethodPlanInvalid("method dispatch requires a dynamic task plan")
        index = int(task["current_step"])
        step = view.pack.step(index)
        if step is None:
            raise MethodPlanInvalid("no next method instance")
        results = await accepted_results(repo, task, view)
        refs = _inputs(view, index, results, dispatch=True)
        opened = await ledger.open_attempt(
            task_id,
            step_id=step.step_id,
            step_version=step.step_version,
            reserve=Decimal(step.reserve),
            input_artifact_versions=refs,
            meta=_meta(view, index),
        )
        inputs = {
            ref["name"]: {
                "version": ref["version"],
                "content": results[ref["instance_id"]]["content"],
            }
            for ref in refs
        }
        return {**opened, "step": step, "inputs": inputs, "execution": view}


async def active_method_step(
    repo: WorkbenchStore, task: dict[str, Any], view: ExecutionPlan
):
    """Read permissions only from the active attempt's accepted plan binding."""
    if view.receipt is None:
        return view.pack.step(int(task["current_step"]))
    if (
        task["state"] != TaskState.RUNNING
        or task.get("cancel_requested_at")
        or not task.get("active_attempt_id")
    ):
        raise MethodPlanInvalid("no active method attempt")
    attempt = await repo.get_attempt(str(task["active_attempt_id"]))
    index = int(task["current_step"])
    step = view.pack.step(index)
    if (
        step is None
        or str(attempt["task_id"]) != str(task["id"])
        or attempt["status"] not in (AttemptState.CREATED, AttemptState.RUNNING)
        or attempt["step_id"] != step.step_id
        or attempt["step_version"] != step.step_version
        or attempt["role"] != "execute"
    ):
        raise MethodPlanInvalid("active method attempt differs from the cursor")
    refs = attempt.get("refs") or {}
    if any(refs.get(k) != v for k, v in _meta(view, index).items()):
        raise MethodPlanInvalid("active method binding changed")
    if attempt["input_artifact_versions"] != _inputs(
        view, index, await accepted_results(repo, task, view)
    ):
        raise MethodPlanInvalid("active method inputs changed")
    return step


async def active_method_inputs(
    repo: WorkbenchStore, task: dict[str, Any], view: ExecutionPlan
) -> dict[str, Any]:
    """The exact inputs of the current attempt, for deterministic acceptance.

    Check the plan/lineage first, then fetch each registered version, never the
    latest artifact with that name. A stale or unrelated attempt is not usable.
    """
    if view.receipt is None:
        raise MethodPlanInvalid("method inputs require a dynamic task plan")
    await active_method_step(repo, task, view)
    attempt = await repo.get_attempt(str(task["active_attempt_id"]))
    inputs = {}
    for ref in attempt["input_artifact_versions"]:
        artifact = await repo.get_artifact(
            task_id=str(task["id"]), name=ref["name"], version=ref["version"]
        )
        if (
            artifact is None
            or str(artifact["id"]) != ref["id"]
            or artifact["digest"] != ref["digest"]
        ):
            raise MethodPlanInvalid("bound input artifact changed")
        inputs[ref["name"]] = artifact["content"]
    return inputs
