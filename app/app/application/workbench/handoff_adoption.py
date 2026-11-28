"""Review an acquired, frozen revision from the original KnowLab handoff."""

from app.application.workbench.formal_expert_intake import DIRECTORY
from app.application.workbench.formal_handoff import INTENT, RECEIPT
from app.domain.workbench.errors import NotFound, StaleStateVersion
from app.domain.workbench.formal_execution import host_snapshot
from app.domain.workbench.formal_expert_match import frozen_scope
from app.domain.workbench.formal_method_generated import Digest, MethodRef, Strict
from app.domain.workbench.match_requirements import digest
from app.domain.workbench.ordinary_expert import CONFIRMED, ConsumerPlan

ADOPTED = "task/handoff_version_adopted"


class AdoptVersion(Strict):
    reference: MethodRef
    review_digest: Digest


async def available(service, owner, origin):
    repo = service.repo
    async with repo.transaction():
        await repo.get_task(origin, owner_actor_id=owner)
        receipt = await repo.latest_task_payload(origin, RECEIPT)
        intent = await repo.latest_task_payload(origin, INTENT)
        if not receipt or not intent:
            raise NotFound("No completed handoff for this request")
        prior = intent["reference"]
        directory = await repo.latest_task_payload(origin, DIRECTORY)
        previous = next(
            (o for o in directory["items"] if o["reference"] == prior), None
        )
        old_scope = None
        if execution := await repo.latest_task_payload(origin, CONFIRMED):
            old_task = await repo.get_task(execution["task_id"], owner_actor_id=owner)
            plan = host_snapshot(old_task)
            if isinstance(plan, ConsumerPlan) and prior == plan.reference.model_dump(
                mode="json"
            ):
                old_scope = frozen_scope(plan.draft.definition)
        existing = [
            e["payload"] for e in await repo.list_task_events(origin, types=(ADOPTED,))
        ]
    cursor = ("00000000-0000-0000-0000-000000000000", 0)
    options = []
    registry = service.formal_registry
    while True:
        async with repo.transaction():
            rows = await repo.handoff_versions(receipt["request_id"], prior, cursor)
        if not rows:
            break
        refs = [MethodRef.model_validate(row) for row in rows]
        eligible = await registry.planning_definitions(owner, refs, experts_only=True)
        for ref in refs:
            if ref not in eligible or (
                prior
                and (
                    str(ref.method_id) != prior["method_id"]
                    or ref.version <= prior["version"]
                )
            ):
                continue
            candidate = registry.public_declaration(ref, eligible[ref])
            scope = frozen_scope(eligible[ref])
            before = {
                **(previous["declaration"] if previous else {}),
                **(old_scope or {}),
            }
            after = {**candidate["declaration"], **scope}
            changes = {
                key: {"before": before.get(key), "after": after.get(key)}
                for key in sorted(before.keys() | after.keys())
                if before.get(key) != after.get(key)
            }
            review = digest(
                [origin, receipt, intent["gap"], previous, candidate, changes]
            )
            options.append(
                {"candidate": candidate, "review_digest": review, "changes": changes}
            )
        if len(rows) < 100:
            break
        cursor = (str(refs[-1].method_id), refs[-1].version)
    return {"previous": previous, "options": options, "adopted": existing}


async def adopt(service, owner, origin, body):
    repo = service.repo
    key = body.model_dump(mode="json")

    async def replay():
        for e in await repo.list_task_events(origin, types=(ADOPTED,)):
            saved = e["payload"]
            if all(saved[k] == v for k, v in key.items()):
                return saved
        return None

    async with repo.transaction():
        await repo.get_task(origin, owner_actor_id=owner)
        if saved := await replay():
            return saved
    choices = await available(service, owner, origin)
    option = next(
        (
            o
            for o in choices["options"]
            if o["review_digest"] == body.review_digest
            and o["candidate"]["reference"] == key["reference"]
        ),
        None,
    )
    if option is None:
        raise StaleStateVersion(
            "Review the acquired revision and current declaration first"
        )
    async with repo.transaction():
        task = await repo.get_task(origin, owner_actor_id=owner)
        await repo.get_project(
            str(task["project_id"]), owner_actor_id=owner, for_update=True
        )
        await repo.get_task(origin, owner_actor_id=owner, for_update=True)
        if saved := await replay():
            return saved
        original = task["original_input"]["input"]
        followup = {
            "kind": "handoff",
            "origin": origin,
            "previous_reference": choices["previous"]["reference"]
            if choices["previous"]
            else None,
        }
        made = await service.start(
            owner_actor_id=owner,
            project_id=str(task["project_id"]),
            question=original["text"],
            idempotency_key="adopt:" + body.review_digest,
            match_contract="knowbench/expert-match/v5",
            reference=body.reference,
            source_session=original.get("source_session"),
            followup=followup,
            checked_directory={"items": [option["candidate"]], "more": False},
        )
        receipt = {**key, "task_id": made["task_id"]}
        await repo.append_event(
            session_id=str(task["session_id"]),
            task_id=origin,
            kind="task",
            event_type=ADOPTED,
            payload=receipt,
        )
        return receipt
