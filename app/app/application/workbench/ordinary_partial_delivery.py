"""An owner's explicit receipt for verified branch artifacts of an interrupted join."""

from pydantic import Field

from app.application.workbench.formal_orchestration_execution import accepted_step
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import NotFound, StaleStateVersion
from app.domain.workbench.formal_execution import (
    host_snapshot,
    orchestration_plan,
    recursive_plan,
)
from app.domain.workbench.formal_method_execution_generated import fingerprint
from app.domain.workbench.formal_method_generated import Digest, Orchestration, Strict
from app.domain.workbench.ordinary_expert import ORDINARY_SOURCE
from app.domain.workbench.states import TaskState

RECEIVED = "task/partial_branch_received"


class BranchReceipt(Strict):
    scope_digest: Digest
    step: str = Field(min_length=1, max_length=64)


async def partial_delivery(repo, owner, task_id, command=None):
    async with repo.transaction():
        task = await repo.get_task(
            task_id, owner_actor_id=owner, for_update=command is not None
        )
        if task["pack_source"] != ORDINARY_SOURCE or task["state"] not in (
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.REJECTED,
        ):
            raise NotFound("No partial branch delivery")
        snapshot = host_snapshot(task)
        definition = snapshot.draft.definition
        graph = definition.procedure
        if (
            not isinstance(graph, Orchestration)
            or recursive_plan(snapshot)
            or not any(len(s.depends_on) >= 2 for s in graph.steps)
        ):
            if command:
                raise StaleStateVersion("No branches to receive")
            return {"report": None}
        attempts = await repo.list_attempts(task_id)
        by_slot = {}
        for attempt in attempts:
            by_slot.setdefault(attempt["step_id"], []).append(attempt)
        candidates, values, missing = {}, {}, []
        for index, step in enumerate(orchestration_plan(snapshot)):
            try:
                if not set(step.depends_on) <= candidates.keys():
                    raise CapabilityError("Missing input branch")
                matches = by_slot.get(step.id, [])
                output, pin = await accepted_step(
                    repo, task, snapshot, index, step, matches
                )
                expected = [a for k, a in candidates.items() if k in step.depends_on]
                if matches[0]["refs"].get("input_bindings") != expected:
                    raise CapabilityError("Changed branch inputs")
            except (ValueError, CapabilityError):
                missing.append(step.id)
                continue
            candidates[step.id] = {"step_id": step.id, **pin}
            values[step.id] = output
        promises = {}
        for rule in definition.how_to_validate:
            promises.setdefault(rule.promise_id, set()).update(rule.outputs)
        options = []
        for step, pin in candidates.items():
            exports = {
                name
                for name, ref in graph.exports.items()
                if ref.step == step and ref.output in values[step]
            }
            options.append(
                {
                    "artifact": pin,
                    "output_names": sorted(values[step]),
                    "applicable_promises": [
                        name
                        for name, required in promises.items()
                        if required and required <= exports
                    ],
                }
            )
        scope = fingerprint(
            [task_id, task["pack_digest"], task["state"], options, missing]
        )
        events = await repo.list_task_events(task_id, types=(RECEIVED,))
        receipts = {e["payload"]["step"]: e["payload"] for e in events}
        if command:
            if scope != command.scope_digest or command.step not in candidates:
                raise StaleStateVersion("Review the current partial delivery scope")
            if command.step not in receipts:
                option = next(
                    o for o in options if o["artifact"]["step_id"] == command.step
                )
                receipt = {
                    "scope_digest": scope,
                    "step": command.step,
                    **option,
                    "decided_by": owner,
                }
                await repo.append_event(
                    session_id=str(task["session_id"]),
                    kind="task",
                    task_id=task_id,
                    event_type=RECEIVED,
                    payload=receipt,
                )
                receipts[command.step] = receipt
        return {
            "report": {
                "scope_digest": scope,
                "options": options,
                "missing_steps": missing,
                "promises": definition.capability_declaration.promises,
                "received": [
                    dict(r, outputs=values[step])
                    for step, r in receipts.items()
                    if r["scope_digest"] == scope and step in values
                ],
            }
        }
