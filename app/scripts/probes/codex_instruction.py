"""Isolated Codex instruction rehearsal. Only synthetic data and loopback providers."""

import json
import os
import signal
import subprocess
import sys
import threading
from http.server import HTTPServer
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


def main():
    assert Path("/.dockerenv").exists() and {
        p.name for p in Path("/sys/class/net").iterdir()
    } == {"lo"}
    assert (
        subprocess.check_output(["codex", "--version"], text=True).strip()
        == "codex-cli 0.170.0"
    )
    job = json.load(sys.stdin)
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir()
    cwd = Path("/tmp/instruction-work")
    cwd.mkdir()
    server = TrialServer(("127.0.0.1", 0), InstructionProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
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
base_url = "http://127.0.0.1:{server.server_port}/v1"
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
            for scenario in ("valid", "invalid_output", "question", "question_bounded"):
                marker = uuid4().hex
                workspace = cwd / scenario
                workspace.mkdir()
                start = len(captures)
                client.notices.clear()
                client.denied.clear()
                server.first_request = start + 1
                server.output = {
                    "summary": "ready" if scenario != "invalid_output" else 7
                }
                server.ask = scenario.startswith("question")
                server.limit_one = scenario != "question"
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
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    signal.alarm(60)
    main()
