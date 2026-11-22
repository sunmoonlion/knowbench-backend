"""Scoped directory evidence, verified proposals and user selection on the match task."""

import json

from app.domain.workbench.errors import IdempotencyConflict, NotFound, StaleStateVersion
from app.domain.workbench.expert_match import digest
from app.domain.workbench.formal_expert_match import frozen_scope, verify_options
from app.domain.workbench.formal_method_generated import MethodRef
from app.domain.workbench.method_search import PlanningQuery

DIRECTORY = "task/formal_expert_directory"
SELECTION = "task/formal_expert_selected"
REVISION = "task/formal_expert_revised"
ENDED = "task/formal_expert_ended"


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
    if await repo.latest_task_payload(task_id, ENDED):
        raise StaleStateVersion("This expert request has ended")
    return previous


async def directory_for(registry, owner, question, named, *, offset=0):
    if named:
        refs, more = [named], False
    else:
        page = await registry.planning_versions(
            owner, 8, offset, query=PlanningQuery(stage="expert", question=question)
        )
        if not page["items"]:
            return {"items": [], "next": page["next"]}
        refs = [
            MethodRef.model_validate(registry.reference(row)) for row in page["items"]
        ]
        more = offset > 0 or page["next"] is not None
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


async def selected_definition(registry, owner, result, selections):
    receipt = selections[0]["payload"]
    selected = receipt["reference"]
    options = [o for o in result["options"] if o["reference"] == selected]
    if (
        len(selections) != 1
        or receipt["review_digest"] != result["review_digest"]
        or len(options) != 1
    ):
        raise StaleStateVersion("Invalid selection for this matching result")
    ref = MethodRef.model_validate(selected)
    definition = await registry.planning_definition(owner, ref, experts_only=True)
    if registry.public_declaration(ref, definition) != options[0]:
        raise StaleStateVersion("Selected declaration changed")
    return selected, definition, options


async def review(repo, registry, owner, task, artifact):
    async with repo.transaction():
        result = await read_verified(repo, task, artifact)
        selections = await repo.list_task_events(str(task["id"]), types=(SELECTION,))
    selected = None
    preview = None
    if selections:
        selected, definition, options = await selected_definition(
            registry, owner, result, selections
        )
        preview = frozen_scope(definition)
        result["options"] = options
        result["match"]["picks"] = [
            p for p in result["match"]["picks"] if p["reference"] == selected
        ]
    else:
        for option in result["options"]:
            await check_current(registry, owner, option)
    async with repo.transaction():
        if await repo.list_task_events(str(task["id"]), types=(REVISION,)):
            raise StaleStateVersion("The question has been revised")
        ended = await repo.latest_task_payload(str(task["id"]), ENDED)
        if ended is not None and ended.get("review_digest") != result["review_digest"]:
            raise StaleStateVersion("The closed matching result has changed")
    return {
        **result,
        "selected": selected,
        "scope_preview": preview,
        "ended": ended is not None,
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
        if await repo.latest_task_payload(task_id, ENDED):
            raise StaleStateVersion("This expert request has ended")
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


async def end_unmatched(repo, owner, task_id, review_digest):
    """Close only this reviewed no-match request, preserving its matching history."""
    async with repo.transaction():
        task = await repo.get_task(task_id, owner_actor_id=owner, for_update=True)
        if task["profile_id"] != "FIND_EXPERT" or str(task["profile_version"]) != "5":
            raise NotFound("No such formal expert match")
        if task["state"] != "SUCCEEDED" or await repo.latest_task_payload(
            task_id, REVISION
        ):
            raise StaleStateVersion("This is not the current completed matching round")
        artifact = await repo.get_artifact(task_id=task_id, name="find_expert")
        result = await read_verified(repo, task, artifact)
        if (
            result["review_digest"] != review_digest
            or result["match"]["outcome"] != "no_match"
        ):
            raise StaleStateVersion(
                "This result does not establish the reviewed expert gap"
            )
        receipt = {"review_digest": review_digest}
        previous = await repo.latest_task_payload(task_id, ENDED)
        if previous is None:
            await repo.append_event(
                session_id=str(task["session_id"]),
                kind="task",
                event_type=ENDED,
                task_id=task_id,
                payload=receipt,
            )
        elif previous != receipt:
            raise StaleStateVersion("The closed matching result has changed")
        return receipt
