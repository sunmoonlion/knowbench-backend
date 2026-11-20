"""C6 uses the same account and connection across two conversations and a frozen expert."""

import json
import socket
import ssl
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import text
from test_capabilities_db import OWNER, SCHEMA, TOOLS, URL, approval, service
from test_formal_script_execution_db import mixed_expert
from test_workbench_ledger_db import db as db
from test_workbench_ledger_db import seed
from tool_invocation_support import request_call

from app.application.workbench.session_service import SessionService
from app.domain.workbench.capabilities import SelectionPut, tool_key
from app.infrastructure.workbench import capability_mcp
from app.infrastructure.workbench.repository import WorkbenchRepository


@pytest.fixture
def connector(monkeypatch, tmp_path):
    """Supply the approved DNS name and route its TLS socket to the test server."""
    host = httpx.URL(URL).host
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            f"/CN={host}",
            "-addext",
            f"subjectAltName=DNS:{host}",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    calls = []
    drop_after_send = set()
    tools = [
        *TOOLS,
        {
            "name": "lookup",
            "description": "Read one synthetic record",
            "inputSchema": SCHEMA,
        },
    ]

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            assert self.path == "/mcp" and self.headers["Host"] == host
            assert self.headers["Authorization"] == "Bearer only-server-knows"
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = message["method"]
            if method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if method == "initialize":
                result = {"protocolVersion": "2025-06-18"}
            elif method == "tools/list":
                result = {"tools": tools}
            else:
                assert method == "tools/call"
                name, args = message["params"]["name"], message["params"]["arguments"]
                calls.append((URL, "office", name, args))
                if name == "send" and args["to"] in drop_after_send:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                result = {"content": [], "structuredContent": {"receipt": args["to"]}}
            data = json.dumps(
                {"jsonrpc": "2.0", "id": message["id"], "result": result}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class LocalTLS(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            assert (
                request.url.host == "127.0.0.1"
                and request.extensions["sni_hostname"] == host
            )
            request.url = request.url.copy_with(port=server.server_port)
            return await super().handle_async_request(request)

    client = httpx.AsyncClient

    def local_client(**options):
        return client(
            transport=LocalTLS(verify=ssl.create_default_context(cafile=cert)),
            **options,
        )

    monkeypatch.setattr(
        capability_mcp, "public_address", AsyncMock(return_value=("127.0.0.1", host))
    )
    monkeypatch.setattr(capability_mcp.httpx, "AsyncClient", local_client)
    remote = capability_mcp.RemoteMcp((URL,))
    remote.calls = calls
    try:
        yield remote, drop_after_send
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def test_same_connection_across_chat_work_and_expert(db, connector):
    remote, drop_after_send = connector
    asset_id, policies, task_id = await mixed_expert(
        db, "success", shared_connector=remote
    )
    _, _, work = await seed(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        expert_calls = [
            c for c in await repo.list_task_calls(OWNER, task_id) if c["connection_id"]
        ]
        assert {str(c["connection_id"]) for c in expert_calls} == {asset_id}
        assert sorted(c["detail"]["tool"]["name"] for c in expert_calls) == [
            "lookup",
            "send",
        ]
        chat = (await SessionService(repo).start(owner_actor_id=OWNER, kind="chat"))[
            "session_id"
        ]
    approvals = {}
    for scene, sid in (("chat", chat), ("work", work)):
        async with db() as session:
            repo = WorkbenchRepository(session)
            svc = service(repo, remote, operations=policies)
            assert (await repo.get_session(sid))["kind"] == scene
            keys = {name: tool_key(asset_id, name) for name in ("lookup", "send")}
            view = await svc.selection(OWNER, sid)
            await svc.save_selection(
                OWNER,
                sid,
                SelectionPut(
                    revision=view["revision"],
                    tools=list(keys.values()),
                    purpose="Read and deliver synthetic records",
                ),
            )
            prepared = await svc.prepare(OWNER, sid)
            token = prepared["config"]["mcp_servers.sunmoon_capabilities"][
                "http_headers"
            ]["Authorization"].split()[1]
            for name, decision, outcome in (
                ("lookup", "accept", "succeeded"),
                ("send", "decline", "declined"),
                ("send", "accept", "succeeded"),
                ("send", "accept", "unknown"),
            ):
                recipient = f"{scene}-{name}-{outcome}@example.test"
                args = {"to": "shared-record" if name == "lookup" else recipient}
                if outcome == "unknown":
                    drop_after_send.add(recipient)
                before = len(remote.calls)
                staged = await request_call(
                    svc, sid, token, keys[name], args, call_id=recipient
                )
                call = await repo.capability_call(OWNER, staged["call_id"])
                assert (
                    call["status"] == "awaiting_approval"
                    and len(remote.calls) == before
                )
                assert (
                    call["task_id"] is None and str(call["connection_id"]) == asset_id
                )
                answer = approval(call, decision).model_copy(
                    update={"remember_read": name == "lookup"}
                )
                result = await svc.decide(OWNER, call["id"], answer)
                if name == "lookup":
                    approvals[scene] = (sid, token, keys[name], call["id"])
                assert result["status"] == outcome
                if outcome == "unknown":
                    async with db() as restarted:
                        fresh = service(
                            WorkbenchRepository(restarted), remote, operations=policies
                        )
                        original = await request_call(
                            fresh, sid, token, keys[name], args, call_id=recipient
                        )
                        assert original["call_id"] == call["id"]
                        assert original["status"] == "unknown"
                assert len(remote.calls) == before + (decision == "accept")
                if decision == "accept":
                    assert remote.calls[-1][2:] == (name, args)

    for scene, (sid, token, key, original_id) in approvals.items():
        async with db() as session:
            repo = WorkbenchRepository(session)
            svc = service(repo, remote, operations=policies)
            args = {"to": "shared-record"}
            reused = await request_call(svc, sid, token, key, args, call_id="reuse")
            assert reused["status"] == "succeeded"
            receipt = await repo.capability_call(OWNER, reused["call_id"])
            assert receipt["detail"]["read_approval_id"] == original_id
            if scene == "chat":
                await svc.revoke_read_approval(OWNER, original_id)
            else:
                async with repo.transaction():
                    await session.execute(
                        text(
                            "UPDATE workbench_calls SET read_scope_until=now() WHERE id=:id"
                        ),
                        {"id": original_id},
                    )
            before = len(remote.calls)
            staged = await request_call(
                svc, sid, token, key, args, call_id="after-scope"
            )
            assert (
                staged["status"] == "awaiting_approval" and len(remote.calls) == before
            )
