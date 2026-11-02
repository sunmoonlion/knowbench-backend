"""Check the selected thread's real MCP inventory before spending a model turn.

The frozen step authorizes tools; prose claiming they exist is not runtime
availability. No reload, model call, grant change or business confirmation here.
"""

from __future__ import annotations

from typing import Any

from app.application.ports.workbench import AppServerConnection, WorkbenchStore
from app.application.workbench.catalog import SNAPSHOT_SOURCES
from app.application.workbench.execution_plan import execution_for_task
from app.application.workbench.method_tools import SERVER
from app.domain.workbench.knowbots import DATA_MCP_TOOLS
from app.domain.workbench.method_plan import PLAN_STEP
from app.domain.workbench.states import TaskState, Wheel


async def declared_data_tools(repo: WorkbenchStore, thread_id: str) -> frozenset[str]:
    """Read the active frozen step, never model-supplied names or prompt text."""
    session = await repo.get_session_by_thread(thread_id)
    if not session or session.get("wheel") != Wheel.advisor:
        return frozenset()
    task_id = session.get("active_task_id")
    if not task_id:
        return frozenset()
    task = await repo.get_task(str(task_id))
    if (
        task.get("pack_source") not in SNAPSHOT_SOURCES
        or task["state"] != TaskState.RUNNING
        or task.get("cancel_requested_at")
        or not task.get("active_attempt_id")
    ):
        return frozenset()
    attempt = await repo.get_attempt(str(task["active_attempt_id"]))
    if attempt["step_id"] == PLAN_STEP:
        return frozenset()  # Method planning consumes the frozen catalog, not data.
    if attempt["step_id"] == "data-plan":
        return frozenset({"describe_items"})
    view = await execution_for_task(repo, task)
    step = view.pack.step(int(task["current_step"])) if view else None
    if step is None or step.step_id != attempt["step_id"]:
        return frozenset()
    return DATA_MCP_TOOLS & frozenset(step.tools)


async def unavailable_data_tools(
    client: AppServerConnection, thread_id: str, required: frozenset[str]
) -> str | None:
    """Bounded, thread-scoped discovery. Never leak RPC bodies or credentials."""
    if not required:
        return None
    cursor = None
    seen: set[str] = set()
    try:
        for _ in range(4):
            params: dict[str, Any] = {
                "threadId": thread_id,
                "detail": "toolsAndAuthOnly",
                "limit": 100,
            }
            if cursor:
                params["cursor"] = cursor
            reply = await client.request("mcpServerStatus/list", params, timeout=10)
            if not isinstance(reply, dict) or not isinstance(reply.get("data"), list):
                break
            for server in reply["data"]:
                if not isinstance(server, dict) or server.get("name") != SERVER:
                    continue
                if server.get("toolsError") or server.get("runtimeStatus") not in (
                    None,
                    "connected",
                ):
                    return "数据读工具发现失败，已停止本步骤；请在服务修复并刷新线程工具后重新执行。"
                tools = server.get("tools")
                if not isinstance(tools, dict):
                    break
                names = {
                    tool["name"]
                    for tool in tools.values()
                    if isinstance(tool, dict) and isinstance(tool.get("name"), str)
                }
                missing = sorted(required - names)
                if not missing:
                    return None
                return "数据读工具未挂载，已停止本步骤：" + ", ".join(missing)
            cursor = reply.get("nextCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                break
            seen.add(cursor)
    except Exception:
        # An unavailable discovery service is a platform fault, not missing data.
        pass
    return "无法核实本线程的数据读工具，已停止本步骤；这不代表用户没有数据。"
