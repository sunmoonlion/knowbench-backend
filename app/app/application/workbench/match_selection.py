"""Resolve a browser selection from durable matching evidence before data grants."""

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.find_expert import (
    ExpertDirectory,
    ExpertDirectoryUnavailable,
    FindExpertRefused,
    match_receipt,
)
from app.domain.workbench.expert_match import MatchSelection
from app.domain.workbench.knowbots import parse_profile
from app.domain.workbench.packs import FIND_EXPERT
from app.domain.workbench.states import TaskState
from app.domain.workbench.task_requirements import TaskRequirements, from_match


async def resolve_selection(
    repo: WorkbenchStore,
    directory: ExpertDirectory | None,
    selection: MatchSelection,
    *,
    owner_actor_id: str,
    project_id: str,
    profile_id: str,
    question: str,
    expert_digest: str,
) -> TaskRequirements | None:
    key = (selection.knowbot_id, selection.version)
    if parse_profile(profile_id) != key:
        raise FindExpertRefused("selection names a different expert")
    task = await repo.get_task(selection.match_task_id, owner_actor_id=owner_actor_id)
    original = (task.get("original_input") or {}).get("input") or {}
    if (
        task["profile_id"] != FIND_EXPERT.profile_id
        or str(task["profile_version"]) not in {"2", "3", "4"}
        or task["state"] != TaskState.SUCCEEDED
        or str(task.get("project_id")) != project_id
        or original.get("text") != question
    ):
        raise FindExpertRefused(
            "matching belongs to another question, project or unfinished round"
        )
    artifact = await repo.get_artifact(
        task_id=selection.match_task_id,
        name="find_expert",
        version=selection.match_artifact_version,
    )
    latest = await repo.get_artifact(
        task_id=selection.match_task_id, name="find_expert"
    )
    if artifact is None or latest is None or artifact["version"] != latest["version"]:
        raise FindExpertRefused("matching artifact is missing or superseded")
    receipt = await match_receipt(repo, task, artifact)
    if (
        receipt.question_revision != selection.question_revision
        or receipt.result.outcome not in {"summon", "candidates"}
    ):
        raise FindExpertRefused("matching is stale or does not authorize an invitation")
    if not any((pick.knowbot_id, pick.version) == key for pick in receipt.result.picks):
        raise FindExpertRefused("expert is not a verified candidate")
    expected = next(
        entry
        for entry in receipt.directory.experts
        if (entry.knowbot_id, entry.version) == key
    )
    if expected.digest != expert_digest:
        raise FindExpertRefused("pinned expert differs from matched version")
    subject = await repo.subject_of_actor(owner_actor_id)
    if directory is None or not directory.configured() or subject is None:
        raise ExpertDirectoryUnavailable("directory recheck is unavailable")
    current = await directory.snapshot(subject)
    if not current.complete:
        raise ExpertDirectoryUnavailable("directory recheck is incomplete")
    live = next(
        (
            entry
            for entry in current.experts
            if (entry.knowbot_id, entry.version) == key
        ),
        None,
    )
    if live != expected:
        raise FindExpertRefused(
            "expert availability, terms or declaration changed; match again"
        )
    if expected.runtime_contract != "knowbench/dynamic-methods/v3":
        return None
    return from_match(
        receipt,
        match_task_id=selection.match_task_id,
        match_artifact_version=selection.match_artifact_version,
        knowbot_id=selection.knowbot_id,
        version=selection.version,
        expert_digest=expert_digest,
        question=question,
    )
