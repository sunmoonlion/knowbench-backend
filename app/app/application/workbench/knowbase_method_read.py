"""A formal method read through the registered KnowBase function and its owner consent."""

import hashlib
from uuid import UUID

from app.domain.workbench.capabilities import CapabilityError, CapabilityOutcomeUnknown
from app.domain.workbench.formal_execution import expanded_plan

TOOL = "read_method_input"


async def read_binding(repo, task, snapshot, arguments, purpose, child, scopes):
    """Use the invocation already verified by FormalHostCalls.context, never caller IDs."""
    try:
        material = str(UUID(arguments["material_id"]))
        version = arguments["version_no"]
        if (
            set(arguments) != {"material_id", "version_no"}
            or type(version) is not int
            or version < 1
            or material != arguments["material_id"]
        ):
            raise ValueError
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        raise CapabilityError("Select an exact KnowBase material and version") from exc
    from app.application.workbench.method_calls import root_call
    from app.domain.workbench.ordinary_expert import ConsumerPlan

    root = await root_call(repo, task)
    path = [
        {
            "call_id": str(root["id"]),
            "method_id": str(snapshot.method_id),
            "definition_digest": snapshot.definition_digest,
            "version": snapshot.reference.version
            if isinstance(snapshot, ConsumerPlan)
            else None,
        }
    ]
    if child is not None:
        plan = expanded_plan(snapshot)
        leaf = plan.leaf_nodes[int(task["current_step"])]
        ancestors = {n.path: n for n in plan.nodes}
        ids = [str(s.call_id) for s in (scopes or ())[1:]] + [str(child["id"])]
        if len(ids) != len(leaf.path):
            raise CapabilityError("Formal method read has an incomplete parent chain")
        for depth, call_id in enumerate(ids, 1):
            ref = ancestors[leaf.path[:depth]].reference
            path.append(
                {
                    "call_id": call_id,
                    "method_id": str(ref.method_id),
                    "version": ref.version,
                    "definition_digest": ref.digest,
                }
            )
    return {
        "task_id": str(task["id"]),
        "path": path,
        "purpose": purpose,
        "material_id": material,
        "version_no": version,
    }


def verify_material(content, binding):
    source = content.get("source")
    if (
        not isinstance(source, dict)
        or not isinstance(content.get("text"), str)
        or source.get("material_id") != binding["material_id"]
        or type(source.get("version_no")) is not int
        or source["version_no"] != binding["version_no"]
        or source.get("method_call_id") != binding["path"][-1]["call_id"]
    ):
        raise CapabilityOutcomeUnknown(
            "KnowBase returned a different material or invocation"
        )
    if source.get("sha256") != hashlib.sha256(content["text"].encode()).hexdigest():
        raise CapabilityOutcomeUnknown("KnowBase text differs from its content digest")
    try:
        UUID(source["grant_id"])
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise CapabilityOutcomeUnknown(
            "KnowBase omitted the resource grant receipt"
        ) from exc


async def pending_read(svc, owner, call_id):
    from app.application.workbench.formal_host_calls import FormalHostCalls

    async with svc.repo.transaction(independent=True):
        call = await svc.repo.capability_call(owner, str(call_id))
    grants = (
        await svc.formal_sources.registry.task_grants(str(call["task_id"]), owner=owner)
        if call.get("task_id")
        else {}
    )
    async with svc.repo.transaction():
        if not call.get("task_id"):
            raise CapabilityError("This call has no formal Task")
        task = await svc.repo.get_task(
            str(call["task_id"]), owner_actor_id=owner, for_update=True
        )
        call = await svc.repo.capability_call(owner, str(call_id), lock=True)
        if call["status"] != "awaiting_approval" or not svc.data_function:
            raise CapabilityError("This call has no pending KnowBase material read")
        formal = FormalHostCalls(svc)
        host = formal.pending_invocation(task, call)
        context = await formal.approval_context(owner, call, host, grants=grants)
        if not context.knowbase_read:
            raise CapabilityError("This action does not read a KnowBase material")
        interaction = await svc.repo.get_interaction(str(call["interaction_id"]))
        expiry = min(
            [interaction["expires_at"], *(s.expires_at for s in context.scopes or ())]
        )
        return {
            "expires_at": expiry,
            "caller": "model"
            if call["detail"]["execution_kind"] == "formal_probe"
            else "host",
            "subject_digest": call["digest"],
            "parent_run_ref": "formal-task:" + host["task_id"],
            **(
                {"material_sha256": context.intent["material_sha256"]}
                if "material_sha256" in context.intent
                else {}
            ),
            "read": context.knowbase_read,
        }
