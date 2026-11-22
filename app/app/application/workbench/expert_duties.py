"""Each period opens an ordinary request on one explicitly released prior output."""

import hashlib
import json
from datetime import timedelta
from uuid import UUID

from pydantic import Field

from app.application.workbench.find_expert import QUESTION_CHARS
from app.application.workbench.outputs import _basis_echoed, task_ref
from app.domain.workbench.errors import NotFound, StaleStateVersion
from app.domain.workbench.formal_execution import host_snapshot
from app.domain.workbench.formal_method_generated import MethodRef, Strict
from app.domain.workbench.ordinary_expert import CONFIRMED, ConsumerPlan, MaterialPin


class DutyConfig(Strict):
    instruction: str = Field(
        min_length=1,
        max_length=QUESTION_CHARS - len("\n9999-12-31T23:59:59.999999+00:00"),
        pattern=r"\S",
    )
    interval_hours: int = Field(strict=True, ge=1)
    material_slot: str | None = Field(default=None, min_length=1, max_length=100)
    output: str = Field(min_length=1, max_length=100)


class ReleaseOutput(Strict):
    execution_id: UUID


def next_period(start, hours):
    try:
        return start + timedelta(hours=hours)
    except OverflowError as exc:
        raise StaleStateVersion(
            "The next period exceeds the supported date range"
        ) from exc


def view(row):
    if not row:
        return None
    return {
        "origin": str(row["origin"]),
        "current_match": str(row["current_match"]),
        "config": row["config"],
        "approved": row["approved"],
        "paused": row["paused"],
        "next_at": row["next_at"].isoformat(),
        "problem": row["problem"],
    }


async def current_delivery(repo, owner, match_id):
    match = await repo.get_task(match_id, owner_actor_id=owner)
    receipt = await repo.latest_task_payload(match_id, CONFIRMED)
    if not receipt:
        raise StaleStateVersion(
            "Confirm and complete the original expert request first"
        )
    task = await repo.get_task(receipt["task_id"], owner_actor_id=owner)
    plan = host_snapshot(task)
    if (
        not isinstance(plan, ConsumerPlan)
        or plan.temporary
        or str(plan.match_task_id) != match_id
        or task["state"] != "SUCCEEDED"
        or str(task["session_id"]) != str(match["session_id"])
    ):
        raise StaleStateVersion("A completed published expert version is required")
    delivery = await repo.get_artifact(
        task_id=str(task["id"]), name="expert_delivery", version=1
    )
    if not delivery or delivery["id"] != task["terminal_result_ref"]:
        raise StaleStateVersion("The accepted delivery is missing")
    return task, plan, delivery


async def configure(service, owner, origin, body):
    repo = service.repo
    async with repo.transaction():
        task = await repo.get_task(origin, owner_actor_id=owner)
        await repo.get_project(
            str(task["project_id"]), owner_actor_id=owner, for_update=True
        )
        await repo.get_task(origin, owner_actor_id=owner, for_update=True)
        task, plan, delivery = await current_delivery(repo, owner, origin)
        if body.output not in delivery["content"]:
            raise StaleStateVersion("Choose a delivered output")
        if body.material_slot:
            from app.application.workbench.formal_expert_preparation import PREPARATION

            saved = await repo.latest_task_payload(origin, PREPARATION)
            if not saved or body.material_slot not in saved["bindings"]["materials"]:
                raise StaleStateVersion(
                    "Select a verified material input from the original execution"
                )
        config = {
            **body.model_dump(),
            "reference": plan.reference.model_dump(mode="json"),
        }
        row = await repo.duty(origin, lock=True)
        if row:
            if row["config"] != config:
                raise StaleStateVersion("This request already has a different duty")
        else:
            await repo.create_duty(
                origin, config, next_period(service.now(), body.interval_hours)
            )
        return view(await repo.duty(origin))


async def release(service, owner, origin, execution_id, library):
    repo = service.repo
    async with repo.transaction():
        await repo.get_task(origin, owner_actor_id=owner)
        row = await repo.duty(origin, lock=True)
        if not row or row["paused"]:
            raise StaleStateVersion("An active duty is required")
        task, plan, artifact = await current_delivery(
            repo, owner, str(row["current_match"])
        )
        config = row["config"]
        if (
            str(task["id"]) != execution_id
            or plan.reference.model_dump(mode="json") != config["reference"]
        ):
            raise StaleStateVersion("Review the current delivery first")
        if row["approved"]:
            return view(row)
        field = config["output"]
        if field not in artifact["content"]:
            raise StaleStateVersion("The designated output was not delivered")
        value = artifact["content"][field]
        content = (
            value
            if isinstance(value, str)
            else json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
        ).encode()
        if not content:
            raise StaleStateVersion("An empty delivery cannot be released")
        subject = await repo.subject_of_actor(owner)
        if not subject:
            raise NotFound("No such owner")
        where = {
            "beneficiary": subject,
            "task_ref": task_ref(execution_id),
            "run_ref": str(artifact["id"]),
            "name": field + ".txt",
        }
        started, finished = (
            task["created_at"].isoformat(),
            task["updated_at"].isoformat(),
        )
        written = await library.write_output(
            **where,
            content=content,
            expert_id=str(plan.reference.method_id),
            expert_version=str(plan.reference.version),
            task_started_at=started,
            task_finished_at=finished,
        )
        pin = MaterialPin.model_validate(
            {k: written.get(k) for k in MaterialPin.model_fields}
        )
        if pin.sha256 != hashlib.sha256(content).hexdigest() or not _basis_echoed(
            written, None, started, finished
        ):
            raise StaleStateVersion("KnowBase did not attest the approved output")
        await repo.approve_duty_output(
            origin,
            {
                "material": pin.model_dump(mode="json"),
                "execution": execution_id,
                "artifact": str(artifact["id"]),
                "output": field,
            },
        )
        return view(await repo.duty(origin))


async def withdraw(service, owner, origin, execution_id):
    repo = service.repo
    async with repo.transaction():
        await repo.get_task(origin, owner_actor_id=owner)
        row = await repo.duty(origin, lock=True)
        if not row:
            raise NotFound("No such duty")
        receipt = await repo.latest_task_payload(str(row["current_match"]), CONFIRMED)
        if not receipt or receipt["task_id"] != execution_id:
            raise StaleStateVersion("The duty has already advanced to another period")
        await repo.approve_duty_output(origin, None)
        return view(await repo.duty(origin))


async def set_paused(service, owner, origin, paused):
    repo = service.repo
    async with repo.transaction():
        await repo.get_task(origin, owner_actor_id=owner)
        if not await repo.duty(origin, lock=True):
            raise NotFound("No such duty")
        await repo.pause_duty(origin, paused)
        return view(await repo.duty(origin))


async def check_released_output(library, subject, prior):
    live = await library.find_output(
        beneficiary=subject,
        task_ref=task_ref(prior["execution"]),
        run_ref=prior["artifact"],
        name=prior["output"] + ".txt",
        require_available=True,
    )
    if live != prior["material"]:
        raise StaleStateVersion("The released material version is unavailable")


async def wake(service, owner, origin, period, library):
    repo = service.repo
    async with repo.transaction():
        task = await repo.get_task(origin, owner_actor_id=owner)
        await repo.get_project(
            str(task["project_id"]), owner_actor_id=owner, for_update=True
        )
        row = await repo.duty(origin, lock=True)
        if (
            not row
            or row["paused"]
            or row["next_at"] != period
            or period > service.now()
            or not row["approved"]
        ):
            return None
        config, prior = row["config"], row["approved"]
        ref = MethodRef.model_validate(config["reference"])
        subject = await repo.subject_of_actor(owner)
        await check_released_output(library, subject, prior)
        definitions = await service.formal_registry.planning_definitions(
            owner, [ref], experts_only=True, locked=True
        )
        if ref not in definitions:
            raise StaleStateVersion(
                "Use of this duty's expert version has been revoked"
            )
        declaration = service.formal_registry.public_declaration(ref, definitions[ref])
        made = await service.start(
            owner_actor_id=owner,
            project_id=str(task["project_id"]),
            question=config["instruction"] + "\n" + period.isoformat(),
            idempotency_key=f"duty:{origin}:{period.isoformat()}",
            reference=ref,
            match_contract="knowbench/expert-match/v5",
            source_session=task["original_input"]["input"].get("source_session"),
            followup={
                "kind": "duty",
                "origin": origin,
                "config_revision": 1,
                "previous": str(row["current_match"]),
                "period": period.isoformat(),
                "released": prior,
                "material_slot": config.get("material_slot"),
            },
            checked_directory={"items": [declaration], "more": False},
        )
        await repo.advance_duty(
            origin, made["task_id"], next_period(period, config["interval_hours"])
        )
        return made
