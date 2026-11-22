"""Transfer the approved problem and interface gap, and recover an interrupted transfer."""

from uuid import UUID

from pydantic import Field

from app.application.workbench.formal_expert_intake import (
    ENDED,
    REVISION,
    SELECTION,
    read_verified,
)
from app.domain.workbench.errors import NotFound, StaleStateVersion
from app.domain.workbench.formal_method_generated import Digest, MethodRef, Strict

INTENT = "task/formal_handoff_requested"
RECEIPT = "task/formal_handoff_received"


class Handoff(Strict):
    review_digest: Digest
    gap: str = Field(min_length=1, max_length=2000)
    reference: MethodRef | None = None
    plan_digest: Digest | None = None


async def context(repo, task, review_digest):
    from app.application.workbench.temporary_planning import PLAN, SAVED

    task_id = str(task["id"])
    intent = await repo.latest_task_payload(task_id, INTENT)
    saved = await repo.latest_task_payload(task_id, SAVED)
    plan = await repo.latest_task_payload(task_id, PLAN) if saved else None
    selection = await repo.latest_task_payload(task_id, SELECTION)
    return {
        "review_digest": review_digest,
        "handoff": await repo.latest_task_payload(task_id, RECEIPT),
        "handoff_gap": intent["gap"] if intent else None,
        "handoff_reference": selection["reference"]
        if selection
        else task["original_input"]["input"].get("reference"),
        "suggested_plan": {
            "sketch_digest": plan["sketch_digest"],
            "methods": plan["steps"],
            "outputs": [item["name"] for item in plan["scope"]["outputs"]],
        }
        if plan and saved["sketch_digest"] == plan["sketch_digest"]
        else None,
        "attached_plan": bool(intent and intent.get("suggested_plan")),
    }


async def transfer(service, owner, task_id, command):
    repo = service.repo
    async with repo.transaction():
        await repo.get_task(task_id, owner_actor_id=owner)
        pending = await repo.latest_task_payload(task_id, INTENT)
    if not pending and command.reference:
        await service.formal_registry.planning_declaration(
            owner, command.reference, experts_only=True
        )
    async with repo.transaction():
        task = await repo.get_task(task_id, owner_actor_id=owner, for_update=True)
        if task["profile_id"] != "FIND_EXPERT" or str(task["profile_version"]) != "5":
            raise NotFound("No such formal request")
        if (
            task["state"] != "SUCCEEDED"
            or await repo.latest_task_payload(task_id, REVISION)
            or await repo.latest_task_payload(task_id, ENDED)
        ):
            raise StaleStateVersion("This request is no longer open")
        artifact = await repo.get_artifact(task_id=task_id, name="find_expert")
        review = await read_verified(repo, task, artifact)
        selected = await repo.latest_task_payload(task_id, SELECTION)
        given = task["original_input"]["input"]
        ref = selected["reference"] if selected else given.get("reference")
        if (
            command.review_digest != review["review_digest"]
            or not command.gap.strip()
            or (
                command.reference.model_dump(mode="json") if command.reference else None
            )
            != ref
            or not selected
            and review["match"]["outcome"] != "no_match"
        ):
            raise StaleStateVersion(
                "The gap or method differs from this reviewed request"
            )
        subject = await repo.subject_of_actor(owner)
        if not subject:
            raise NotFound("No such owner")
        attachment = (
            (await context(repo, task, command.review_digest))["suggested_plan"]
            if command.plan_digest
            else None
        )
        if command.plan_digest and (
            not attachment or attachment["sketch_digest"] != command.plan_digest
        ):
            raise StaleStateVersion("The saved plan differs from the approved proposal")
        payload = {
            "contract": "knowlab/formal-handoff/v1",
            "owner_subject": subject,
            "project_id": str(task["project_id"]),
            "task_id": task_id,
            "key": "knowbench:formal-handoff:" + task_id,
            "question": given["text"],
            **command.model_dump(mode="json", exclude={"plan_digest"}),
            **({"suggested_plan": attachment} if attachment else {}),
        }
        prior = await repo.latest_task_payload(task_id, INTENT)
        if prior and prior != payload:
            raise StaleStateVersion(
                "A different handoff was already approved for this request"
            )
        saved = await repo.latest_task_payload(task_id, RECEIPT)
        if saved:
            return saved
        if service.incubations is None:
            raise StaleStateVersion("KnowLab is unavailable")
        if not prior:
            await repo.append_event(
                session_id=str(task["session_id"]),
                task_id=task_id,
                kind="task",
                event_type=INTENT,
                payload=payload,
            )
    remote = await service.incubations.submit(payload)
    try:
        mode = remote["handoff"]
        if mode not in (
            {"new"} if not command.reference else {"author_revision", "user_demand"}
        ):
            raise ValueError("Unexpected handoff mode")
        receipt = {
            "request_id": str(UUID(remote["request_id"])),
            "mode": mode,
            "review_digest": command.review_digest,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise StaleStateVersion("Could not verify the remote handoff receipt") from exc
    async with repo.transaction():
        await repo.get_task(task_id, owner_actor_id=owner, for_update=True)
        saved = await repo.latest_task_payload(task_id, RECEIPT)
        if saved and saved != receipt:
            raise StaleStateVersion("Conflicting KnowLab receipt")
        if not saved:
            await repo.append_event(
                session_id=str(task["session_id"]),
                task_id=task_id,
                kind="task",
                event_type=RECEIPT,
                payload=receipt,
            )
        return receipt
