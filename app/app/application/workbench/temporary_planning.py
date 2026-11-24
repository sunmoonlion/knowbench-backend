"""Bounded planning in the owner's project. Accepting a sketch never dispatches its methods."""

import json
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.application.workbench.formal_expert_intake import (
    ENDED,
    REVISION,
    read_verified,
)
from app.application.workbench.ledger import Ledger
from app.domain.workbench import hierarchical_planning as htn
from app.domain.workbench.errors import NotFound, StaleStateVersion
from app.domain.workbench.expert_match import digest
from app.domain.workbench.formal_expert_match import frozen_scope
from app.domain.workbench.formal_method_compiler_generated import FrozenMethod
from app.domain.workbench.formal_method_generated import Digest, MethodRef, Strict
from app.domain.workbench.incubation_selection import (
    graph_patterns,
    routing_alternatives,
    verify_selection_graph,
)
from app.domain.workbench.method_search import PlanningQuery
from app.domain.workbench.models import HandoverRequest
from app.domain.workbench.planning_capabilities import (
    CATALOG_ENTRIES,
    CATALOG_SNAPSHOT_BYTES,
    capabilities,
    check_structure,
    requirements,
    same_runtime,
)
from app.domain.workbench.temporary_plan import (
    Choice,
    GraphChoice,
    build_selection,
    expected_routes,
    read_choice,
    root_for,
)

ROUND = "task/temporary_planning_round"
PLAN = "task/temporary_plan_adopted"
SAVED = "task/temporary_plan_saved"


class PlanningRequest(Strict):
    catalog_mode: Literal["auto", "directory", "goal"] = "auto"
    review_digest: Digest
    previous: UUID | None = None
    probe_digest: Digest | None = None
    required_capabilities: tuple[str, ...] = Field(default=(), max_length=8)
    clarification: str = Field(default="", max_length=1500)


class GraphReview(Strict):
    confirmed: Literal[True]
    routing_required: bool = Field(strict=True)
    cases: dict[str, list[str]] = Field(max_length=16)
    step_text: dict[str, str] = Field(max_length=16)


class Adoption(Strict):
    review: GraphReview | None = Field(default=None, exclude_if=lambda v: v is None)
    review_digest: Digest
    sketch_digest: Digest
    source_task: UUID | None = None


async def current(repo, owner, task_id, expected, *, lock=False):
    task = await repo.get_task(task_id, owner_actor_id=owner, for_update=lock)
    if task["profile_id"] != "FIND_EXPERT" or str(task["profile_version"]) != "5":
        raise NotFound("No such formal expert request")
    if (
        task["state"] != "SUCCEEDED"
        or await repo.latest_task_payload(task_id, ENDED)
        or await repo.latest_task_payload(task_id, REVISION)
    ):
        raise StaleStateVersion("This expert request is no longer current")
    artifact = await repo.get_artifact(task_id=task_id, name="find_expert")
    review = await read_verified(repo, task, artifact)
    if review["review_digest"] != expected or review["match"]["outcome"] != "no_match":
        raise StaleStateVersion("The current result has not established this gap")
    return task, review


async def record(repo, task, event, payload):
    await repo.append_event(
        session_id=str(task["session_id"]),
        task_id=str(task["id"]),
        kind="task",
        event_type=event,
        payload=payload,
    )


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False).encode())


def page_refs(registry, page):
    return [MethodRef.model_validate(registry.reference(row)) for row in page["items"]]


async def describe(registry, owner, pins):
    definitions = await registry.planning_definitions(owner, pins)
    entries = []
    for ref in pins:
        if ref not in definitions:
            continue
        definition = definitions[ref]
        declaration = definition.capability_declaration
        entry = {
            "reference": ref.model_dump(mode="json"),
            "title": declaration.title,
            "purpose": declaration.purpose,
        }
        entry.update(
            declaration=registry.public_declaration(ref, definition)["declaration"],
            interfaces=definition.interfaces(),
            kind=definition.procedure.kind,
            isolated_instruction=(
                definition.procedure.kind == "direct"
                and definition.procedure.implementation.kind
                in ("registered_instruction", "skill_instruction")
            ),
        )
        entries.append(entry)
    return entries


async def catalog(
    registry, owner, question, limit, *, previous=None, goal=None, mode="auto"
):
    trace = list(previous["trace"]) if previous else []
    if not previous:
        page = await registry.planning_versions(
            owner, limit, 0, query=PlanningQuery(stage="directory")
        )
        trace.append({"by": "host", "stage": "directory", "next": page["next"]})
        if page["next"] is None and mode != "goal":
            entries = await describe(registry, owner, page_refs(registry, page))
            if encoded_size(entries) <= CATALOG_SNAPSHOT_BYTES:
                return {
                    "mode": "directory",
                    "entries": entries,
                    "partial": len(entries) < len(page["items"]),
                    "queries": 1,
                    "trace": trace,
                }
        if mode == "directory":
            raise StaleStateVersion(
                "The authorized directory exceeds the complete summary limit"
            )
    if len(trace) >= 3:
        raise StaleStateVersion("Catalog query budget exhausted")
    query = goal or question
    page = await registry.planning_versions(
        owner,
        limit,
        0,
        query=PlanningQuery(stage="composition", question=query),
    )
    pins = page_refs(registry, page)
    if previous:
        for entry in previous["entries"]:
            ref = MethodRef.model_validate(entry["reference"])
            if ref not in pins:
                pins.append(ref)
    entries = await describe(registry, owner, pins[:limit])
    partial = (
        page["next"] is not None
        or len(entries) < len(pins)
        or bool(previous and previous["partial"])
    )
    while entries and encoded_size(entries) > CATALOG_SNAPSHOT_BYTES:
        entries.pop()
        partial = True
    trace.append(
        {
            "by": "model" if goal else "host",
            "stage": "composition",
            "query": query,
            "next": page["next"],
        }
    )
    return {
        "mode": "goal",
        "entries": entries,
        "partial": partial,
        "queries": len(trace),
        "trace": trace,
    }


async def start(service, owner, task_id, command, *, library=None, token=None):
    repo = service.repo
    subject = await repo.subject_of_actor(owner)
    if command.catalog_mode != "auto" and (
        subject not in service.planning_owners or command.previous
    ):
        raise NotFound(
            "Catalog comparisons are available only in the first round of an authorized trial"
        )
    if not service.planning_enabled(subject):
        raise NotFound("Temporary planning is not enabled for this user")
    async with repo.transaction():
        task, review = await current(
            repo, owner, task_id, command.review_digest, lock=True
        )
        events = await repo.list_task_events(task_id, types=(ROUND,))
        key = digest(command.model_dump(mode="json"))
        if events and events[-1]["payload"]["key"] == key:
            return events[-1]["payload"]
        if (
            events
            and not command.previous
            or (
                command.previous
                and (
                    not events
                    or events[-1]["payload"]["planning_task"] != str(command.previous)
                )
            )
        ):
            raise StaleStateVersion("Planning round changed")
        if await repo.latest_task_payload(task_id, PLAN):
            raise StaleStateVersion("This request already has an adopted plan")
        probe = None
        if command.probe_digest:
            from app.application.workbench.planning_probe import OBSERVED

            probe = await repo.latest_task_payload(task_id, OBSERVED)
            if (
                not command.previous
                or not probe
                or probe["consent"] != command.probe_digest
                or probe["planning_task"] != str(command.previous)
            ):
                raise StaleStateVersion(
                    "The approved probe is not for this planning round"
                )
        choice = None
        goal = None
        if events:
            prior = await repo.get_task(str(command.previous), owner_actor_id=owner)
            previous_result = await repo.get_artifact(
                task_id=str(command.previous), name="find_expert"
            )
            if prior["state"] != "SUCCEEDED" or not previous_result:
                raise StaleStateVersion("The previous planning round did not complete")
            choice = read_choice(prior, previous_result["content"])
            goal = choice.search_goal
            if (
                choice.outcome != "clarify"
                and not (
                    isinstance(choice, GraphChoice) and command.clarification.strip()
                )
            ) or not (command.clarification.strip() or probe or goal):
                raise StaleStateVersion(
                    "Complete the round and clarify before replanning"
                )
            if len(events) >= 2:
                raise StaleStateVersion("Planning budget exhausted")
            directory = prior["original_input"]["input"]["catalog"]
        question = review["question"]
        runtime = capabilities(service)
        needed = requirements(
            question + " " + command.clarification, command.required_capabilities
        )
        if events and prior:
            needed = sorted(
                set(needed)
                | set(prior["original_input"]["input"].get("required_capabilities", []))
            )
        missing = [k for k in needed if not runtime["patterns"].get(k, False)]
    previous_aliases = (
        {
            MethodRef.model_validate(e["reference"]): e["alias"]
            for e in directory["entries"]
            if "alias" in e
        }
        if events
        else {}
    )
    if probe:
        result = probe["result"]
        if not library or not token:
            raise StaleStateVersion("Cannot verify access to the probed material")
        _, versions = await library.preparation_material(
            await token(), result["material_id"], {result["version_no"]}
        )
        if (result["version_no"], result["sha256"]) not in versions:
            raise StaleStateVersion(
                "The approved material version is no longer available"
            )
    if missing:
        directory = {"entries": [], "queries": 0}
    elif not events:
        directory = await catalog(
            service.formal_registry,
            owner,
            question,
            CATALOG_ENTRIES,
            mode=command.catalog_mode,
        )
    elif goal:
        directory = await catalog(
            service.formal_registry,
            owner,
            question,
            CATALOG_ENTRIES,
            previous=directory,
            goal=goal,
        )
    else:
        refs = [
            MethodRef.model_validate(entry["reference"])
            for entry in directory["entries"]
        ]
        entries = await describe(service.formal_registry, owner, refs)
        while entries and encoded_size(entries) > CATALOG_SNAPSHOT_BYTES:
            entries.pop()
        directory = {
            **directory,
            "entries": entries,
            "partial": directory["partial"] or len(entries) < len(refs),
        }

    reserved = set(previous_aliases.values())
    next_id = 1
    for entry in directory["entries"]:
        pin = MethodRef.model_validate(entry["reference"])
        alias = previous_aliases.get(pin)
        if alias is None:
            while f"m{next_id}" in reserved:
                next_id += 1
            alias = f"m{next_id}"
            reserved.add(alias)
        entry["alias"] = alias
    # Enforce the same byte ceiling after aliases have been added.
    while encoded_size(directory["entries"]) > CATALOG_SNAPSHOT_BYTES:
        directory["entries"].pop()
        directory["partial"] = True
    async with repo.transaction():
        task, _ = await current(repo, owner, task_id, command.review_digest, lock=True)
        latest = await repo.latest_task_payload(task_id, ROUND)
        if latest and latest["key"] == key:
            return latest
        if latest != (
            events[-1]["payload"] if events else None
        ) or await repo.latest_task_payload(task_id, PLAN):
            raise StaleStateVersion("Planning round changed")
        if not directory["entries"]:
            receipt = {
                "key": key,
                "planning_task": None,
                "outcome": "gap",
                "reason": "unsupported_capability" if missing else "empty_catalog",
                "partial": directory.get("partial", False),
                "catalog_queries": directory["queries"],
                "missing": missing,
                "runtime": runtime,
            }
        else:
            req = HandoverRequest(
                idempotency_key="compose:" + task_id + ":" + str(len(events) + 1),
                session_id=str(task["session_id"]),
                profile_id="FIND_EXPERT",
                profile_version="7",
                original_input={
                    "planner": htn.PROTOCOL,
                    "text": question,
                    **(
                        {"previous_selection": choice.selection.model_dump(mode="json")}
                        if isinstance(choice, GraphChoice)
                        and (
                            (
                                choice.selection.tree
                                and htn.FOCUSED_AMENDMENT.match(command.clarification)
                            )
                            or set(choice.aliases)
                            <= {e["alias"] for e in directory["entries"]}
                        )
                        else {}
                    ),
                    "clarification": command.clarification,
                    "catalog": directory,
                    "runtime": runtime,
                    "required_capabilities": needed,
                    "origin_task": task_id,
                    **({"authorized_column_probe": probe["result"]} if probe else {}),
                },
                budget_limit=Decimal("2"),
            )
            made = await Ledger(repo).handover(req, owner_actor_id=owner)
            if made["created"] and not made.get("queued_behind"):
                session = await repo.get_session(req.session_id)
                await repo.enqueue_command(
                    session_id=req.session_id,
                    sandbox_id=str(session["sandbox_id"]),
                    kind="task.drive",
                    payload={"task_id": made["task_id"]},
                )
            receipt = {
                "key": key,
                "planning_task": str(made["task_id"]),
                "outcome": "planning",
            }
        await record(repo, task, ROUND, receipt)
        return receipt


async def compile_choice(
    service, owner, task_id, question, choice, runtime, required, review=None
):
    definitions = await service.formal_registry.planning_definitions(
        owner, choice.steps
    )
    if not isinstance(choice, GraphChoice):
        ref, root = root_for(task_id, question, choice, definitions)
        check_structure(root, definitions, runtime, required)
        return ref, root
    routes = expected_routes(choice.selection)
    if review is not None:
        routes = {k: set(v) for k, v in review.cases.items()}
    ref, root = build_selection(task_id, question, choice, definitions, runtime, routes)
    found = dict(definitions)
    found.pop(ref, None)
    frontier = [root]
    visited = set()
    for _ in range(16):
        needed = set()
        for definition in frontier:
            if definition.procedure.kind == "orchestration":
                for step in definition.procedure.steps:
                    needed.update(step.candidates)
        needed -= visited
        visited.update(needed)
        missing = list(needed - found.keys())
        if ref in needed or len(found) + len(missing) > 1000:
            raise ValueError("selection_graph")
        for offset in range(0, len(missing), 100):
            page = missing[offset : offset + 100]
            authorized = await service.formal_registry.planning_definitions(owner, page)
            if set(page) != authorized.keys():
                raise ValueError("selection_candidate")
            found.update(authorized)
        frontier = [
            found[r] for r in needed if found[r].procedure.kind == "orchestration"
        ]
        if not frontier:
            break
    else:
        raise ValueError("selection_graph")
    check_structure(root, found, runtime, required)
    frozen = {(r.method_id, r.version): FrozenMethod(r, d) for r, d in found.items()}
    verify_selection_graph(ref.method_id, root, frozen, runtime)
    return ref, root


def reviewed_choice(choice, review):
    if not isinstance(choice, GraphChoice):
        return choice
    if not review or set(review.step_text) != {s.id for s in choice.selection.steps}:
        raise StaleStateVersion("Review every step before adopting the plan")
    if review.routing_required != bool(expected_routes(choice.selection)):
        raise StaleStateVersion(
            "Review the required conditional routes before adopting the plan"
        )
    if any(not text.strip() or len(text) > 1500 for text in review.step_text.values()):
        raise StaleStateVersion("Invalid reviewed step description")
    steps = tuple(
        s.model_copy(update={"task": review.step_text[s.id]})
        for s in choice.selection.steps
    )
    return choice.model_copy(
        update={"selection": choice.selection.model_copy(update={"steps": steps})}
    )


def visible_graph(choice, entries):
    if not isinstance(choice, GraphChoice):
        return {}
    catalog = {
        e["alias"]: {
            "declaration": e["declaration"],
            "inputs": {k: i["slot"] for k, i in e["interfaces"]["inputs"].items()},
        }
        for e in entries
    }
    return {
        "method_titles": {
            k: m["declaration"]["title"]
            for k, m in catalog.items()
            if k in choice.aliases
        },
        "patterns": sorted(graph_patterns(choice.selection)),
        "routing_alternatives": routing_alternatives(choice.selection, catalog),
    }


async def sketch(service, owner, task_id):
    repo = service.repo
    async with repo.transaction():
        task = await repo.get_task(task_id, owner_actor_id=owner)
        saved = await repo.latest_task_payload(task_id, PLAN)
        if saved:
            return {
                **{
                    k: saved[k]
                    for k in (
                        "selection",
                        "aliases",
                        "method_titles",
                        "patterns",
                        "routing_alternatives",
                    )
                    if k in saved
                },
                "outcome": "adopted",
                "reference": saved["reference"],
                "scope": saved["scope"],
                "sketch_digest": saved["sketch_digest"],
                "runtime": saved["runtime"],
                "saved": bool(await repo.latest_task_payload(task_id, SAVED)),
            }
        run = await repo.latest_task_payload(task_id, ROUND)
        if not run or not run["planning_task"]:
            return run or {"outcome": "idle"}
        child = await repo.get_task(run["planning_task"], owner_actor_id=owner)
        if (
            str(task["session_id"]) != str(child["session_id"])
            or child["original_input"]["input"]["origin_task"] != task_id
        ):
            raise NotFound("No such planning round")
        from app.application.workbench.planning_probe import OBSERVED

        probe = await repo.latest_task_payload(task_id, OBSERVED)
        context = child["original_input"]["input"]
        if child["state"] != "SUCCEEDED":
            if child["state"] == "FAILED":
                event = await repo.latest_task_payload(
                    run["planning_task"], "task/state"
                )
                reason = ((event or {}).get("reason") or {}).get("code")
                if reason in {"selection_repair_failed", "selection_planning_timeout"}:
                    return {**run, "outcome": "gap", "explanation": reason}
            return {
                **run,
                "outcome": "gap"
                if child["state"] in {"FAILED", "CANCELLED", "REJECTED"}
                else "planning",
            }
        artifact = await repo.get_artifact(
            task_id=run["planning_task"], name="find_expert"
        )
        if not artifact:
            raise StaleStateVersion("Missing planner output")
        choice = read_choice(child, artifact["content"])
        directory = context["catalog"]
    allowed = [MethodRef.model_validate(e["reference"]) for e in directory["entries"]]
    if not set(choice.steps) <= set(allowed):
        raise StaleStateVersion("Planner selected outside the authorized directory")
    result = {
        **run,
        **choice.model_dump(mode="json"),
        **visible_graph(choice, directory["entries"]),
        "remaining_rounds": 2
        - len(await repo.list_task_events(task_id, types=(ROUND,))),
        "directory_mode": directory["mode"],
        "partial": directory["partial"],
        "catalog_queries": directory["queries"],
    }
    if probe and probe["planning_task"] == run["planning_task"]:
        result["probe"] = probe
    result["runtime"] = context["runtime"]
    result["required_capabilities"] = context["required_capabilities"]
    if choice.outcome != "plan":
        return result
    try:
        runtime = capabilities(service)
        if not same_runtime(context["runtime"], runtime):
            raise ValueError(
                "The runner capabilities changed; review the current deployment"
            )
        _, root = await compile_choice(
            service,
            owner,
            task_id,
            context["text"],
            choice,
            runtime,
            context["required_capabilities"],
        )
    except ValueError as exc:
        return {**result, "outcome": "gap", "explanation": str(exc)}
    return {
        **result,
        "scope": frozen_scope(root),
        "sketch_digest": digest(
            [task_id, run["key"], choice.model_dump(mode="json"), root.digest()]
        ),
    }


async def adopt(service, owner, task_id, command):
    repo = service.repo
    if not service.planning_enabled(await repo.subject_of_actor(owner)):
        raise NotFound("Temporary planning is not enabled for this user")
    source_id = str(command.source_task or task_id)
    async with repo.transaction():
        task, review = await current(repo, owner, task_id, command.review_digest)
        existing = await repo.latest_task_payload(task_id, PLAN)
        if existing:
            if existing["approval"] != command.model_dump(mode="json"):
                raise StaleStateVersion(
                    "This request already adopted a different sketch"
                )
            return existing["scope"]
        await repo.get_task(source_id, owner_actor_id=owner)
        saved = (
            await repo.latest_task_payload(source_id, PLAN)
            if command.source_task
            else None
        )
        if command.source_task and (
            not saved or not await repo.latest_task_payload(source_id, SAVED)
        ):
            raise StaleStateVersion("The source plan was not saved for reuse")
    picked = saved if saved else await sketch(service, owner, source_id)
    if (
        picked.get("sketch_digest") != command.sketch_digest
        or picked.get("outcome") != "plan"
    ):
        raise StaleStateVersion("The planning evidence changed")
    if picked.get("selection"):
        original = GraphChoice.model_validate(
            {
                k: picked[k]
                for k in ("outcome", "steps", "explanation", "aliases", "selection")
            }
        )
    else:
        original = Choice.model_validate(
            {k: picked[k] for k in ("outcome", "steps", "explanation")}
        )
    choice = reviewed_choice(original, command.review)
    try:
        runtime = capabilities(service)
        required = set(picked.get("required_capabilities", [])) | set(
            requirements(review["question"])
        )
        ref, root = await compile_choice(
            service,
            owner,
            task_id,
            review["question"],
            choice,
            runtime,
            required,
            command.review,
        )
        if not saved and not same_runtime(picked["runtime"], runtime):
            raise ValueError("Runner changed since this sketch was prepared")
    except ValueError as exc:
        raise StaleStateVersion(str(exc)) from exc
    data = {
        **choice.model_dump(mode="json"),
        "reference": ref.model_dump(mode="json"),
        "definition": root.document(),
        "scope": frozen_scope(root),
        **{
            k: picked[k]
            for k in ("method_titles", "patterns", "routing_alternatives")
            if k in picked
        },
        "sketch_digest": command.sketch_digest
        if original == choice
        else digest(
            [command.sketch_digest, choice.model_dump(mode="json"), root.digest()]
        ),
        "approval": command.model_dump(mode="json"),
        "required_capabilities": sorted(required),
        "runtime": runtime,
        "local_root": ref not in choice.steps,
    }
    async with repo.transaction():
        task, _ = await current(repo, owner, task_id, command.review_digest, lock=True)
        previous = await repo.latest_task_payload(task_id, PLAN)
        if previous and previous != data:
            raise StaleStateVersion("Another plan was adopted")
        if not previous:
            if (
                not saved
                and (await repo.latest_task_payload(task_id, ROUND))["key"]
                != picked["key"]
            ):
                raise StaleStateVersion("A newer round exists")
            await record(repo, task, PLAN, data)
    return data["scope"]


async def save(repo, owner, task_id, sketch_digest):
    async with repo.transaction():
        task = await repo.get_task(task_id, owner_actor_id=owner, for_update=True)
        plan = await repo.latest_task_payload(task_id, PLAN)
        if not plan or plan["sketch_digest"] != sketch_digest:
            raise StaleStateVersion("No such adopted plan")
        receipt = {"source_task": task_id, "sketch_digest": sketch_digest}
        if not await repo.latest_task_payload(task_id, SAVED):
            await record(repo, task, SAVED, receipt)
        return receipt


async def audit(service, owner, task_id):
    from app.application.workbench.formal_expert_intake import DIRECTORY
    from app.domain.workbench.ordinary_expert import CONFIRMED

    repo = service.repo
    async with repo.transaction():
        parent = await repo.get_task(task_id, owner_actor_id=owner)
        if (
            parent["profile_id"] != "FIND_EXPERT"
            or str(parent["profile_version"]) != "5"
            or await repo.subject_of_actor(owner) not in service.planning_owners
        ):
            raise NotFound("No such planning trial")

        async def evidence(task):
            attempts = await repo.list_attempts(str(task["id"]))
            return {
                "task_id": str(task["id"]),
                "state": task["state"],
                "budget": task["budget"],
                "attempts": [
                    {
                        k: a.get(k)
                        for k in (
                            "id",
                            "model",
                            "model_provider",
                            "codex_version",
                            "started_at",
                            "ended_at",
                            "budget_consumed",
                            "turn_ids",
                            "failure_code",
                        )
                    }
                    for a in attempts
                ],
            }

        runs = []
        for event in await repo.list_task_events(task_id, types=(ROUND,)):
            row = event["payload"]
            if not row["planning_task"]:
                runs.append(row)
                continue
            task = await repo.get_task(row["planning_task"], owner_actor_id=owner)
            data = task["original_input"]["input"]
            if (
                data["origin_task"] != task_id
                or task["session_id"] != parent["session_id"]
            ):
                raise StaleStateVersion("Planning task does not belong to this trial")
            artifact = await repo.get_artifact(
                task_id=str(task["id"]), name="find_expert"
            )
            runs.append(
                {
                    **await evidence(task),
                    "runtime": data["runtime"],
                    "catalog": data["catalog"],
                    "clarification": data["clarification"],
                    "observed_column": data.get("authorized_column_probe"),
                    "proposal": artifact["content"] if artifact else None,
                }
            )
        plan = await repo.latest_task_payload(task_id, PLAN)
        receipt = await repo.latest_task_payload(task_id, CONFIRMED)
        delivery = None
        if receipt:
            execution = await repo.get_task(receipt["task_id"], owner_actor_id=owner)
            output = await repo.get_artifact(
                task_id=receipt["task_id"], name="expert_delivery", version=1
            )
            delivery = {
                **await evidence(execution),
                "artifact_id": str(output["id"]) if output else None,
            }
        return {
            "match": await evidence(parent),
            "question": parent["original_input"]["input"]["text"],
            "expert_directory": await repo.latest_task_payload(task_id, DIRECTORY),
            "rounds": runs,
            "adopted": None
            if not plan
            else {"digest": plan["sketch_digest"], "methods": plan["steps"]},
            "execution": delivery,
        }
