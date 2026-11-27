"""Exercise account-bound OAuth from the HTTP callback to approved tool dispatch."""

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import text
from test_capabilities_db import CIPHER, OPERATION, TOOLS, URL, Remote, approval, body
from test_capability_routes_db import application, principal
from test_workbench_ledger_db import OTHER, OWNER, seed
from test_workbench_ledger_db import db as db
from tool_invocation_support import request_call

from app.application.workbench.capabilities import Capabilities
from app.application.workbench.connection_oauth import A2A_TASK
from app.domain.workbench.capabilities import (
    AssetAction,
    CapabilityError,
    CapabilityReauthRequired,
    SelectionPut,
    tool_key,
)
from app.domain.workbench.connection_oauth import OAuthProvider, OAuthStart
from app.domain.workbench.tool_registry import ToolRegistration
from app.infrastructure.workbench import connection_oauth as adapter
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.interfaces.endpoints import capability_routes
from app.interfaces.http.middleware.auth import get_web_current_user

A2A = "https://agent.example.test/rpc"
ORIGIN = "https://bench.example.test"
BASE = "/api/workbench/capabilities"
AGENT_OP = OPERATION.model_copy(
    update={
        "service_url": A2A,
        "tool_name": A2A_TASK["name"],
        "effect": "read",
        "operation": "read",
        "descriptor_digest": ToolRegistration.discover(
            "a2a", A2A, A2A_TASK
        ).descriptor_digest,
        "resources": (),
    }
)


class Provider:
    def __init__(self, db, monkeypatch):
        self.db = db
        self.remote = Remote()
        self.now = datetime.now(UTC)
        self.account = "external-one"
        self.grants = []
        self.revoked = []
        self.tasks = []
        self.operations = (OPERATION, AGENT_OP)
        self.requests = {}
        self.started = asyncio.Event()
        self.resume = asyncio.Event()
        self.resume.set()
        self.deny_refresh = self.deny_call = False
        self.scopes = "use"
        self.issue_refresh = True
        self.providers = []
        for protocol, url, name in [
            ("a2a", A2A, A2A_TASK["name"]),
            ("mcp", URL, TOOLS[0]["name"]),
        ]:
            self.providers.append(
                OAuthProvider(
                    provider_id=protocol,
                    protocol=protocol,
                    service_url=url,
                    issuer="https://issuer.example.test",
                    authorization_url="https://issuer.example.test/authorize",
                    token_url="https://issuer.example.test/token",
                    revocation_url="https://issuer.example.test/revoke",
                    account_url=url.rsplit("/", 1)[0] + "/me",
                    client_id="app",
                    scopes=("use",),
                    tool_scopes={name: ("use",)},
                )
            )

        async def address(url):
            return "192.0.2.1", urlsplit(url).hostname

        monkeypatch.setattr(adapter, "public_address", address)
        self.transport = adapter.OAuthTransport(
            transport=httpx.MockTransport(self.respond)
        )

    async def respond(self, req):
        assert req.url.host == "192.0.2.1"
        assert req.headers["host"] == req.extensions["sni_hostname"]
        if req.url.path == "/token":
            fields = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            self.grants.append(fields["grant_type"])
            assert fields["resource"] in (URL, A2A) and fields["client_id"] == "app"
            if fields["grant_type"] == "authorization_code":
                challenge, redirect = self.requests.pop(fields["code"])
                assert fields["redirect_uri"] == redirect
                assert (
                    base64.urlsafe_b64encode(
                        hashlib.sha256(fields["code_verifier"].encode()).digest()
                    )
                    .decode()
                    .rstrip("=")
                    == challenge
                )
            else:
                self.started.set()
                await self.resume.wait()
                if self.deny_refresh:
                    return httpx.Response(400, json={"error": "invalid_grant"})
            number = len(self.grants)
            return httpx.Response(
                200,
                json={
                    "access_token": f"test-access-{number}",
                    "expires_in": 60,
                    "token_type": "Bearer",
                    "scope": self.scopes,
                    **(
                        {"refresh_token": f"test-refresh-{number}"}
                        if self.issue_refresh
                        else {}
                    ),
                },
            )
        if req.url.path == "/revoke":
            self.revoked.append(parse_qs(req.content.decode())["token"][0])
            return httpx.Response(200)
        assert req.headers["authorization"].startswith("Bearer test-access-")
        if req.url.path == "/me":
            return httpx.Response(
                200, json={"sub": self.account, "name": "Same display name"}
            )
        call = json.loads(req.content)
        assert call["method"] == "GetTask" and call["params"]["historyLength"] == 0
        self.tasks.append(call["params"]["id"])
        if self.deny_call:
            return httpx.Response(401)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": call["id"],
                "result": {
                    "id": call["params"]["id"],
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": ["private"],
                },
            },
        )

    def service(self, session):
        svc = Capabilities(
            WorkbenchRepository(session),
            self.remote,
            CIPHER,
            oauth_providers=self.providers,
            oauth_transport=self.transport,
            web_origin=ORIGIN,
            runtime_url="http://test/api/mcp/capabilities",
            operations=self.operations,
        )
        svc.oauth.now = lambda: self.now
        return svc

    def grant(self, start):
        query = parse_qs(urlsplit(start["authorization_url"]).query)
        assert query["code_challenge_method"] == ["S256"]
        state = query["state"][0]
        self.requests[state] = (query["code_challenge"][0], query["redirect_uri"][0])
        return state

    async def connect(self, protocol="a2a", asset=None):
        async with self.db() as s:
            svc = self.service(s)
            target = (
                {"connection_id": asset["id"], "revision": asset["revision"]}
                if asset
                else {}
            )
            state = self.grant(
                await svc.oauth.start(
                    OWNER, OAuthStart(provider_id=protocol, name="Account", **target)
                )
            )
            await svc.oauth.finish(OWNER, protocol, state, None, state, False)
            assets = await svc.assets(OWNER)
            return next(
                a
                for a in assets
                if a["detail"]["oauth"]["account_id"] == self.account
                and a["detail"]["oauth"]["protocol"] == protocol
            )

    async def lease(self, asset):
        async with self.db() as s:
            return await self.service(s).oauth.credential(
                OWNER, asset["id"], asset["revision"]
            )

    def expire(self):
        self.now += timedelta(seconds=61)


async def test_callback_ownership_expiry_pkce_replay_and_account_switch(
    db, monkeypatch
):
    provider = Provider(db, monkeypatch)
    app = application(db, provider.remote)

    async def svc():
        async with db() as session:
            yield provider.service(session)

    app.dependency_overrides[capability_routes.service] = svc
    owner = OWNER
    app.dependency_overrides[get_web_current_user] = lambda: principal(owner)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as http:
        catalog = (await http.get(BASE)).json()
        assert catalog["services"] and all(
            s["authentication"] == ["oauth"] for s in catalog["services"]
        )

        async def start():
            res = await http.post(
                BASE + "/oauth/start",
                json={"provider_id": "mcp", "name": "MCP", "locale": "en"},
            )
            assert res.status_code == 200, res.text
            return provider.grant(res.json())

        async def finish(state, **params):
            return await http.get(
                BASE + "/oauth/mcp/callback",
                params={"state": state, "code": state, **params},
            )

        state = await start()
        declined = await http.get(
            BASE + "/oauth/mcp/callback",
            params={"state": state, "error": "access_denied"},
        )
        assert (
            declined.status_code == 303
            and "authorization=incomplete" in declined.headers["location"]
        )
        assert (await finish(state)).status_code == 409 and not provider.grants
        state = await start()
        owner = OTHER
        assert (await finish(state)).status_code == 409
        owner = OWNER
        assert (await finish(state, iss="https://wrong.example")).status_code == 409
        assert (await finish(state)).status_code == 409 and not provider.grants
        state = await start()
        async with db() as s:
            await s.execute(
                text("UPDATE workbench_connection_oauth SET expires_at=now()")
            )
            await s.commit()
        assert (await finish(state)).status_code == 409
        state = await start()
        result = await finish(state)
        assert (
            result.status_code == 303
            and result.headers["location"]
            == ORIGIN + "/en/workbench/settings/connections"
        )
        assert (await finish(state)).status_code == 409
        public = (await http.get(BASE)).json()
        assert "test-access-" not in json.dumps(
            public
        ) and "test-refresh-" not in json.dumps(public)
        asset = public["assets"][0]
        assert asset["detail"]["oauth"]["account_id"] == "external-one"
        assert (
            await http.post(BASE + "/connections", json=body().model_dump(mode="json"))
        ).status_code == 409
    provider.account = "external-two"
    with pytest.raises(CapabilityError, match="different account"):
        await provider.connect("mcp", asset)
    second = await provider.connect("mcp")
    assert second["id"] != asset["id"]
    async with db() as s:
        stored = await WorkbenchRepository(s).capability_asset(OWNER, asset["id"])
        assert (
            stored["revision"] == asset["revision"]
            and stored["detail"]["oauth"]["account_id"] == "external-one"
        )
        assert "test-access-" not in stored["secret"]
    provider.account = "external-one"
    again = await provider.connect("mcp", asset)
    assert again["revision"] == asset["revision"] + 1
    with pytest.raises(CapabilityReauthRequired):
        await provider.lease(asset)


@pytest.mark.parametrize("protocol", ["mcp", "a2a"])
async def test_actual_approved_operation_uses_bound_account(db, monkeypatch, protocol):
    p = Provider(db, monkeypatch)
    _, _, sid = await seed(db)
    asset = await p.connect(protocol)
    key = tool_key(asset["id"], asset["detail"]["tools"][0]["name"])
    async with db() as s:
        svc = p.service(s)
        current = await svc.selection(OWNER, sid)
        await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=current["revision"], tools=[key], purpose="Get one result"
            ),
        )
        runtime = await svc.prepare(OWNER, sid)
        token = runtime["config"]["mcp_servers.sunmoon_capabilities"]["http_headers"][
            "Authorization"
        ].split()[1]
        args = {"to": "recipient"} if protocol == "mcp" else {"id": "task-1"}
        req = await request_call(svc, sid, token, key, args)
        call = await svc.repo.capability_call(OWNER, req["call_id"])
        assert not p.tasks and not p.remote.calls
        p.expire()
        done = await svc.decide(OWNER, call["id"], approval(call))
        assert done["status"] == "succeeded", done
        assert "private" not in json.dumps(done, default=str)
        await svc.decide(OWNER, call["id"], approval(call))
        if protocol == "a2a":
            assert p.tasks == ["task-1"]
        else:
            assert p.remote.calls == [(URL, "test-access-2", "send", args)]
        assert p.grants == ["authorization_code", "refresh_token"]


@pytest.mark.parametrize("disconnect", [False, True])
async def test_parallel_rotation_and_local_disconnect_wins(db, monkeypatch, disconnect):
    p = Provider(db, monkeypatch)
    asset = await p.connect()
    p.expire()
    p.resume.clear()
    one = asyncio.create_task(p.lease(asset))
    await asyncio.wait_for(p.started.wait(), 3)
    two = asyncio.create_task(p.lease(asset))
    try:
        if disconnect:
            async with db() as s:
                svc = p.service(s)
                removed = await svc.action(
                    OWNER,
                    asset["id"],
                    AssetAction(action="disconnect", revision=asset["revision"]),
                )
                assert "unconfirmed" in removed["detail"]["revocation"]
    finally:
        p.resume.set()
    result = await asyncio.gather(one, two, return_exceptions=True)
    assert p.grants.count("refresh_token") == 1
    if disconnect:
        assert all(isinstance(x, CapabilityReauthRequired) for x in result)
        assert p.revoked == [
            "test-refresh-1",
            "test-access-1",
            "test-refresh-2",
            "test-access-2",
        ]
        with pytest.raises(CapabilityReauthRequired):
            await p.lease(asset)
    else:
        assert result[0] == result[1] and result[0][1] == "test-access-2"


@pytest.mark.parametrize(
    "mode", ["no_refresh", "revoked", "account_change", "scope_change", "remote_401"]
)
async def test_expired_or_revoked_grant_cannot_authorize_next_call(
    db, monkeypatch, mode
):
    p = Provider(db, monkeypatch)
    p.issue_refresh = mode != "no_refresh"
    asset = await p.connect()
    if mode != "remote_401":
        p.expire()
    p.deny_refresh = mode == "revoked"
    p.deny_call = mode == "remote_401"
    if mode == "account_change":
        p.account = "external-two"
    if mode == "scope_change":
        p.scopes = ""
    async with db() as s:
        svc = p.service(s)
        await svc.action(
            OWNER, asset["id"], AssetAction(action="default_on", revision=1)
        )
        with pytest.raises(CapabilityReauthRequired):
            await svc.oauth.call(
                OWNER,
                asset,
                {"revision": 1, "tool": A2A_TASK, "arguments": {"id": "one"}},
            )
        current = await svc.repo.capability_asset(OWNER, asset["id"])
        assert current["status"] == "reauth_required" and current["secret"] is None
        assert (
            not current["default_selected"] and current["revision"] > asset["revision"]
        )
    with pytest.raises(CapabilityReauthRequired):
        await p.lease(asset)


async def test_rewrap_preserves_pending_authorization_rotating_claim_and_old_bearer(
    db, monkeypatch
):
    from cryptography.fernet import Fernet, InvalidToken

    from app.infrastructure.workbench.connection_keys import (
        ConnectionKeys,
        rotate_connection_keys,
    )

    p = Provider(db, monkeypatch)
    previous, primary = Fernet.generate_key(), Fernet.generate_key()
    old = ConnectionKeys([previous])
    ring = ConnectionKeys([primary, previous])
    original = p.service

    def service(s):
        svc = original(s)
        svc.cipher = svc.oauth.cipher = ring
        return svc

    p.service = service
    async with db() as s:
        svc = p.service(s)
        svc.oauth.cipher = old
        pending = p.grant(
            await svc.oauth.start(OWNER, OAuthStart(provider_id="mcp", name="Pending"))
        )
        first = p.grant(
            await svc.oauth.start(OWNER, OAuthStart(provider_id="a2a", name="First"))
        )
        await svc.oauth.finish(OWNER, "a2a", first, None, first, False)
        asset = (await svc.assets(OWNER))[0]
        legacy = Capabilities(WorkbenchRepository(s), Remote(), old)
        bearer = await legacy.create_connection(OWNER, body())
    p.expire()
    p.resume.clear()

    async def rotating_worker():
        async with db() as s:
            worker = p.service(s)
            worker.oauth.cipher = ConnectionKeys([previous, primary])
            return await worker.oauth.credential(OWNER, asset["id"], 1)

    one = asyncio.create_task(rotating_worker())
    await asyncio.wait_for(p.started.wait(), 3)
    try:
        async with db() as s, s.begin():
            assert await rotate_connection_keys(s, ring) == 3
    finally:
        p.resume.set()
    assert (await one)[1] == "test-access-2"
    async with db() as s, s.begin():
        # A worker that still writes the old primary must be upgraded before retirement.
        assert await rotate_connection_keys(s, ring) == 1
        assert await rotate_connection_keys(s, ring) == 0
    async with db() as s:
        svc = p.service(s)
        svc.cipher = svc.oauth.cipher = ConnectionKeys([primary])
        assert (await svc.oauth.credential(OWNER, asset["id"], 1))[1] == "test-access-2"
        await svc.oauth.finish(OWNER, "mcp", pending, None, pending, False)
        raw = await svc.repo.capability_asset(OWNER, bearer["id"])
        assert svc.secret(raw) == "only-server-knows"
        with pytest.raises(InvalidToken):
            old.decrypt(raw["secret"].encode())


async def test_interrupted_rotation_is_never_replayed(db, monkeypatch):
    p = Provider(db, monkeypatch)
    asset = await p.connect()
    p.expire()
    p.resume.clear()
    first = asyncio.create_task(p.lease(asset))
    await asyncio.wait_for(p.started.wait(), 3)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    p.expire()
    with pytest.raises(CapabilityReauthRequired):
        await p.lease(asset)
    assert p.grants.count("refresh_token") == 1


def test_oauth_callback_access_log_omits_all_parameters():
    from app.application.sensitive_paths import redact_sensitive_path

    path = BASE + "/oauth/agent/callback"
    assert redact_sensitive_path(path + "?state=test-state&code=test-code") == path


def test_connection_preview_resolves_required_optional_and_missing_versions():
    from types import SimpleNamespace as Node

    from app.application.workbench.formal_method_catalog import connection_preview

    def ref(name):
        return Node(method_id=name, version=1, digest=name)

    def requirement(service):
        return Node(
            requirement_id=service,
            data_transfer=Node(destination=service),
            capability_ref=service,
            version="1",
        )

    def leaf(*services):
        return Node(
            procedure=Node(kind="tool"),
            access_requirements=[requirement(s) for s in services],
        )

    def choice(required, *children):
        return Node(
            always_required=required,
            inputs={},
            candidates=[ref(child) for child in children],
        )

    def group(*steps):
        return Node(
            procedure=Node(kind="orchestration", steps=steps), access_requirements=[]
        )

    definitions = {
        (name, 1, name): item
        for name, item in {
            "root": group(choice(True, "nested"), choice(False, "optional")),
            "nested": group(choice(True, "a", "b")),
            "a": leaf("common", "branch"),
            "b": leaf("common"),
            "optional": leaf("extra"),
        }.items()
    }
    result = connection_preview(ref("root"), definitions)
    assert result["complete"] and {
        s["url"]: s["required"] for s in result["services"]
    } == {"common": True, "branch": False, "extra": False}
    parent = definitions[("root", 1, "root")]
    parent.access_requirements = [
        requirement("resource"),
        requirement("unused-ceiling"),
    ]
    parent.procedure.steps[0].inputs["record"] = Node(
        binding=Node(source="resource", requirement_id="resource")
    )
    services = connection_preview(ref("root"), definitions)["services"]
    assert any(item["required"] and item["url"] == "resource" for item in services)
    assert all(item["url"] != "unused-ceiling" for item in services)
    parent.procedure.steps[0].inputs.clear()
    # Alternatives use different reviewed capabilities on the same service account.
    definitions[("b", 1, "b")].access_requirements[
        0
    ].capability_ref = "alternate-operation"
    same_service = connection_preview(ref("root"), definitions)["services"]
    assert all(item["required"] for item in same_service if item["url"] == "common")
    del definitions[("b", 1, "b")]
    result = connection_preview(ref("root"), definitions)
    assert not result["complete"] and not any(s["required"] for s in result["services"])


async def test_local_tls_exchange_account_task_and_revocation(
    db, monkeypatch, tmp_path
):
    """Use actual TLS, Host, SNI, form posts and JSON-RPC on a loopback provider."""
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    p = Provider(db, monkeypatch)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "oauth fixture")])
    hosts = ["issuer.example.test", "agent.example.test"]
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(p.now - timedelta(minutes=1))
        .not_valid_after(p.now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(host) for host in hosts]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    (tmp_path / "cert").write_bytes(pem)
    (tmp_path / "key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(tmp_path / "cert", tmp_path / "key")
    sni = []
    tls.set_servername_callback(lambda _, host, ctx: sni.append(host))
    verified = ssl.create_default_context(cadata=pem.decode())
    paths = []

    async def serve(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii").split("\r\n")
            method, path, _ = head[0].split()
            headers = httpx.Headers(line.split(": ", 1) for line in head[1:] if line)
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            req = httpx.Request(
                method,
                "https://192.0.2.1" + path,
                headers=headers,
                content=body,
                extensions={"sni_hostname": sni[-1]},
            )
            result = await p.respond(req)
            paths.append(path)
            writer.write(
                f"HTTP/1.1 {result.status_code} OK\r\nContent-Type: application/json\r\nContent-Length: {len(result.content)}\r\nConnection: close\r\n\r\n".encode()
                + result.content
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=tls)
    port = server.sockets[0].getsockname()[1]

    class LoopbackPort(httpx.AsyncBaseTransport):
        async def __aenter__(self):
            self.inner = httpx.AsyncHTTPTransport(verify=verified)
            return self

        async def handle_async_request(self, request):
            request.url = request.url.copy_with(host="127.0.0.1", port=port)
            return await self.inner.handle_async_request(request)

        async def aclose(self):
            await self.inner.aclose()

    p.transport = adapter.OAuthTransport(transport=LoopbackPort())
    async with server:
        asset = await p.connect()
        p.expire()
        async with db() as session:
            svc = p.service(session)
            result = await svc.oauth.call(
                OWNER,
                asset,
                {"tool": A2A_TASK, "revision": 1, "arguments": {"id": "wire-task"}},
            )
            assert result["structuredContent"] == {
                "id": "wire-task",
                "state": "TASK_STATE_COMPLETED",
            }
            receipt = await svc.action(
                OWNER, asset["id"], AssetAction(revision=1, action="disconnect")
            )
            assert receipt["detail"]["revocation"] == "provider_revoked"
    assert paths == ["/token", "/me", "/token", "/me", "/rpc", "/revoke", "/revoke"]
    assert sni == [hosts[i] for i in (0, 1, 0, 1, 1, 0, 0)]


async def test_provider_revocation_unconfirmed_still_disables_account_and_old_prompt(
    db, monkeypatch
):
    p = Provider(db, monkeypatch)
    _, _, sid = await seed(db)
    asset = await p.connect("mcp")
    key = tool_key(asset["id"], "send")
    async with db() as session:
        svc = p.service(session)
        selected = await svc.selection(OWNER, sid)
        await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(revision=selected["revision"], tools=[key], purpose="Notify"),
        )
        runtime = await svc.prepare(OWNER, sid)
        token = runtime["config"]["mcp_servers.sunmoon_capabilities"]["http_headers"][
            "Authorization"
        ].split()[1]
        prompt = await request_call(svc, sid, token, key, {"to": "recipient"})
        call = await svc.repo.capability_call(OWNER, prompt["call_id"])

        async def unavailable(*args, **kwargs):
            raise CapabilityError("Provider unavailable")

        monkeypatch.setattr(p.transport, "revoke", unavailable)
        removed = await svc.action(
            OWNER, asset["id"], AssetAction(revision=1, action="disconnect")
        )
        assert (
            removed["status"] == "disconnected"
            and "unconfirmed" in removed["detail"]["revocation"]
        )
        p.account = "external-two"
        second = await p.connect("mcp")
        assert second["id"] != asset["id"]
        with pytest.raises(CapabilityError):
            await svc.decide(OWNER, call["id"], approval(call))
        assert not p.remote.calls
        assert (
            await svc.action(
                OWNER, asset["id"], AssetAction(revision=1, action="disconnect")
            )
        ) == removed


@pytest.mark.parametrize("lost_reply", [True, False])
async def test_a2a_dispatch_and_recover_without_duplicate_send(
    db, monkeypatch, lost_reply
):
    from app.domain.workbench.connection_oauth import A2A_TOOLS

    p = Provider(db, monkeypatch)
    p.providers[0] = p.providers[0].model_copy(
        update={"tool_scopes": {n: ("use",) for n in A2A_TOOLS}}
    )
    p.operations = tuple(
        AGENT_OP.model_copy(
            update={
                "tool_name": name,
                "operation": name,
                "effect": {"a2a_submit_task": "send", "a2a_cancel_task": "write"}.get(
                    name, "read"
                ),
                "descriptor_digest": ToolRegistration.discover(
                    "a2a", A2A, descriptor
                ).descriptor_digest,
            }
        )
        for name, descriptor in A2A_TOOLS.items()
    )
    events = []
    task = {
        "id": "remote-id",
        "status": {"state": "TASK_STATE_WORKING"},
        "artifacts": ["private"],
    }

    async def respond(req):
        if req.url.path != "/rpc":
            return await p.respond(req)
        call = json.loads(req.content)
        assert req.headers["A2A-Version"] == "1.0"
        method, params = call["method"], call["params"]
        events.append(method)
        result = task
        if method == "SendMessage":
            message = params["message"]
            assert message["parts"] == [{"text": "Summarize the meeting"}]
            task.update(contextId=message["contextId"], history=[message])
            if lost_reply:
                raise httpx.ReadTimeout("Lost after acceptance")
            result = {"task": task}
        elif method == "ListTasks":
            assert params["contextId"] == task["contextId"]
            result = {"tasks": [task], "totalSize": 1}
        elif method == "CancelTask":
            assert params == {"id": task["id"]}
            task["status"]["state"] = "TASK_STATE_CANCELED"
        else:
            assert method == "GetTask" and params == {
                "id": task["id"],
                "historyLength": 0,
            }
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": call["id"], "result": result}
        )

    p.transport = adapter.OAuthTransport(transport=httpx.MockTransport(respond))
    _, _, sid = await seed(db)
    asset = await p.connect()
    from test_formal_method_admission_db import validation_request

    from app.application.workbench.formal_methods import FormalMethods
    from app.domain.workbench.formal_adapter import (
        host_kind,
        live_registration,
        retained_registration,
    )

    async with db() as session:
        svc = p.service(session)
        methods = FormalMethods(svc.repo, operations=p.operations)
        sources = methods._sources(asset, "author", now=p.now)
        assert {s.entry for s in sources} == set(A2A_TOOLS)
        for source in sources:
            assert source.kind == "a2a" and host_kind(source) == "formal_a2a"
            draft = validation_request(source.model_dump(mode="json")).draft
            assert live_registration(draft, asset) == retained_registration(
                draft, A2A_TOOLS[source.entry]
            )
    keys = {n: tool_key(asset["id"], n) for n in A2A_TOOLS}
    async with db() as s:
        svc = p.service(s)
        current = await svc.selection(OWNER, sid)
        await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=current["revision"],
                tools=list(keys.values()),
                purpose="External meeting task",
            ),
        )
        runtime = await svc.prepare(OWNER, sid)
        token = runtime["config"]["mcp_servers.sunmoon_capabilities"]["http_headers"][
            "Authorization"
        ].split()[1]

    async def approve(name, args):
        async with db() as s:
            svc = p.service(s)
            req = await request_call(svc, sid, token, keys[name], args)
            call = await svc.repo.capability_call(OWNER, req["call_id"])
            done = await svc.decide(OWNER, call["id"], approval(call))
            replay = await svc.decide(OWNER, call["id"], approval(call))
            assert replay["status"] == done["status"]
            return await svc.repo.capability_call(OWNER, call["id"])

    sent = await approve("a2a_submit_task", {"text": "Summarize the meeting"})
    assert sent["status"] == ("unknown" if lost_reply else "succeeded"), sent[
        "detail"
    ].get("result")
    if lost_reply:
        found = await approve("a2a_reconcile_task", {"call_id": sent["id"]})
        assert found["status"] == "succeeded", found["detail"].get("result")
        assert (
            found["detail"]["result"]["structuredContent"]["submission_call_id"]
            == sent["id"]
        )
    read = await approve("a2a_get_task", {"id": "remote-id"})
    assert read["detail"]["result"]["structuredContent"] == {
        "id": "remote-id",
        "state": "TASK_STATE_WORKING",
    }
    canceled = await approve("a2a_cancel_task", {"id": "remote-id"})
    assert (
        canceled["detail"]["result"]["structuredContent"]["state"]
        == "TASK_STATE_CANCELED"
    )
    assert events == [
        "SendMessage",
        *(["ListTasks"] if lost_reply else []),
        "GetTask",
        "CancelTask",
    ]
