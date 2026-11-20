"""Exercise thread-scoped model routing in the pinned, offline sandbox image."""

import json
import os
import signal
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from codex_capabilities_probe import Handler, captures


def main():
    assert Path("/.dockerenv").is_file()
    assert {p.name for p in Path("/sys/class/net").iterdir()} == {"lo"}
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir()
    assert subprocess.check_output(["codex", "--version"], text=True).strip() == (
        "codex-cli 0.170.0"
    )
    config = ['model = "primary-test"', 'model_provider = "primary"']
    order = []

    class CaptureProvider(Handler):
        def do_POST(self):
            if self.path == "/v1/responses":
                order.append(self.server.server_port)
            super().do_POST()

    servers = {}
    for name in ("primary", "review"):
        server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureProvider)
        servers[name] = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        config.extend(
            [
                f"[model_providers.{name}]",
                f'name = "{name}"',
                f'base_url = "http://127.0.0.1:{server.server_port}/v1"',
                'wire_api = "responses"',
                "supports_websockets = false",
                "requires_openai_auth = false",
                "request_max_retries = 0",
            ]
        )
    (home / "config.toml").write_text("\n".join(config))
    with subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd="/tmp",
    ) as proc:
        events = []
        seq = 0

        def receive():
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("app-server closed")
            msg = json.loads(line)
            if "method" in msg:
                events.append(msg)
            return msg

        def call(method, params):
            nonlocal seq
            seq += 1
            print(
                json.dumps(dict(id=seq, method=method, params=params)),
                file=proc.stdin,
                flush=True,
            )
            while True:
                msg = receive()
                if msg.get("id") == seq:
                    assert "result" in msg, msg
                    return msg["result"]

        try:
            call(
                "initialize",
                {
                    "clientInfo": {"name": "review-probe", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            print('{"method":"initialized"}', file=proc.stdin, flush=True)
            threads = {}
            for name in servers:
                result = call(
                    "thread/start",
                    {
                        "model": name + "-test",
                        "modelProvider": name,
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                    },
                )
                assert (
                    result["model"] == name + "-test"
                    and result["modelProvider"] == name
                )
                threads[name] = result["thread"]["id"]
            assert len(set(threads.values())) == 2
            for name in ("primary", "review", "primary"):
                before = len(events)
                tid = threads[name]
                turn = call(
                    "turn/start",
                    {
                        "threadId": tid,
                        "input": [{"type": "text", "text": "synthetic routing check"}],
                    },
                )["turn"]["id"]

                def notices(method, before=before, tid=tid):
                    return [
                        e["params"]
                        for e in events[before:]
                        if e["method"] == method and e["params"].get("threadId") == tid
                    ]

                while not any(
                    e["turn"]["id"] == turn for e in notices("turn/completed")
                ):
                    receive()
                assert notices("turn/completed")[-1]["turn"]["status"] == "completed"
                assert order[-1] == servers[name].server_port
                assert captures[-1]["model"] == name + "-test"
                usage = notices("thread/tokenUsage/updated")
                while not any(e["turnId"] == turn for e in usage):
                    receive()
                    usage = notices("thread/tokenUsage/updated")
                usage = [e for e in usage if e["turnId"] == turn]
                print(
                    name,
                    captures[-1]["model"],
                    sorted(usage[-1]),
                    sorted(usage[-1]["tokenUsage"]),
                )
            assert len(order) == len(captures) == 3
        finally:
            proc.terminate()
            for server in servers.values():
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    signal.alarm(60)
    main()
