"""Scoped directory evidence, verified proposals and user selection on the match task."""

import json

from app.domain.workbench.errors import IdempotencyConflict, NotFound, StaleStateVersion
from app.domain.workbench.expert_match import digest
from app.domain.workbench.formal_expert_match import verify_options
from app.domain.workbench.formal_method_generated import MethodRef
from app.domain.workbench.method_search import PlanningQuery

DIRECTORY = "task/formal_expert_directory"
SELECTION = "task/formal_expert_selected"
REVISION = "task/formal_expert_revised"


async def previous_round(repo, owner, project_id, task_id):
    previous = await repo.get_task(task_id, owner_actor_id=owner, for_update=True)
    if (
        previous["profile_id"] != "FIND_EXPERT"
        or str(previous["profile_version"]) != "5"
        or str(previous["project_id"]) != project_id
    ):
        raise NotFound("No such project match")
    if previous["state"] not in ("SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"):
        raise StaleStateVersion("Finish or stop the previous matching round first")
    if await repo.list_task_events(task_id, types=(REVISION,)):
        raise StaleStateVersion("A newer revision already exists")
    return previous


async def directory_for(registry, owner, question, named):
    if named:
        refs, more = [named], False
    else:
        page = await registry.planning_versions(
            owner, 8, 0, query=PlanningQuery(stage="expert", question=question)
        )
        refs = [
            MethodRef.model_validate(registry.reference(row)) for row in page["items"]
        ]
        more = page["next"] is not None
    items = []
    used = 0
    for ref in refs:
        entry = await registry.planning_declaration(owner, ref, experts_only=True)
        size = len(json.dumps(entry, ensure_ascii=False).encode())
        if used + size > 64000:
            if not items:
                raise StaleStateVersion(
                    "Expert declaration exceeds the matching context"
                )
            more = True
            break
        items.append(entry)
        used += size
    return {"items": items, "more": more}


async def snapshot(repo, task):
    events = await repo.list_task_events(str(task["id"]), types=(DIRECTORY,))
    if len(events) != 1:
        raise ValueError("Matching requires a unique authorized directory")
    return events[0]["payload"]


async def verified_result(repo, task, artifact):
    directory = await snapshot(repo, task)
    question = task["original_input"]["input"]["text"]
    match = verify_options(question, directory, artifact["content"])
    return {
        "question": question,
        "match": match.model_dump(mode="json"),
        "more": directory["more"],
        "review_digest": digest(
            [
                str(task["id"]),
                question,
                directory,
                artifact["version"],
                artifact["content"],
            ]
        ),
        "options": [
            e
            for e in directory["items"]
            if any(
                p.reference.model_dump(mode="json") == e["reference"]
                for p in match.picks
            )
        ],
    }


async def read_verified(repo, task, artifact):
    if artifact is None:
        raise StaleStateVersion("Missing matching result")
    try:
        return await verified_result(repo, task, artifact)
    except (ValueError, KeyError) as exc:
        raise StaleStateVersion("Invalid matching evidence") from exc


async def check_current(registry, owner, option):
    current = await registry.planning_declaration(
        owner, MethodRef.model_validate(option["reference"]), experts_only=True
    )
    if current != option:
        raise StaleStateVersion("Expert declaration changed")


async def review(repo, registry, owner, task, artifact):
    async with repo.transaction():
        result = await read_verified(repo, task, artifact)
    for option in result["options"]:
        await check_current(registry, owner, option)
    selections = await repo.list_task_events(str(task["id"]), types=(SELECTION,))
    if (
        selections
        and selections[0]["payload"]["review_digest"] != result["review_digest"]
    ):
        raise StaleStateVersion("Selection belongs to an earlier matching result")
    return {
        **result,
        "selected": selections[0]["payload"]["reference"] if selections else None,
    }


async def confirm(repo, registry, owner, task_id, body):
    async with repo.transaction():
        task = await repo.get_task(task_id, owner_actor_id=owner)
        if task["profile_id"] != "FIND_EXPERT" or str(task["profile_version"]) != "5":
            raise NotFound("No such formal expert match")
        if task["state"] != "SUCCEEDED":
            raise StaleStateVersion("Matching has not succeeded")
        artifact = await repo.get_artifact(task_id=task_id, name="find_expert")
        result = await read_verified(repo, task, artifact)
    ref = body.reference.model_dump(mode="json")
    option = next((e for e in result["options"] if e["reference"] == ref), None)
    if body.review_digest != result["review_digest"] or option is None:
        raise StaleStateVersion(
            "Selection differs from the reviewed question and recommendation"
        )
    await check_current(registry, owner, option)
    payload = {"review_digest": body.review_digest, "reference": ref}
    async with repo.transaction():
        await repo.get_task(task_id, owner_actor_id=owner, for_update=True)
        if await repo.list_task_events(task_id, types=(REVISION,)):
            raise StaleStateVersion("The question has been revised")
        latest = await repo.get_artifact(task_id=task_id, name="find_expert")
        if (
            latest is None
            or latest["version"] != artifact["version"]
            or latest["content"] != artifact["content"]
        ):
            raise StaleStateVersion("The reviewed result has changed")
        prior = await repo.list_task_events(task_id, types=(SELECTION,))
        if prior:
            if prior[0]["payload"] != payload:
                raise IdempotencyConflict("A different expert was already selected")
        else:
            await repo.append_event(
                session_id=str(task["session_id"]),
                kind="task",
                event_type=SELECTION,
                task_id=task_id,
                payload=payload,
            )
    return payload
