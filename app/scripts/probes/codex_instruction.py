"""Isolated Codex instruction rehearsal. Only synthetic data and loopback providers."""

import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import uuid4

from codex_capabilities_probe import Handler, captures, tool_names
from codex_probe_client import ProbeClient


# Serial HTTP handling makes the one-request check and fixture dispatch atomic.
class TrialServer(HTTPServer):
    first_request: int
    ask: bool
    limit_one: bool
    refused: int
    output: dict


class InstructionProvider(Handler):
    server: TrialServer

    def do_POST(self):
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        if self.server.limit_one and len(captures) >= self.server.first_request:
            self.server.refused += 1
            self.send_error(403, "The trial already sent its one provider request")
            return
        super().do_POST()

    def response_item(self, rid):
        if self.server.ask and len(captures) == self.server.first_request:
            return {
                "type": "function_call",
                "id": "fc_" + rid,
                "call_id": "call_" + rid,
                "name": "request_user_input",
                "arguments": json.dumps(
                    {
                        "questions": [
                            {
                                "id": "color",
                                "header": "Color",
                                "question": "Which color?",
                                "options": [
                                    {"label": "Blue", "description": "Use blue"},
                                    {"label": "Green", "description": "Use green"},
                                ],
                            }
                        ]
                    }
                ),
                "status": "completed",
            }
        item = super().response_item(rid)
        item["content"][0]["text"] = json.dumps(self.server.output, ensure_ascii=False)
        return item


class RelayServer(HTTPServer):
    fixture: TrialServer
    turn: str
    turn_ready: threading.Event


class MeteredRelay(BaseHTTPRequestHandler):
    """Two separate loopback sockets. Only a committed host claim reaches the fixture."""

    server: RelayServer

    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path != "/v1/responses" or not self.server.turn_ready.wait(10):
            self.send_error(403)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # Only model input and provider output cross this simulated Pod attach.
        # The provider, control ticket and model key are outside this container.
        print(json.dumps({"forward": body, "turn": self.server.turn}), flush=True)
        response = json.loads(sys.stdin.readline())
        if "events" not in response:
            self.server.fixture.refused += 1
            self.send_error(403, "Request has no dispatch permission")
            return
        captures.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(response["events"].encode())


def main():
    assert Path("/.dockerenv").exists() and {
        p.name for p in Path("/sys/class/net").iterdir()
    } == {"lo"}
    assert (
        subprocess.check_output(["codex", "--version"], text=True).strip()
        == "codex-cli 0.170.0"
    )
    metered = "--host-meter" in sys.argv
    job = json.loads(sys.stdin.readline()) if metered else json.load(sys.stdin)
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir()
    cwd = Path("/tmp/instruction-work")
    cwd.mkdir()
    server = TrialServer(("127.0.0.1", 0), InstructionProvider)
    if not metered:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    relay = None
    if metered:
        relay = RelayServer(("127.0.0.1", 0), MeteredRelay)
        relay.fixture = server
        relay.turn_ready = threading.Event()
        threading.Thread(target=relay.serve_forever, daemon=True).start()
    port = (relay or server).server_port
    (home / "config.toml").write_text(f"""
model = "instruction-test"
model_provider = "local"
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
[features]
multi_agent = false
multi_agent_v2 = false
apps = false
goals = false
shell_tool = false
view_image = false
default_mode_request_user_input = false
[model_providers.local]
name = "local fixture"
base_url = "http://127.0.0.1:{port}/v1"
wire_api = "responses"
supports_websockets = false
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0
""")
    with subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=cwd,
    ) as proc:
        client = ProbeClient(proc)
        try:
            client.initialize("instruction-test")
            runs = []
            old_markers = []
            scenarios = (
                ("question_bounded",)
                if metered
                else ("valid", "invalid_output", "question", "question_bounded")
            )
            for scenario in scenarios:
                marker = uuid4().hex
                workspace = cwd / scenario
                workspace.mkdir()
                start = len(captures)
                client.notices.clear()
                client.denied.clear()
                server.first_request = start + 1
                result_field = job["output_schema"]["required"][0]
                server.output = {
                    result_field: "ready" if scenario != "invalid_output" else 7
                }
                server.ask = scenario.startswith("question")
                server.limit_one = not metered and scenario != "question"
                server.refused = 0
                opened = client.call(
                    "thread/start",
                    {
                        "cwd": str(workspace),
                        "ephemeral": True,
                        "sandbox": "read-only",
                        "approvalPolicy": "never",
                        "dynamicTools": [],
                        "environments": [],
                        "selectedCapabilityRoots": [],
                        "model": "instruction-test",
                        "modelProvider": "local",
                        "allowProviderModelFallback": False,
                        "baseInstructions": f"Return the requested structured result. Trial {marker}.",
                    },
                )
                assert (
                    opened["model"] == "instruction-test"
                    and opened["modelProvider"] == "local"
                )
                assert (
                    opened["cwd"] == str(workspace)
                    and opened["approvalPolicy"] == "never"
                    and opened["sandbox"]["type"] == "readOnly"
                )
                tid = opened["thread"]["id"]
                params = {
                    "threadId": tid,
                    "environments": [],
                    "input": [
                        {
                            "type": "text",
                            "text": json.dumps(job["input"], ensure_ascii=False),
                        }
                    ],
                    "outputSchema": job["output_schema"],
                }
                turn = client.call("turn/start", params)["turn"]["id"]
                if relay:
                    relay.turn = turn
                    relay.turn_ready.set()
                while True:
                    events = [
                        n
                        for n in client.notices
                        if n.get("params", {}).get("threadId") == tid
                    ]
                    completed = [
                        e["params"]["turn"]
                        for e in events
                        if e["method"] == "turn/completed"
                        and e["params"]["turn"]["id"] == turn
                    ]
                    counts = [
                        e["params"]["tokenUsage"]
                        for e in events
                        if e["method"] == "thread/tokenUsage/updated"
                        and e["params"].get("turnId") == turn
                    ]
                    if completed and counts:
                        break
                    client.receive()
                assert completed[-1]["status"] == (
                    "failed" if scenario == "question_bounded" else "completed"
                )
                items = [
                    e["params"]["item"]
                    for e in events
                    if e["method"] == "item/completed"
                    and e["params"].get("turnId") == turn
                ]
                replies = [i["text"] for i in items if i["type"] == "agentMessage"]
                requests = captures[start:]
                assert requests and all(r["model"] == opened["model"] for r in requests)
                assert all(
                    set(tool_names(r)) <= {"request_user_input"} for r in requests
                )
                assert (
                    json.loads(requests[0]["input"][-1]["content"][0]["text"])
                    == job["input"]
                )
                assert requests[0]["text"]["format"]["schema"] == job["output_schema"]
                wire = json.dumps(requests)
                assert marker in wire and all(
                    previous not in wire for previous in old_markers
                )
                old_markers.append(marker)
                runs.append(
                    {
                        "case": scenario,
                        "thread": tid,
                        "turn": turn,
                        "provider": opened["modelProvider"],
                        "model": opened["model"],
                        "requests": len(requests),
                        "refused": server.refused,
                        "status": completed[-1]["status"],
                        "tools": tool_names(requests[0]),
                        "items": [i["type"] for i in items],
                        "denied": list(client.denied),
                        "usage": counts[-1]["total"],
                        "final": replies[-1] if replies else None,
                    }
                )
            assert len({r["thread"] for r in runs}) == len(runs)
            print(json.dumps(runs))
        finally:
            proc.terminate()
            if relay:
                relay.shutdown()
                relay.server_close()
            if not metered:
                server.shutdown()
            server.server_close()


if __name__ == "__main__":
    signal.alarm(60)
    main()
