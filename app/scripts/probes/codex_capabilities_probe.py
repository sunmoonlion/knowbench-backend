"""Pinned Codex protocol probe. Disposable, network-none Docker container only."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

captures = []
mcp_events = []
results = []
outdir = Path("/tmp/evidence")
next_model_tool = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(405)
        self.end_headers()

    def do_DELETE(self):
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        msg = json.loads(raw)
        if self.path == "/v1/responses":
            global next_model_tool
            captures.append(msg)
            rid = "resp_" + str(len(captures))
            item = {
                "type": "message",
                "role": "assistant",
                "id": "msg_" + rid,
                "content": [{"type": "output_text", "text": "synthetic_ok"}],
            }
            if next_model_tool:
                item = {
                    "type": "function_call",
                    "id": "fc_" + rid,
                    "call_id": "call_" + rid,
                    "name": next_model_tool,
                    "namespace": "mcp__sunmoon_capabilities",
                    "arguments": json.dumps(
                        {
                            "skill_id": "11111111-1111-4111-8111-111111111111",
                            "path": "references/guide.md",
                        }
                    )
                    if next_model_tool == "skill_read"
                    else "{}",
                    "status": "completed",
                }
                next_model_tool = None
            events = [
                {"type": "response.created", "response": {"id": rid}},
                {"type": "response.output_item.done", "item": item},
                {
                    "type": "response.completed",
                    "response": {
                        "id": rid,
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "total_tokens": 2,
                        },
                    },
                },
            ]
            data = "".join(
                "event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n"
                for e in events
            ).encode()
            ctype = "text/event-stream"
        elif self.path == "/mcp":
            mcp_events.append(
                {
                    "method": msg.get("method"),
                    "params": msg.get("params"),
                    "scope": self.headers.get("Authorization"),
                }
            )
            if "id" not in msg:
                self.send_response(202)
                self.end_headers()
                return
            method = msg["method"]
            if method == "initialize":
                value = {
                    "protocolVersion": msg["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "scope-probe", "version": "1"},
                }
            elif method == "tools/list":
                value = {
                    "tools": [
                        {
                            **tool(n),
                            "annotations": {
                                "readOnlyHint": n == "skill_read",
                                "destructiveHint": False,
                                "openWorldHint": False,
                            },
                        }
                        for n in (
                            "c_11111111111141118111111111111111_27ce1d1bf427aecd",
                            "c_22222222222242228222222222222222_1b29de8941ee403a",
                            "skill_read",
                        )
                    ]
                }
            elif method == "tools/call":
                value = {
                    "content": [
                        {
                            "type": "text",
                            "text": "LAZY_REFERENCE_BODY_9182"
                            if msg["params"]["name"] == "skill_read"
                            else "synthetic_" + msg["params"]["name"],
                        }
                    ],
                    "isError": False,
                }
            else:
                value = {}
            data = json.dumps(
                {"jsonrpc": "2.0", "id": msg["id"], "result": value}
            ).encode()
            ctype = "application/json"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def tool(name):
    if name == "skill_read":
        return {
            "name": name,
            "description": "Read a selected Skill reference without execution",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["skill_id", "path"],
                "additionalProperties": False,
            },
        }
    return {
        "name": name,
        "description": "Synthetic no-side-effect probe " + name,
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }


def tool_names(body):
    def walk(items, prefix=""):
        names = []
        for item in items:
            if item.get("type") == "namespace":
                names += walk(item.get("tools", []), item["name"] + ".")
            else:
                names.append(prefix + item.get("name", item.get("type", "?")))
        return names

    return sorted(walk(body.get("tools", [])))


async def main():
    assert await asyncio.to_thread(Path("/.dockerenv").exists)
    assert await asyncio.to_thread(
        lambda: {p.name for p in Path("/sys/class/net").iterdir()}
    ) == {"lo"}
    folder = Path("/data/codex")
    await asyncio.to_thread(folder.mkdir, parents=True)
    cwd = Path("/tmp/step-probe")
    await asyncio.to_thread(cwd.mkdir)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    (folder / "config.toml").write_text(f"""model = "test-model"
model_provider = "probe"
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
[features]
shell_tool = false
multi_agent = false
apps = false
goals = false
[analytics]
enabled = false
[model_providers.probe]
name = "Offline probe"
base_url = "http://127.0.0.1:{server.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
""")
    version = await asyncio.create_subprocess_exec(
        "/probe/codex", "--version", stdout=asyncio.subprocess.PIPE
    )
    version_text = (await version.communicate())[0].decode().strip()
    assert version_text == "codex-cli 0.155.1", version_text
    proc = await asyncio.create_subprocess_exec(
        "/probe/codex",
        "app-server",
        "--listen",
        "stdio://",
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
    assert stdin is not None and stdout is not None and stderr is not None
    pending = {}
    notices = []
    seq = 0
    stderr_lines = []

    async def consume():
        while line := await stdout.readline():
            msg = json.loads(line)
            if "method" in msg:
                notices.append(msg)
            elif msg.get("id") in pending:
                f = pending[msg["id"]]
                if not f.done():
                    f.set_result(msg)

    async def err():
        while line := await stderr.readline():
            stderr_lines.append(line.decode(errors="replace"))

    readers = [asyncio.create_task(consume()), asyncio.create_task(err())]

    async def call(method, params):
        nonlocal seq
        seq += 1
        number = seq
        pending[number] = asyncio.get_running_loop().create_future()
        stdin.write(
            (
                json.dumps({"id": number, "method": method, "params": params}) + "\n"
            ).encode()
        )
        await stdin.drain()
        answer = await asyncio.wait_for(pending[number], 20)
        pending.pop(number, None)
        if "error" in answer:
            raise RuntimeError(json.dumps({"method": method, "error": answer["error"]}))
        return answer["result"]

    async def start(dynamic=None, enabled=None):
        params = {"cwd": str(cwd), "approvalPolicy": "never", "environments": []}
        if dynamic is not None:
            params["dynamicTools"] = [tool(n) for n in dynamic]
            params["config"] = {
                "mcp_servers.sunmoon_capabilities": {
                    "enabled": False,
                    "url": "http://127.0.0.1:9",
                    "enabled_tools": [],
                    "http_headers": {},
                    "required": False,
                }
            }
        else:
            params["config"] = {
                "mcp_servers.sunmoon_capabilities.enabled_tools": enabled
            }
        reply = await call("thread/start", params)
        return reply["thread"]["id"]

    async def turn(tid, label, extra=None, invoke=None):
        global next_model_tool
        next_model_tool = invoke
        before = len(captures)
        n_before = len(notices)
        params = {
            "threadId": tid,
            "environments": [],
            "input": [{"type": "text", "text": "MARKER_" + label}],
        }
        params.update(extra or {})
        reply = await call("turn/start", params)
        turnid = reply["turn"]["id"]
        for _ in range(400):
            completed = next(
                (
                    m
                    for m in notices[n_before:]
                    if m.get("method") == "turn/completed"
                    and m["params"]["turn"]["id"] == turnid
                ),
                None,
            )
            if completed:
                assert completed["params"]["turn"]["status"] == "completed", completed
                break
            await asyncio.sleep(0.05)
        else:
            raise TimeoutError(label + ": turn did not complete")
        assert len(captures) == before + (2 if invoke else 1), (
            label,
            len(captures) - before,
        )
        body = captures[-1]
        names = tool_names(body)
        row = {
            "case": label,
            "thread_id": tid,
            "tools": names,
            "history_has_first_dynamic_marker": "MARKER_D1_initial"
            in json.dumps(body.get("input")),
            "history_has_first_mcp_marker": "MARKER_M1_c_11111111111141118111111111111111_27ce1d1bf427aecd"
            in json.dumps(body.get("input")),
        }
        results.append(row)
        print(json.dumps(row), flush=True)
        return names

    async def resume(tid, **kwargs):
        reply = await call(
            "thread/resume", {"threadId": tid, "excludeTurns": True, **kwargs}
        )
        assert reply["thread"]["id"] == tid

    try:
        await call(
            "initialize",
            {
                "clientInfo": {"name": "step-scope-probe", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        stdin.write(b'{"method":"initialized"}\n')
        await stdin.drain()
        tid = await start(dynamic=[])
        assert not mcp_events, "Disabled initial server must not connect"
        state = await call("thread/read", {"threadId": tid, "includeTurns": False})
        assert state["thread"]["status"]["type"] == "idle"
        await call(
            "thread/inject_items",
            {
                "threadId": tid,
                "items": [
                    {
                        "type": "message",
                        "role": "developer",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Platform mode: chat, no local command execution.",
                            }
                        ],
                    }
                ],
            },
        )
        await call("thread/unsubscribe", {"threadId": tid})
        await resume(
            tid,
            config={
                "mcp_servers.sunmoon_capabilities": {
                    "enabled": True,
                    "url": f"http://127.0.0.1:{server.server_port}/mcp",
                    "http_headers": {"Authorization": "Bearer synthetic-scope-a"},
                    "enabled_tools": [
                        "c_11111111111141118111111111111111_27ce1d1bf427aecd"
                    ],
                    "required": True,
                }
            },
        )
        names = await turn(
            tid,
            "managed_stage_a",
            extra={
                "input": [
                    {
                        "type": "text",
                        "text": '<user_selected_skill name="fixture" version="1">INSTRUCTION_BODY_7821</user_selected_skill>',
                    },
                    {"type": "text", "text": "Request a staged action"},
                ]
            },
            invoke="c_11111111111141118111111111111111_27ce1d1bf427aecd",
        )
        assert "INSTRUCTION_BODY_7821" in json.dumps(captures[-1]["input"])
        assert any(
            "c_11111111111141118111111111111111_27ce1d1bf427aecd" in n for n in names
        ) and not any(
            "c_22222222222242228222222222222222_1b29de8941ee403a" in n for n in names
        ), names
        calls = [e for e in mcp_events if e["method"] == "tools/call"]
        assert len(calls) == 1 and calls[0]["scope"] == "Bearer synthetic-scope-a", (
            calls
        )
        await call("thread/unsubscribe", {"threadId": tid})
        await resume(
            tid,
            config={
                "mcp_servers.sunmoon_capabilities": {
                    "enabled": True,
                    "url": f"http://127.0.0.1:{server.server_port}/mcp",
                    "http_headers": {"Authorization": "Bearer synthetic-scope-b"},
                    "enabled_tools": [
                        "c_22222222222242228222222222222222_1b29de8941ee403a"
                    ],
                    "required": True,
                }
            },
        )
        names = await turn(
            tid,
            "managed_stage_b",
            invoke="c_22222222222242228222222222222222_1b29de8941ee403a",
        )
        assert any(
            "c_22222222222242228222222222222222_1b29de8941ee403a" in n for n in names
        ) and not any(
            "c_11111111111141118111111111111111_27ce1d1bf427aecd" in n for n in names
        ), names
        calls = [e for e in mcp_events if e["method"] == "tools/call"]
        assert len(calls) == 2 and calls[1]["scope"] == "Bearer synthetic-scope-b", (
            calls
        )
        await call("thread/unsubscribe", {"threadId": tid})
        await resume(
            tid,
            config={
                "mcp_servers.sunmoon_capabilities": {
                    "enabled": True,
                    "url": f"http://127.0.0.1:{server.server_port}/mcp",
                    "http_headers": {"Authorization": "Bearer synthetic-skill-scope"},
                    "enabled_tools": ["skill_read"],
                    "required": True,
                }
            },
        )
        before_skill = len(captures)
        names = await turn(
            tid,
            "lazy_skill_resource",
            extra={
                "input": [
                    {
                        "type": "text",
                        "text": "Available Skill: fixture; description: workflow guidance. Read references on demand using skill_read.",
                    }
                ]
            },
            invoke="skill_read",
        )
        assert "LAZY_REFERENCE_BODY_9182" not in json.dumps(
            captures[before_skill]["input"]
        )
        assert "LAZY_REFERENCE_BODY_9182" in json.dumps(
            captures[before_skill + 1]["input"]
        )
        assert any("skill_read" in n for n in names) and not any(
            "c_111111" in n or "c_222222" in n for n in names
        ), names
        calls = [e for e in mcp_events if e["method"] == "tools/call"]
        assert len(calls) == 3 and calls[-1]["scope"] == "Bearer synthetic-skill-scope"
        assert calls[-1]["params"]["arguments"]["path"] == "references/guide.md"
        await call("thread/unsubscribe", {"threadId": tid})
        await resume(
            tid,
            config={
                "mcp_servers.sunmoon_capabilities": {
                    "enabled": False,
                    "url": "http://127.0.0.1:9",
                    "enabled_tools": [],
                    "http_headers": {},
                    "required": False,
                }
            },
        )
        names = await turn(tid, "managed_disabled")
        assert not any("sunmoon_capabilities" in name for name in names), names
        assert len([e for e in mcp_events if e["method"] == "tools/call"]) == 3
        assert not any("requestApproval" in m.get("method", "") for m in notices), (
            notices
        )
        print(
            "PASS: empty-home start, no local execution, explicit instruction body, staged tool, per-turn header/whitelist rotation, lazy Skill reference read and disable",
            flush=True,
        )
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        for t in readers:
            t.cancel()
        server.shutdown()
        await asyncio.to_thread(outdir.mkdir, exist_ok=True)
        (outdir / "summary.json").write_text(
            json.dumps(
                {"version": version_text, "results": results, "mcp_events": mcp_events},
                indent=2,
            )
        )
        (outdir / "model_requests.json").write_text(json.dumps(captures, indent=2))
        (outdir / "stderr.log").write_text("".join(stderr_lines))


if __name__ == "__main__":
    asyncio.run(main())
