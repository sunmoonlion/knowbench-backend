"""Child execution evidence and the parent's disposition on the existing task ledger."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.domain.workbench.capabilities import digest
from app.domain.workbench.errors import IdempotencyConflict, NotFound, StaleStateVersion
from app.domain.workbench.formal_execution import expanded_plan, host_snapshot
from app.domain.workbench.formal_method_generated import ReviewPhaseStep, Strict
from app.domain.workbench.states import TASK_TERMINAL

ASSIGNED = "method/delegationAssigned"
RETURNED = "method/delegationReceipt"
DECIDED = "method/delegationDisposition"
LATE = "method/delegationLateReceipt"


class HandoffDecision(Strict):
    child_call_id: UUID
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["accept", "decline", "defer"]
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    next_owner: str | None = Field(default=None, min_length=1, max_length=200)


class HandoffClosure(Strict):
    parent_call_id: UUID
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    residual: str | None = Field(default=None, min_length=1, max_length=2000)
    decisions: tuple[HandoffDecision, ...] = Field(min_length=1, max_length=100)


async def assign(
    repo,
    task,
    child,
    attempt_id,
    step,
    reference,
    decl,
    arguments,
    bindings,
    *,
    role_call=None,
):
    refs = role_call or {}
    payload = {
        "task_id": str(task["id"]),
        "parent_call_id": child["parent_call_id"],
        "child_call_id": refs.get("logical_child_call_id", child["id"]),
        "execution_call_id": child["id"],
        "role": refs.get("logical_role") or child["detail"].get("path") or [step.id],
        "attempt_id": attempt_id,
        "reference": reference.model_dump(mode="json"),
        "plan_digest": task["pack_digest"],
        "input_digest": digest(arguments),
        "inputs": bindings,
        "goal": decl.capability_declaration.purpose,
        "exclusions": decl.capability_declaration.excludes,
        "permissions": [r.requirement_id for r in decl.access_requirements],
        "acceptance": [r.model_dump(mode="json") for r in decl.how_to_validate],
    }
    await repo.append_event_once(
        session_id=str(task["session_id"]),
        task_id=str(task["id"]),
        attempt_id=attempt_id,
        kind="task",
        event_type=ASSIGNED,
        payload=payload,
        same={"attempt_id": attempt_id, "child_call_id": payload["child_call_id"]},
    )


async def return_subtree(repo, task, call, state, artifacts=(), failure=None):
    key = {"child_call_id": call["id"], "attempt_id": None, "state": state}
    await repo.append_event_once(
        session_id=str(task["session_id"]),
        task_id=str(task["id"]),
        kind="task",
        event_type=RETURNED,
        same=key,
        payload={
            **key,
            "role": call["detail"]["path"],
            "plan_digest": task["pack_digest"],
            "input_digest": call["detail"]["arguments_digest"],
            "reference": call["detail"]["reference"],
            "artifacts": list(artifacts),
            "failure": failure,
            "executor": {},
            "cost": None,
            "cost_pending": False,
            "operation_call_id": None,
        },
    )


async def record_state(
    repo, task, attempt, target, artifacts, failure_code, consumed, pending
):
    refs = attempt["refs"]
    if (
        not refs.get("method_call_id")
        or refs.get("kind") != "formal_orchestration_step"
    ):
        return
    # State events carry the exact attempt and charged account. No request body or
    # private report is copied to the parent's handoff view.
    quote = refs.get("instruction_quote") or {}
    payload = {
        "child_call_id": refs.get("logical_child_call_id", refs["method_call_id"]),
        "execution_call_id": refs["method_call_id"],
        "attempt_id": str(attempt["id"]),
        "role": refs.get("logical_role") or refs.get("path") or [refs["slot"]],
        "state": str(target),
        "plan_digest": refs["snapshot_digest"],
        "input_digest": refs.get("arguments_digest"),
        "reference": refs.get("reference"),
        "artifacts": artifacts or [],
        "operation_call_id": refs.get("capability_call_id"),
        "failure": failure_code,
        "cost": None if pending else str(consumed),
        "cost_pending": pending,
        "executor": {
            k: v
            for k, v in {
                "provider": quote.get("provider"),
                "model": quote.get("model"),
                "reported_model": (refs.get("instruction_usage") or {}).get("model"),
                "gateway": (refs.get("instruction_gateway") or {}).get("uid"),
                **(refs.get("instruction_thread") or {}),
            }.items()
            if v is not None
        },
    }
    await repo.append_event(
        session_id=str(task["session_id"]),
        task_id=str(task["id"]),
        attempt_id=str(attempt["id"]),
        kind="task",
        event_type=RETURNED,
        payload=payload,
    )


async def record_late(repo, task, attempt, operation_id, status, result):
    refs = attempt.get("refs") or {}
    if (
        refs.get("kind") != "formal_orchestration_step"
        or refs.get("capability_call_id") != operation_id
    ):
        return
    identity = {
        "child_call_id": refs.get("logical_child_call_id", refs["method_call_id"]),
        "execution_call_id": refs["method_call_id"],
        "attempt_id": str(attempt["id"]),
        "operation_call_id": operation_id,
        "result_digest": digest(result),
        "status": status,
    }
    await repo.append_event_once(
        session_id=str(task["session_id"]),
        task_id=str(task["id"]),
        attempt_id=str(attempt["id"]),
        kind="task",
        event_type=LATE,
        same=identity,
        payload=identity,
    )


async def handoffs(repo, owner, task_id, command=None):
    async with repo.transaction():
        task = await repo.get_task(
            task_id, owner_actor_id=owner, for_update=command is not None
        )
        events = await repo.list_task_events(
            task_id, types=(ASSIGNED, RETURNED, DECIDED, LATE)
        )
        parents, receipts, closures, late = {}, {}, {}, {}
        for event in events:
            row = event["payload"]
            kind = event["type"]
            if kind == ASSIGNED:
                children = parents.setdefault(row["parent_call_id"], {})
                children.setdefault(row["child_call_id"], []).append(row)
            elif kind == DECIDED:
                closures[row["parent_call_id"]] = row
            elif kind == RETURNED:
                receipts.setdefault(
                    (row["child_call_id"], row["attempt_id"]), []
                ).append(row)
            else:
                late.setdefault(row["attempt_id"], []).append(row)
        result = []
        plan = expanded_plan(host_snapshot(task)) if parents else None
        by_id = (
            {c["id"]: c for c in await repo.list_task_calls(owner, task_id)}
            if parents
            else {}
        )
        for parent, children in parents.items():
            items = []
            for child, rounds in children.items():
                history = [
                    {
                        "request": r,
                        "receipts": receipts.get((child, r["attempt_id"]), []),
                        "late": late.get(r["attempt_id"], []),
                    }
                    for r in rounds
                ]
                final = history[-1]["receipts"]
                state = final[-1]["state"] if final else "CREATED"
                evidence = digest([[h["request"], h["receipts"]] for h in history])
                items.append(
                    {
                        "child_call_id": child,
                        "rounds": history,
                        "state": state,
                        "evidence_digest": evidence,
                    }
                )
            parent_path = tuple(by_id[parent]["detail"].get("path", []))
            expected = (
                [
                    [n.step.role]
                    if isinstance(n.step, ReviewPhaseStep)
                    else list(n.path)
                    for n in plan.nodes
                    if n.parent_path == parent_path
                ]
                if plan
                else []
            )
            actual = [rounds[0]["role"] for rounds in children.values()]
            missing = [
                list(role)
                for role in dict.fromkeys(map(tuple, expected))
                if list(role) not in actual
            ]
            parent_state = (
                by_id[parent]["status"].upper() if parent_path else str(task["state"])
            )
            terminal = parent_state in TASK_TERMINAL or task["state"] in TASK_TERMINAL
            scope = digest(
                [
                    task_id,
                    task["pack_digest"],
                    parent,
                    parent_state,
                    missing,
                    [[i["child_call_id"], i["evidence_digest"]] for i in items],
                ]
            )
            closed = closures.get(parent)
            if command and parent == str(command.parent_call_id):
                canonical = command.model_dump(mode="json")
                canonical["decisions"].sort(key=lambda d: d["child_call_id"])
                if closed and closed["command"] != canonical:
                    raise IdempotencyConflict("The parent already closed this handoff")
                if not closed:
                    choices = {str(d.child_call_id): d for d in command.decisions}
                    if (
                        command.scope_digest != scope
                        or not terminal
                        or missing
                        and not (command.residual or "").strip()
                        or any(c in parents and c not in closures for c in children)
                        or set(choices) != children.keys()
                        or len(choices) != len(command.decisions)
                    ):
                        raise StaleStateVersion(
                            "Review the terminal task and every assigned child"
                        )
                    for item in items:
                        choice = choices[item["child_call_id"]]
                        if (
                            choice.evidence_digest != item["evidence_digest"]
                            or choice.decision == "accept"
                            and item["state"] != "COMPLETED"
                            or choice.decision != "accept"
                            and not (choice.next_owner or "").strip()
                        ):
                            raise StaleStateVersion(
                                "The disposition must account for missing work and residual ownership"
                            )
                    closed = {
                        "parent_call_id": parent,
                        "writer": owner,
                        "command": canonical,
                        "state": task["state"],
                        "closed_at": datetime.now(UTC).isoformat(),
                        "missing": missing,
                    }
                    await repo.append_event(
                        session_id=str(task["session_id"]),
                        task_id=task_id,
                        kind="task",
                        event_type=DECIDED,
                        payload=closed,
                    )
            result.append(
                {
                    "parent_call_id": parent,
                    "scope_digest": scope,
                    "children": items,
                    "can_close": not closed
                    and terminal
                    and all(c not in parents or c in closures for c in children),
                    "missing": missing,
                    "closure": closed,
                }
            )
        if command and str(command.parent_call_id) not in parents:
            raise NotFound("No assigned child for this parent call")
        return {"parents": result}


async def handoff_evidence(repo, owner, task_id, child_id, artifact_id):
    async with repo.transaction():
        await repo.get_task(task_id, owner_actor_id=owner)
        events = await repo.list_task_events(task_id, types=(RETURNED,))
        for event in events:
            receipt = event["payload"]
            if receipt["child_call_id"] != child_id or receipt["state"] != "COMPLETED":
                continue
            for ref in receipt["artifacts"]:
                if str(ref["id"]) != artifact_id:
                    continue
                stored = await repo.get_artifact(
                    task_id=task_id, name=ref["name"], version=ref["version"]
                )
                if (
                    stored
                    and str(stored["id"]) == artifact_id
                    and stored["digest"] == ref["digest"]
                    and str(stored["attempt_id"] or "")
                    == str(receipt["attempt_id"] or "")
                ):
                    return {"artifact": ref, "outputs": stored["content"]}
        raise NotFound("No such exported artifact on this child receipt")
