"""A formal host read through the configured KnowBase data MCP and its owner consent."""

import hashlib
from uuid import UUID

from app.domain.workbench.capabilities import CapabilityError
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

    root = await root_call(repo, task)
    path = [
        {
            "call_id": str(root["id"]),
            "method_id": str(snapshot.method_id),
            "definition_digest": snapshot.definition_digest,
            "version": None,
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


class KnowbaseMethodReader:
    def __init__(self, client, issuer):
        self.client = client
        self.issuer = issuer
        self.url = client.data_mcp_url

    async def rpc(self, subject, method, params, binding=None):
        token = self.issuer.data_token(
            subject, sandbox="formal-host", tools=[TOOL], method_read=binding
        ).token
        reply = await self.client.data_mcp(
            token,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params,
            },
        )
        if "error" in reply or not isinstance(reply.get("result"), dict):
            raise CapabilityError("KnowBase refused the method read request")
        return reply["result"]

    async def discover(self, subject):
        result = await self.rpc(subject, "tools/list", {})
        tools = result.get("tools")
        if (
            not isinstance(tools, list)
            or len(tools) != 1
            or tools[0].get("name") != TOOL
        ):
            raise CapabilityError("KnowBase method read is unavailable")
        return tools

    async def call(self, subject, intent, binding):
        if (
            intent.get("execution_kind") != "formal_mcp"
            or intent["tool"]["name"] != TOOL
            or not binding
        ):
            raise CapabilityError(
                "KnowBase method reads require a verified host invocation"
            )
        result = await self.rpc(
            subject,
            "tools/call",
            {"name": TOOL, "arguments": intent["arguments"]},
            binding,
        )
        if not result.get("isError"):
            content = result.get("structuredContent")
            source = content.get("source") if isinstance(content, dict) else None
            if (
                not isinstance(source, dict)
                or not isinstance(content.get("text"), str)
                or source.get("material_id") != binding["material_id"]
                or type(source.get("version_no")) is not int
                or source["version_no"] != binding["version_no"]
                or source.get("method_call_id") != binding["path"][-1]["call_id"]
            ):
                raise CapabilityError(
                    "KnowBase returned a different material or invocation"
                )
            if (
                source.get("sha256")
                != hashlib.sha256(content["text"].encode()).hexdigest()
            ):
                raise CapabilityError("KnowBase text differs from its content digest")
            try:
                UUID(source["grant_id"])
            except (KeyError, ValueError, TypeError, AttributeError) as exc:
                raise CapabilityError(
                    "KnowBase omitted the resource grant receipt"
                ) from exc
        return result


async def pending_read(svc, owner, call_id):
    from app.application.workbench.formal_host_calls import FormalHostCalls

    async with svc.repo.transaction():
        call = await svc.repo.capability_call(owner, str(call_id))
        if not call.get("task_id"):
            raise CapabilityError("This call has no formal Task")
        task = await svc.repo.get_task(
            str(call["task_id"]), owner_actor_id=owner, for_update=True
        )
        call = await svc.repo.capability_call(owner, str(call_id), lock=True)
        host = call["detail"].get("formal_host") or {}
        if (
            call["status"] != "awaiting_approval"
            or not host
            or not svc.knowbase_reader
            or task.get("cancel_requested_at")
            or str(task.get("active_interaction_id")) != str(call["interaction_id"])
            or str(task["id"]) != host.get("task_id")
        ):
            raise CapabilityError("This call has no pending KnowBase material read")
        context = await FormalHostCalls(svc).context(
            owner, host["task_id"], host["attempt_id"]
        )
        if (
            call["digest"] != context.identity
            or context.intent["formal_host"] != host
            or not context.knowbase_read
        ):
            raise CapabilityError("The requested KnowBase method invocation changed")
        return {
            "subject_digest": call["digest"],
            "parent_run_ref": "formal-task:" + host["task_id"],
            "read": context.knowbase_read,
        }
