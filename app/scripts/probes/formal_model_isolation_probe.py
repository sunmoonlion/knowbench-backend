"""Verify the pinned Codex caller profile against real outbound tool schemas.

Run only inside a disposable network-none container. The model and MCP are
loopback fixtures, with no credentials, external services or billable requests.
"""

import asyncio
import io
import json
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import codex_capabilities_probe as fixture

TOOL = "formal_probe_call"
PROFILE = {
    "features.shell_tool": False,
    "features.view_image": False,
    "features.multi_agent": False,
    "features.apps": False,
    "features.goals": False,
    "features.default_mode_request_user_input": False,
    "web_search": "disabled",
}


class Handler(fixture.Handler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        request = json.loads(body)
        if self.path == "/mcp" and request.get("method") == "tools/list":
            data = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "tools": [
                            {
                                **fixture.tool(name),
                                "annotations": {
                                    "readOnlyHint": False,
                                    "destructiveHint": False,
                                    "openWorldHint": False,
                                },
                            }
                            for name in (TOOL, "unrelated_tool")
                        ]
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.rfile = io.BytesIO(body)
        super().do_POST()


async def main():
    assert await asyncio.to_thread(Path("/.dockerenv").exists)
    assert await asyncio.to_thread(
        lambda: {p.name for p in Path("/sys/class/net").iterdir()}
    ) == {"lo"}
    home = Path(os.environ["CODEX_HOME"])
    await asyncio.to_thread(home.mkdir, parents=True, exist_ok=False)
    cwd = Path("/tmp/formal-probe-work")
    await asyncio.to_thread(cwd.mkdir)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home.joinpath("config.toml").write_text(f"""model = "test-model"
model_provider = "probe"
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "cached"
[features]
shell_tool = true
multi_agent = {str(os.environ.get("PERMISSIVE_STARTUP") == "1").lower()}
apps = true
goals = false
default_mode_request_user_input = false
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
    binary = "/usr/local/bin/codex"
    version = await asyncio.create_subprocess_exec(
        binary, "--version", stdout=asyncio.subprocess.PIPE
    )
    version_text = (await version.communicate())[0].decode().strip()
    assert version_text == "codex-cli 0.170.0", version_text
    proc = await asyncio.create_subprocess_exec(
        binary,
        "app-server",
        "--listen",
        "stdio://",
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdin, stdout, error_stream = proc.stdin, proc.stdout, proc.stderr
    assert stdin and stdout and error_stream
    pending, notices, errors, rows = {}, [], [], []
    number = 0

    async def read():
        while line := await stdout.readline():
            message = json.loads(line)
            if "method" in message:
                notices.append(message)
            elif message.get("id") in pending:
                pending[message["id"]].set_result(message)

    async def stderr():
        while line := await error_stream.readline():
            errors.append(line.decode(errors="replace"))

    readers = [asyncio.create_task(read()), asyncio.create_task(stderr())]

    async def rpc(method, params):
        nonlocal number
        number += 1
        key = number
        pending[key] = asyncio.get_running_loop().create_future()
        stdin.write(
            (
                json.dumps({"id": key, "method": method, "params": params}) + "\n"
            ).encode()
        )
        await stdin.drain()
        response = await asyncio.wait_for(pending[key], 30)
        pending.pop(key)
        if "error" in response:
            raise RuntimeError((method, response["error"]))
        return response["result"]

    def connection(enabled, tools):
        return {
            "url": f"http://127.0.0.1:{server.server_port}/mcp",
            "enabled": enabled,
            "enabled_tools": tools,
            "http_headers": {},
            "required": enabled,
        }

    async def turn(tid, label, invoke=False):
        before = len(fixture.captures)
        fixture.next_model_tool = TOOL if invoke else None
        response = await rpc(
            "turn/start",
            {
                "threadId": tid,
                "environments": [],
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                "input": [{"type": "text", "text": label}],
            },
        )
        turn_id = response["turn"]["id"]
        for _ in range(600):
            done = [
                n
                for n in notices
                if n.get("method") == "turn/completed"
                and n["params"]["turn"]["id"] == turn_id
            ]
            if done:
                assert done[-1]["params"]["turn"]["status"] == "completed", done
                break
            await asyncio.sleep(0.05)
        else:
            raise TimeoutError(label)
        captured = fixture.captures[before:]
        assert len(captured) == (2 if invoke else 1)
        names = [fixture.tool_names(c) for c in captured]
        rows.append(
            {"case": label, "thread_id": tid, "turn_id": turn_id, "tools": names}
        )
        print(json.dumps(rows[-1]), flush=True)
        return names

    try:
        initialized = await rpc(
            "initialize",
            {
                "clientInfo": {"name": "formal-model-isolation", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        rows.append({"case": "initialize", "result": initialized})
        print(json.dumps(rows[-1]), flush=True)
        stdin.write(b'{"method":"initialized"}\n')
        await stdin.drain()
        for method in ("config/read", "experimentalFeature/list"):
            try:
                reply = await rpc(method, {})
                safe = reply.get("data", reply.get("config", {}).get("features", reply))
                rows.append({"case": method, "result": safe})
                print(json.dumps(rows[-1]), flush=True)
            except RuntimeError as exc:
                print(str(exc), flush=True)
        started = await rpc(
            "thread/start",
            {
                "cwd": str(cwd),
                "environments": [],
                "approvalPolicy": "never",
                "config": {
                    "mcp_servers.sunmoon_capabilities": connection(
                        True, [TOOL, "unrelated_tool"]
                    ),
                    "mcp_servers.other_scope": connection(True, ["unrelated_tool"]),
                },
            },
        )
        tid = started["thread"]["id"]
        baseline = await turn(tid, "ordinary_before_probe")
        assert any("unrelated_tool" in name for name in baseline[0]), baseline
        await rpc("thread/unsubscribe", {"threadId": tid})
        await rpc(
            "thread/resume",
            {
                "threadId": tid,
                "excludeTurns": True,
                "config": {
                    **PROFILE,
                    "mcp_servers.sunmoon_capabilities": connection(True, [TOOL]),
                    "mcp_servers.other_scope": connection(False, []),
                },
            },
        )
        names = await turn(tid, "formal_probe_after_resume", invoke=True)
        calls = [e for e in fixture.mcp_events if e["method"] == "tools/call"]
        assert len(calls) == 1 and calls[0]["params"]["name"] == TOOL, calls
        completed = [
            n["params"]["item"]
            for n in notices
            if n.get("method") == "item/completed"
            and n["params"].get("item", {}).get("type") == "mcpToolCall"
        ]
        assert len(completed) == 1 and completed[0]["status"] == "completed", completed
        expected = sorted(
            [
                "mcp__sunmoon_capabilities." + TOOL,
                "list_mcp_resources",
                "list_mcp_resource_templates",
                "read_mcp_resource",
                "request_user_input",
            ]
        )
        if os.environ.get("PERMISSIVE_STARTUP") == "1":
            assert any("multi_agent_v1" in name for name in names[0])
            print(
                "PASS negative: permissive startup is not safely narrowed by resume",
                flush=True,
            )
            return
        assert all(row == expected for row in names), names
        print(
            "PASS: the resumed profile exposes the candidate, MCP metadata and user-question helpers; no commands, agents, goals, search or other MCP tools",
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
        for reader in readers:
            reader.cancel()
        server.shutdown()
        output = Path("/tmp/evidence")
        await asyncio.to_thread(output.mkdir, exist_ok=True)
        (output / "summary.json").write_text(
            json.dumps(
                {"version": version_text, "profile": PROFILE, "results": rows}, indent=2
            )
        )
        (output / "model_requests.json").write_text(
            json.dumps(fixture.captures, indent=2)
        )
        (output / "events.json").write_text(json.dumps(notices, indent=2))
        (output / "stderr.log").write_text("".join(errors))


if __name__ == "__main__":
    asyncio.run(main())
