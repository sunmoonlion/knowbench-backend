"""Ordinary fixed MCP delegations share the host driver and its per-action approvals."""

from app.application.workbench.ledger import Ledger
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import IdempotencyConflict
from app.domain.workbench.formal_execution import same_implementation
from app.domain.workbench.formal_method_admission_generated import FormalMethodDraft
from app.domain.workbench.formal_method_generated import MethodRef
from app.domain.workbench.models import HandoverRequest
from app.domain.workbench.ordinary_expert import CONFIRMED, ConsumerMethod, ConsumerPlan


def execution_sources(graph, preview, subject, assets, methods):
    """Resolve the consumer's licensed installation, never the publisher's credentials."""
    if not graph or preview["issues"]:
        return {}, "preview_incomplete"
    if preview["bindings"]["materials"]:
        return {}, "material_authority_required"
    if any(
        len(n.path) != 1 or n.definition.procedure.implementation.kind != "mcp"
        for n in graph.leaf_nodes
    ):
        return {}, "driver_unavailable"
    by_asset = {a["id"]: a for a in assets}
    sources = {}
    for leaf, step in zip(graph.leaf_nodes, preview["steps"], strict=True):
        quote = step["quote"]
        impl = leaf.definition.procedure.implementation
        options = methods._sources(
            by_asset[quote["asset_id"]], subject, now=methods.now()
        )
        matches = [
            s
            for s in options
            if s.license_basis != "pending" and same_implementation(impl, s)
        ]
        if len(matches) != 1:
            return {}, "source_unlicensed"
        sources[leaf.path] = matches[0]
    return sources, None


async def confirm_execution(
    repo, owner, task, selected, definition, graph, preview, sources
):
    methods = {}
    policies = {}
    for node, step in zip(graph.leaf_nodes, preview["steps"], strict=True):
        method = ConsumerMethod(
            reference=node.reference,
            draft=FormalMethodDraft(
                definition=node.definition,
                source=sources[node.path],
                allowed_callers=("host",),
            ),
        )
        if node.reference in methods and methods[node.reference] != method:
            raise CapabilityError(
                "A repeated method requires conflicting account bindings"
            )
        methods[node.reference] = method
        policies[node.step.id] = step["quote"]["basis"]
    plan = ConsumerPlan(
        match_task_id=task["id"],
        preparation_revision=preview["revision"],
        preview_digest=preview["preview_digest"],
        reference=MethodRef.model_validate(selected),
        draft=FormalMethodDraft(
            definition=definition, source=None, allowed_callers=("host",)
        ),
        arguments=preview["bindings"]["values"],
        dependencies=tuple(methods.values()),
        policies=policies,
    )
    session_id = str(task["session_id"])
    result = await Ledger(repo).handover(
        HandoverRequest(
            idempotency_key="ordinary-expert:" + str(task["id"]),
            session_id=session_id,
            profile_id="formal-expert",
            original_input={
                "text": task["original_input"]["input"]["text"],
                **plan.identity(),
            },
            budget_limit=preview["bindings"]["budget"]["limit"],
        ),
        owner_actor_id=owner,
        snapshot=plan,
    )
    receipt = {
        "task_id": str(result["task_id"]),
        "revision": preview["revision"],
        "preview_digest": plan.preview_digest,
        "snapshot_digest": plan.task_columns()["pack_digest"],
    }
    await repo.append_event(
        session_id=session_id,
        task_id=str(task["id"]),
        kind="task",
        event_type=CONFIRMED,
        payload=receipt,
    )
    if result["created"] and not result.get("queued_behind"):
        session = await repo.get_session(session_id, owner_actor_id=owner)
        await repo.enqueue_command(
            session_id=session_id,
            sandbox_id=str(session["sandbox_id"]),
            kind="task.drive",
            payload={"task_id": receipt["task_id"]},
        )
    return receipt


def replay(receipt, submission):
    if (
        receipt["revision"] != submission.revision
        or receipt["preview_digest"] != submission.preview_digest
    ):
        raise IdempotencyConflict(
            "This match already has a different confirmed execution"
        )
    return receipt


async def check_execution(svc, owner, task, plan, grants):
    repo = svc.repo
    match = await repo.get_task(str(plan.match_task_id), owner_actor_id=owner)
    receipt = await repo.latest_task_payload(str(plan.match_task_id), CONFIRMED)
    if (
        not receipt
        or str(match["session_id"]) != str(task["session_id"])
        or receipt
        != {
            "task_id": str(task["id"]),
            "revision": plan.preparation_revision,
            "preview_digest": plan.preview_digest,
            "snapshot_digest": task["pack_digest"],
        }
    ):
        raise CapabilityError("Missing matching owner confirmation for this execution")
    expected = {d.reference: d.draft.definition for d in plan.dependencies}
    expected[plan.reference] = plan.draft.definition
    allowed = (grants or {}).get("ordinary_refs", set())
    if set(expected) != allowed:
        raise CapabilityError("Ordinary method use qualification changed")
    live = await svc.formal_sources.registry.local_definitions(owner, allowed)
    if live != expected:
        raise CapabilityError("A confirmed method registration changed")
