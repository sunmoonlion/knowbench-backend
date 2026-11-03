"""Offline pinned-Codex protocol probe; mock HTTP provider, no model key or fees.

Run: python tests/probe_native_search_scope.py /path/to/codex-0.155.1
Uses a fresh temporary CODEX_HOME, never the user's sandbox or history.
"""

import http.server
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

BINARY = sys.argv[1]
requests = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append(request)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        item = {
            "id": "msg_" + str(len(requests)),
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "done", "annotations": []}],
        }
        for event in [
            {
                "type": "response.created",
                "response": {
                    "id": "r" + str(len(requests)),
                    "status": "in_progress",
                    "output": [],
                },
            },
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {
                "type": "response.completed",
                "response": {
                    "id": "r" + str(len(requests)),
                    "status": "completed",
                    "output": [item],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
            },
        ]:
            self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
            self.wfile.flush()


with tempfile.TemporaryDirectory(prefix="luna-stage-config-") as root:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    mcp = Path(root, "mcp.py")
    mcp.write_text("""import sys, json
for line in sys.stdin:
    q=json.loads(line)
    if "id" not in q: continue
    if q["method"] == "initialize":
        r={"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"probe","version":"1"}}
    elif q["method"] == "tools/list":
        r={"tools":[{"name":n,"description":"offline discovery stub","inputSchema":{"type":"object","properties":{}}} for n in ("find_subject","request_data")]}
    elif q["method"] == "tools/call":
        r={"content":[{"type":"text","text":"offline-only"}]}
    else: r={}
    print(json.dumps({"jsonrpc":"2.0","id":q["id"],"result":r}),flush=True)
""")
    Path(root, "config.toml").write_text(
        'model="probe"\nmodel_provider="probe"\nweb_search="cached"\n[model_providers.probe]\nname="probe"\nbase_url="http://127.0.0.1:'
        + str(server.server_port)
        + '/v1"\nwire_api="responses"\nrequires_openai_auth=false\n'
        + "[mcp_servers.probe]\ncommand="
        + json.dumps(sys.executable)
        + "\nargs=["
        + json.dumps(str(mcp))
        + "]\n"
    )
    env = {
        k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")
    }
    env["CODEX_HOME"] = root
    stderr = open(Path(root, "stderr.log"), "w")
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        endpoint = f"ws://127.0.0.1:{port_socket.getsockname()[1]}"
    p = subprocess.Popen(
        [BINARY, "app-server", "--listen", endpoint],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
        text=True,
        env=env,
        cwd=root,
    )
    connection = None
    messages = queue.Queue()

    def reader():
        try:
            for line in connection:
                messages.put(json.loads(line))
        except ConnectionClosed:
            pass

    seq = 0

    def call(method, params):
        global seq
        seq += 1
        connection.send(json.dumps({"id": seq, "method": method, "params": params}))
        end = time.monotonic() + 30
        while time.monotonic() < end:
            msg = messages.get(timeout=30)
            if msg.get("id") == seq:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result")
        raise TimeoutError(method)

    def unload(thread):
        assert (
            call("thread/unsubscribe", {"threadId": thread})["status"] == "unsubscribed"
        )

    def run(thread):
        inventory = call(
            "mcpServerStatus/list", {"threadId": thread, "detail": "toolsAndAuthOnly"}
        )
        names = {
            tool["name"]
            for server in inventory["data"]
            if server["name"] == "probe"
            for tool in server["tools"].values()
        }
        assert names == {"find_subject", "request_data"}
        count = len(requests)
        call(
            "turn/start",
            {"threadId": thread, "input": [{"type": "text", "text": "Say done."}]},
        )
        while True:
            msg = messages.get(timeout=30)
            if msg.get("method") == "turn/completed":
                break
        assert len(requests) == count + 1
        return [x["type"] for x in requests[-1]["tools"]]

    try:
        deadline = time.monotonic() + 10
        while connection is None:
            try:
                connection = connect(endpoint, open_timeout=1)
            except OSError:
                assert time.monotonic() < deadline, "app-server did not start"
                time.sleep(0.05)
        threading.Thread(target=reader, daemon=True).start()
        call(
            "initialize",
            {
                "clientInfo": {"name": "offline_probe", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        t = call(
            "thread/start",
            {
                "cwd": root,
                "environments": [],
                "approvalPolicy": "never",
                "sandbox": "read-only",
            },
        )["thread"]["id"]
        assert "web_search" in run(t)
        print("PASS: baseline search available", flush=True)
        call("thread/resume", {"threadId": t, "config": {"web_search": "disabled"}})
        assert "web_search" in run(t)
        print("PASS: loaded-thread resume ignores override (reproduced)", flush=True)
        unload(t)
        call("thread/resume", {"threadId": t, "config": {"web_search": "disabled"}})
        assert "web_search" not in run(t)
        print("PASS: unsubscribe/resume applies search restriction", flush=True)
        other = call(
            "thread/start",
            {
                "cwd": root,
                "environments": [],
                "approvalPolicy": "never",
                "sandbox": "read-only",
            },
        )["thread"]["id"]
        assert "web_search" in run(other)
        print("PASS: other thread keeps search", flush=True)
        unload(t)
        call("thread/resume", {"threadId": t, "config": {}})
        assert "web_search" in run(t)
        print("PASS: original search restored on same durable thread", flush=True)
        print("PASS: MCP discovery/proposal tools available in every phase", flush=True)
    finally:
        if connection is not None:
            connection.close()
        p.terminate()
        p.wait(timeout=10)
        server.shutdown()
        stderr.close()
