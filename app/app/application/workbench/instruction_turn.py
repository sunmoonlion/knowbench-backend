"""One isolated Codex turn. Only the dedicated worker connection carries model input."""

import asyncio
import json

from app.application.ports.workbench import AppServerConnection
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.formal_model import PROBE_PASSIVE_ITEMS
from app.domain.workbench.instruction_requests import INSTRUCTION_GUIDANCE


class InstructionOutputError(CapabilityError):
    """A stable error code suitable for receipts that must omit private model text."""


def instruction_output(parts):
    if not parts:
        raise InstructionOutputError("instruction_final_missing")
    if any(not isinstance(part, str) for part in parts):
        raise InstructionOutputError("instruction_final_invalid")
    try:
        result = json.loads("".join(parts))
    except (ValueError, RecursionError):
        raise InstructionOutputError("instruction_final_invalid_json") from None
    if not isinstance(result, dict):
        raise InstructionOutputError("instruction_output_not_object")
    return result


async def run_instruction_turn(
    connector, worker, model, job, *, take_thread, bind_turn, reject
):
    completed = asyncio.Event()
    thread_id = turn_id = None
    parts = []
    failed = False

    def observe_turn(reply):
        nonlocal turn_id
        turn_id = reply["turn"]["id"]
        if not isinstance(turn_id, str) or not 0 < len(turn_id) <= 128:
            raise CapabilityError("Instruction turn identity is missing")

    async def notice(method, params):
        nonlocal failed
        if not thread_id or params.get("threadId") != thread_id or not turn_id:
            return
        if method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
            failed |= params["turn"].get("status") != "completed"
            completed.set()
        if (
            method in ("item/started", "item/completed")
            and params.get("turnId") == turn_id
        ):
            item = params.get("item") or {}
            if item.get("type") not in PROBE_PASSIVE_ITEMS and not failed:
                failed = True
                await reject(thread_id)
            if (
                method == "item/completed"
                and item.get("type") == "agentMessage"
                and item.get("phase") != "commentary"
            ):
                parts.append(item.get("text"))

    async def deny(rid, method, params) -> None:
        nonlocal failed
        failed = True
        try:
            await reject(thread_id)
        finally:
            await client.respond_error(rid, -32601, "Instruction tools are disabled")

    client: AppServerConnection = connector(
        worker["ip"],
        worker["app_server_token"],
        on_notification=notice,
        on_server_request=deny,
    )
    try:
        await client.connect()
        opened = await client.request(
            "thread/start",
            {
                "cwd": "/tmp",
                "ephemeral": False,  # Keep history only in the dedicated Pod's temporary volume.
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "dynamicTools": [],
                "environments": [],
                "selectedCapabilityRoots": [],
                "model": model,
                "modelProvider": "instruction",
                "allowProviderModelFallback": False,
                "baseInstructions": INSTRUCTION_GUIDANCE,
            },
        )
        thread_id = opened["thread"]["id"]
        if (
            not isinstance(thread_id, str)
            or not 0 < len(thread_id) <= 128
            or opened.get("model") != model
            or opened.get("modelProvider") != "instruction"
            or opened.get("approvalPolicy") != "never"
            or opened.get("cwd") != "/tmp"
            or opened.get("sandbox", {}).get("type") != "readOnly"
        ):
            raise CapabilityError(
                "Worker did not open the authorized instruction profile"
            )
        if failed:
            raise CapabilityError("Unexpected instruction worker activity")
        if not await take_thread(thread_id):
            return None
        await client.request(
            "turn/start",
            {
                "threadId": thread_id,
                "environments": [],
                "input": [
                    {
                        "type": "text",
                        "text": json.dumps(job["input"], ensure_ascii=False),
                    }
                ],
                "outputSchema": job["output_schema"],
            },
            on_result=observe_turn,
        )
        if failed:
            raise CapabilityError("Unexpected instruction worker activity")
        await bind_turn(turn_id)
        await asyncio.wait_for(completed.wait(), timeout=180)
        if failed:
            raise InstructionOutputError("instruction_turn_rejected")
        return instruction_output(parts)
    finally:
        await client.close()


async def read_instruction_turn(connector, worker, thread_id, turn_id):
    """Recover the single recorded turn from this worker without resuming a thread."""

    async def ignore(method, params) -> None:
        pass

    async def deny(rid, method, params) -> None:
        await client.respond_error(rid, -32601, "Read-only instruction recovery")

    client: AppServerConnection = connector(
        worker["ip"],
        worker["app_server_token"],
        on_notification=ignore,
        on_server_request=deny,
    )
    try:
        await client.connect()
        page = await client.request(
            "thread/turns/list",
            {"threadId": thread_id, "limit": 2, "itemsView": "full"},
            timeout=10,
        )
        turns = page.get("data", [])
        if not turns:
            return None
        if len(turns) != 1 or page.get("nextCursor") or turns[0].get("id") != turn_id:
            raise CapabilityError("Instruction history has another turn")
        turn = turns[0]
        if turn.get("status") == "inProgress":
            return None
        items = turn.get("items") or []
        if (
            turn.get("status") != "completed"
            or turn.get("itemsView") != "full"
            or any(item.get("type") not in PROBE_PASSIVE_ITEMS for item in items)
        ):
            raise CapabilityError(
                "Instruction history has no admissible completed result"
            )
        finals = [
            item.get("text")
            for item in items
            if item.get("type") == "agentMessage" and item.get("phase") != "commentary"
        ]
        return instruction_output(finals)
    finally:
        await client.close()
