"""Offline probe for the pinned Codex binary. Run only in a disposable container.

See docs/method-execution.md for the network-none/read-only invocation. Both
Responses and MCP are synthetic loopback servers. No model or data account.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

seen = []
events_seen = []
model_calls = 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(405)
        self.end_headers()

    def do_DELETE(self):
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.endswith("/responses"):
            global model_calls
            model_calls += 1
            response = {
                "id": "resp_probe",
                "object": "response",
                "status": "completed",
                "model": "test-model",
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
            item = {
                "id": "msg_probe",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "probe", "annotations": []}
                ],
            }
            if model_calls == 1:
                item = {
                    "type": "function_call",
                    "id": "fc_probe",
                    "call_id": "call_probe",
                    "name": "echo_scope",
                    "namespace": "mcp__sunmoon_workbench",
                    "arguments": "{}",
                    "status": "completed",
                }
            events = [
                {
                    "type": "response.created",
                    "response": {**response, "status": "in_progress"},
                },
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**item, "status": "in_progress", "content": []},
                },
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_probe",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "probe",
                },
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {
                    "type": "response.completed",
                    "response": {**response, "output": [item]},
                },
            ]
            if item["type"] == "function_call":
                events = [
                    events[0],
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": item,
                    },
                    events[-2],
                    events[-1],
                ]
            payload = "".join(
                "event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n"
                for e in events
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        scope = self.headers.get("X-Test-Scope")
        seen.append(
            {
                "method": msg.get("method"),
                "scope": scope,
                "headers": dict(self.headers),
                "params": msg.get("params")
                if msg.get("method") == "tools/call"
                else None,
            }
        )
        method = msg.get("method")
        mid = msg.get("id")
        if mid is None:
            self.send_response(202)
            self.end_headers()
            return
        if method == "initialize":
            result = {
                "protocolVersion": msg["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "isolated-probe", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo_scope",
                        "description": "Return the synthetic transport scope",
                        "inputSchema": {"type": "object", "properties": {}},
                        "annotations": {"readOnlyHint": True},
                    }
                ]
            }
        elif method == "tools/call":
            result = {
                "content": [{"type": "text", "text": scope or "missing"}],
                "isError": False,
            }
        else:
            result = {}
        data = json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def check_isolation():
    assert Path("/.dockerenv").exists(), "requires disposable Docker container"
    assert {p.name for p in Path("/sys/class/net").iterdir()} == {"lo"}, (
        "requires --network none"
    )
    assert not (Path.home() / ".codex").exists(), (
        "requires a fresh container home; never overwrite user config"
    )


async def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    folder = Path.home() / ".codex"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.toml").write_text(f"""model = "test-model"
model_provider = "probe"
[model_providers.probe]
name = "probe"
base_url = "http://127.0.0.1:{server.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
[mcp_servers.sunmoon_workbench]
url = "http://127.0.0.1:{server.server_port}/mcp"
startup_timeout_sec = 4
required = true
http_headers = {{ "X-Test-Scope" = "global" }}
""")
    proc = await asyncio.create_subprocess_exec(
        "/probe/codex",
        "app-server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/tmp",
    )
    pending = {}
    seq = 0
    finished = asyncio.Event()

    async def consume():
        while line := await proc.stdout.readline():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") in pending:
                pending[msg["id"]].set_result(msg)
            elif msg.get("method"):
                events_seen.append(msg)
            if msg.get("method") == "turn/completed":
                finished.set()

    async def err():
        while await proc.stderr.readline():
            pass

    out = asyncio.create_task(consume())
    errtask = asyncio.create_task(err())

    async def call(method, params):
        nonlocal seq
        seq += 1
        f = asyncio.get_running_loop().create_future()
        pending[seq] = f
        proc.stdin.write(
            (
                json.dumps({"id": seq, "method": method, "params": params}) + "\n"
            ).encode()
        )
        await proc.stdin.drain()
        result = await asyncio.wait_for(f, 15)
        pending.pop(seq, None)
        if "error" in result:
            raise RuntimeError(json.dumps(result))
        return result["result"]

    async def echo(tid):
        return await call(
            "mcpServer/tool/call",
            {
                "threadId": tid,
                "server": "sunmoon_workbench",
                "tool": "echo_scope",
                "arguments": {},
            },
        )

    try:
        await call(
            "initialize",
            {
                "clientInfo": {"name": "isolated-method-scope", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        proc.stdin.write(b'{"method":"initialized"}\n')
        await proc.stdin.drain()
        a = await call(
            "thread/start",
            {
                "cwd": "/tmp",
                "approvalPolicy": "never",
                "config": {
                    "mcp_servers.sunmoon_workbench.http_headers.X-Test-Scope": "attempt-a"
                },
            },
        )
        aid = a["thread"]["id"]
        assert (await echo(aid))["content"][0]["text"] == "attempt-a"
        b = await call(
            "thread/start",
            {
                "cwd": "/tmp",
                "approvalPolicy": "never",
                "config": {
                    "mcp_servers.sunmoon_workbench.http_headers.X-Test-Scope": "attempt-b"
                },
            },
        )
        bid = b["thread"]["id"]
        assert (await echo(bid))["content"][0]["text"] == "attempt-b"
        assert (await echo(aid))["content"][0]["text"] == "attempt-a"
        await call(
            "turn/start",
            {"threadId": aid, "input": [{"type": "text", "text": "synthetic probe"}]},
        )
        await asyncio.wait_for(finished.wait(), 15)
        await call(
            "thread/resume",
            {
                "threadId": aid,
                "excludeTurns": True,
                "config": {
                    "mcp_servers.sunmoon_workbench.http_headers.X-Test-Scope": "attempt-a2"
                },
            },
        )
        assert (await echo(aid))["content"][0]["text"] == "attempt-a", (
            "live resume unexpectedly reloaded MCP scope"
        )
        await call("thread/unsubscribe", {"threadId": aid})
        await call(
            "thread/resume",
            {
                "threadId": aid,
                "excludeTurns": True,
                "config": {
                    "mcp_servers.sunmoon_workbench.http_headers.X-Test-Scope": "attempt-a3"
                },
            },
        )
        assert (await echo(aid))["content"][0]["text"] == "attempt-a3"
    finally:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), 5)
        out.cancel()
        errtask.cancel()
        server.shutdown()
    calls = [v for v in seen if v["method"] == "tools/call"]
    model = [
        v
        for v in calls
        if (v["params"].get("_meta") or {}).get("callId") == "call_probe"
    ]
    assert len(model) == 1
    meta = model[0]["params"]["_meta"]
    turn = meta["x-codex-turn-metadata"]
    started = [
        v["params"]
        for v in events_seen
        if v["method"] == "item/started"
        and v["params"].get("item", {}).get("type") == "mcpToolCall"
    ]
    assert len(started) == 1
    item = started[0]["item"]
    assert meta["threadId"] == turn["thread_id"] == started[0]["threadId"] == aid
    assert turn["turn_id"] == started[0]["turnId"]
    assert meta["callId"] == item["id"]
    assert (
        item["server"] == "sunmoon_workbench"
        and item["tool"] == model[0]["params"]["name"]
    )
    assert item["arguments"] == model[0]["params"]["arguments"]
    assert all(
        "x-codex-turn-metadata" not in v["params"]["_meta"]
        for v in calls
        if v not in model
    )
    print(
        json.dumps(
            {
                "codex_version": turn["codex_version"],
                "model_call_matches_observed_event": True,
                "direct_call_has_no_turn_identity": True,
                "thread_scopes_are_separate": True,
                "live_resume_keeps_old_scope": True,
                "unsubscribe_resume_loads_new_scope": True,
            }
        )
    )


if __name__ == "__main__":
    check_isolation()
    asyncio.run(main())
