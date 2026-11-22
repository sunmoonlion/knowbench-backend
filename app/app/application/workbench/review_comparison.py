"""Seal an independent finding and release the deferred assessment to its comparison only."""

from decimal import Decimal

from app.application.workbench.formal_orchestration_execution import read_step_output
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.source_tasks import SourceTask
from app.domain.workbench.states import AttemptState

COMPARISON_STEP = "review-comparison"
COMPARISON_GUIDANCE = (
    "Compare the sealed independent findings with the author's assessment using the approved outputs. "
    "Identify agreements, disagreements and their evidence. Leave unresolved decisions to the task owner."
)


async def open_comparison(ledger, task, first, versions):
    # The caller locks the task and seals the artifact and first usage in this transaction.
    refs = first["refs"]
    quote = refs["instruction_quote"]
    second = await ledger.open_attempt(
        str(task["id"]),
        step_id=COMPARISON_STEP,
        step_version=task["pack_digest"],
        input_artifact_versions=versions,
        reserve=Decimal(quote["maximum"]),
        meta={
            "instruction_unit": {"pending": True, "scope": "attempt"},
            "instruction_quote": quote,
            "instruction_output_version": 1,
            "model": quote["model"],
            "model_provider": quote["provider"],
            "independent_review": {
                "attempt_id": str(first["id"]),
                "gateway": refs["instruction_gateway"]["uid"],
                **refs["instruction_thread"],
            },
        },
    )
    await ledger.attempt_transition(second["attempt_id"], AttemptState.RUNNING)


async def comparison_arguments(repo, owner, binding, task_id, approved, assessment):
    task = await repo.get_task(task_id, owner_actor_id=owner, for_update=True)
    snap = SourceTask.load(task)
    if (
        snap.review != binding
        or snap.review_rounds != 2
        or not task.get("active_attempt_id")
    ):
        raise CapabilityError("Comparison does not belong to the confirmed task")
    second = await repo.get_attempt(str(task["active_attempt_id"]))
    seal = second["refs"].get("independent_review")
    if (
        not seal
        or second["step_id"] != COMPARISON_STEP
        or second["status"] != AttemptState.RUNNING
        or str(second["task_id"]) != task_id
    ):
        raise CapabilityError("Comparison attempt is not active")
    first = await repo.get_attempt(seal["attempt_id"])
    refs = first["refs"]
    if (
        first["status"] != AttemptState.COMPLETED
        or str(first["task_id"]) != task_id
        or refs.get("instruction_thread")
        != {"worker": seal["worker"], "thread": seal["thread"]}
        or refs.get("instruction_gateway", {}).get("uid") != seal["gateway"]
        or first["step_id"] != "direct"
        or first["step_version"] != task["pack_digest"]
        or first["output_artifacts"] != second["input_artifact_versions"]
        or not refs.get("instruction_usage")
        or refs["instruction_usage"]["turn_id"] != refs.get("runtime_turn_id")
        or refs.get("instruction_quote") != second["refs"].get("instruction_quote")
        or refs.get("formal_result", {}).get("outcome") != "succeeded"
    ):
        raise CapabilityError(
            "Independent findings have not been sealed for this review"
        )
    findings = await read_step_output(
        repo,
        task_id,
        seal["attempt_id"],
        "formal-step:direct",
        first["output_artifacts"],
        refs["formal_result"],
    )
    return {
        "approved_outputs": approved,
        "independent_findings": findings["result"],
        "author_assessment": assessment,
    }
