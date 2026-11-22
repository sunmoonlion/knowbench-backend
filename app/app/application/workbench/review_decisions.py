"""Owner decisions on exact excerpts of sealed review artifacts."""

from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.application.workbench.formal_orchestration_execution import read_step_output
from app.application.workbench.review_comparison import COMPARISON_STEP
from app.domain.workbench.errors import NotFound, StaleStateVersion
from app.domain.workbench.formal_method_generated import Digest, Strict
from app.domain.workbench.source_tasks import PACK_SOURCE, SourceTask
from app.domain.workbench.states import TASK_TERMINAL, AttemptState, TaskState


class FindingDecision(Strict):
    artifact_id: UUID
    quote: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    decision: Literal["accepted", "declined", "deferred"]
    reason: str = Field(min_length=1, max_length=1000, pattern=r"\S")

    @field_validator("quote", "reason")
    @classmethod
    def database_text(cls, value):
        if "\x00" in value:
            raise ValueError("Text contains a null character")
        return value


class OwnerDecision(Strict):
    previous_revision: int = Field(strict=True, ge=0)
    review_digest: Digest
    items: list[FindingDecision] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_quotes(self):
        if len({(i.artifact_id, i.quote) for i in self.items}) != len(self.items):
            raise ValueError("Each finding takes one decision")
        return self


async def sealed_reports(repo, task, rounds):
    task_id = str(task["id"])
    attempts = await repo.list_attempts(task_id)
    stages = ["direct", COMPARISON_STEP] if rounds == 2 else ["direct"]
    reports, missing = [], []
    for step in stages:
        matches = [
            a
            for a in attempts
            if a["step_id"] == step and a["status"] == AttemptState.COMPLETED
        ]
        if not matches and task["state"] != TaskState.SUCCEEDED:
            missing.append(step)
            continue
        if len(matches) != 1 or missing:
            raise StaleStateVersion("Inconsistent sealed review stages")
        attempt = matches[0]
        versions, refs = attempt["output_artifacts"], attempt["refs"]
        result = refs.get("formal_result") or {}
        usage = refs.get("instruction_usage") or {}
        if (
            result.get("outcome") != "succeeded"
            or not usage.get("turn_id")
            or usage["turn_id"] != refs.get("runtime_turn_id")
            or attempt["step_version"] != task["pack_digest"]
        ):
            raise StaleStateVersion("Review stage has no settled completion receipt")
        outputs = await read_step_output(
            repo, task_id, str(attempt["id"]), "formal-step:" + step, versions, result
        )
        reports.append({**versions[0], "step": step, "text": outputs["result"]})
    return reports, missing


async def decision_page(repo, owner, task_id, *, command=None, revision=None):
    async with repo.transaction():
        task = await repo.get_task(
            task_id, owner_actor_id=owner, for_update=command is not None
        )
        if task["pack_source"] != PACK_SOURCE:
            raise NotFound("No such review")
        snap = SourceTask.load(task)
        binding = snap.review
        if binding is None:
            raise NotFound("No such review")
        if task["state"] not in TASK_TERMINAL:
            raise StaleStateVersion("Review is still in progress")
        reports, missing = await sealed_reports(repo, task, snap.review_rounds)
        evidence = {
            "owner": owner,
            "task_state": task["state"],
            "missing_stages": missing,
            "review_digest": binding.review_digest,
            "artifacts": [
                {k: r[k] for k in ("id", "version", "digest")} for r in reports
            ],
        }
        latest = await repo.review_decision(task_id)
        current = latest["revision"] if latest else 0
        if latest and any(latest["receipt"].get(k) != v for k, v in evidence.items()):
            raise StaleStateVersion(
                "Sealed evidence differs from the recorded decision"
            )

        if command is not None:
            texts = {r["id"]: r["text"] for r in reports}
            if binding.review_digest != command.review_digest or any(
                i.quote not in texts.get(str(i.artifact_id), "") for i in command.items
            ):
                raise StaleStateVersion(
                    "Decision does not cite a sealed review finding"
                )
            receipt = {
                **evidence,
                "items": [i.model_dump(mode="json") for i in command.items],
            }
            unchanged = latest is not None and latest["receipt"] == receipt
            replay = unchanged and command.previous_revision == current - 1
            if command.previous_revision != current and not replay:
                raise StaleStateVersion(
                    "Owner decisions changed; reload the latest revision"
                )
            if not unchanged:
                current += 1
                await repo.append_review_decision(task_id, current, receipt)
                latest = await repo.review_decision(task_id, current)
        record = (
            latest
            if revision is None
            else await repo.review_decision(task_id, revision)
        )
        if revision is not None and not record:
            raise NotFound("No such decision revision")
        return {
            "review_digest": binding.review_digest,
            "reports": reports,
            "missing_stages": missing,
            "task_state": task["state"],
            "latest_revision": current,
            "revision": record["revision"] if record else 0,
            "recorded_at": record["created_at"] if record else None,
            "items": record["receipt"]["items"] if record else [],
        }
