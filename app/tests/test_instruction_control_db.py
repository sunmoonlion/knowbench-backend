"""The actual internal callbacks bind the signed gateway to its immutable Attempt."""

import asyncio
import http.client
import importlib
import json
import os
import socket
import ssl
import subprocess
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
from test_formal_instruction_db import PROBES
from test_instruction_requests_db import PRICES, ROUTE, setup
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db
from test_workbench_routes import make_client as make_client

from app.application.workbench.instruction_requests import (
    gateway_binding,
    issue_instruction_ticket,
)
from app.application.workbench.ledger import Ledger
from app.application.workbench.tokens import TokenIssuer, generate_private_key_pem
from app.domain.workbench.errors import Forbidden
from app.domain.workbench.pricing import DEFAULT_PRICES
from app.domain.workbench.states import AttemptState
from app.interfaces.endpoints.knowbot_runtime_routes import formal_methods
from app.interfaces.endpoints.workbench_routes import token_issuer
from app.interfaces.http.internal import instruction_control


def sandbox_relay():
    configured = os.getenv("SANDBOX_INSTRUCTION_RELAY")
    if configured:
        return Path(configured).resolve(strict=True)
    checkout = PROBES.parents[4] / "sunmoonun"
    return (
        checkout
        / "infrastructure/applications/component-images/sandbox-platform/image/instruction_responses.py"
    ).resolve(strict=True)


@asynccontextmanager
async def gateway_app(db, app, monkeypatch, *, route=ROUTE, prices=PRICES, budget="1"):
    host, task, cred, body = await setup(
        db, route=route, prices=prices, budget=budget, credential_url=route.base_url
    )
    pod = uuid4()
    issuer = TokenIssuer(generate_private_key_pem())
    async with host() as svc:
        attempt = await svc.open(OWNER, task, caller="knowlab", gateway_uid=pod)
        binding, _ = await gateway_binding(
            svc.repo, UUID(OWNER), UUID(task), UUID(attempt)
        )

        issued = await issue_instruction_ticket(
            svc.repo,
            issuer,
            binding.owner,
            binding.task,
            binding.attempt,
            gateway_uid=pod,
        )
        assert issuer.instruction_identity(issued.token) == binding
        with pytest.raises(Forbidden):
            await issue_instruction_ticket(
                svc.repo,
                issuer,
                binding.owner,
                binding.task,
                binding.attempt,
                gateway_uid=uuid4(),
            )

    async def formal():
        async with host() as svc:
            yield svc.formal

    app.dependency_overrides[formal_methods] = formal
    app.dependency_overrides[token_issuer] = lambda: issuer
    settings = SimpleNamespace(
        workbench_instruction_routes=(route,),
        workbench_model_prices=lambda: prices,
        workbench_sandbox_provider_base_url=route.base_url,
    )
    monkeypatch.setattr(instruction_control, "get_settings", lambda: settings)
    yield host, binding, issuer, body, settings


async def test_bound_control_claim_and_late_receipt(db, make_client, monkeypatch):
    app = make_client.app
    prefix = "/api/internal/v1/instruction-control/"
    async with (
        gateway_app(db, app, monkeypatch) as (host, identity, issuer, body, settings),
        httpx.AsyncClient(
            base_url="https://localhost", transport=httpx.ASGITransport(app=app)
        ) as http,
    ):
        head = {
            "Authorization": "Bearer " + issuer.instruction_ticket(identity).token,
            "X-Instruction-Gateway": str(identity.gateway),
            "X-Instruction-Attempt": str(identity.attempt),
        }
        claim = {
            "credential_id": str(identity.credential),
            "provider": ROUTE.provider,
            "endpoint": ROUTE.base_url + "/responses",
            "turn_id": "turn-1",
            "body": body,
        }
        for bad in (
            {
                "Authorization": "Bearer "
                + issuer.records_token("u", owner_actor_id=OWNER, sandbox="u").token
            },
            {
                "Authorization": "Bearer "
                + issuer.instruction_ticket(identity, ttl_seconds=-1).token
            },
            {"X-Instruction-Gateway": str(uuid4())},
            {"X-Instruction-Attempt": str(uuid4())},
        ):
            assert (
                await http.post(prefix + "claim", headers=head | bad, json=claim)
            ).status_code == 401
        for field in ("gateway", "sandbox", "credential"):
            other = replace(identity, **{field: uuid4()})
            forged = {
                "Authorization": "Bearer " + issuer.instruction_ticket(other).token,
                "X-Instruction-Gateway": str(other.gateway),
                "X-Instruction-Attempt": str(identity.attempt),
            }
            assert (
                await http.post(prefix + "claim", headers=forged, json=claim)
            ).status_code == 403
        for diff in (
            {"credential_id": str(uuid4())},
            {"endpoint": "https://other.test/v1/responses"},
            {"provider": "other"},
        ):
            assert (
                await http.post(prefix + "claim", headers=head, json=claim | diff)
            ).status_code == 403
        assert (
            await http.post(
                "https://testserver" + prefix + "claim", headers=head, json=claim
            )
        ).status_code == 404
        settings.workbench_instruction_routes = ()
        assert (
            await http.post(prefix + "claim", headers=head, json=claim)
        ).status_code == 403
        settings.workbench_instruction_routes = (ROUTE,)
        response = await http.post(prefix + "claim", headers=head, json=claim)
        assert response.status_code == 200, response.text
        assert response.json()["outgoing"]["max_output_tokens"] == ROUTE.output_tokens
        assert response.headers["Cache-Control"] == "no-store"
        assert (
            await http.post(prefix + "claim", headers=head, json=claim)
        ).status_code == 409
        async with host() as svc:
            await Ledger(svc.repo).request_cancel(
                str(identity.task), owner_actor_id=OWNER
            )
        settings.workbench_instruction_routes = ()
        bill = {
            "turn_id": "turn-1",
            "response_id": "r",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }
        assert (
            await http.post(prefix + "receipt", headers=head, json=bill | {"usage": {}})
        ).status_code == 422
        for _ in range(2):
            assert (
                await http.post(prefix + "receipt", headers=head, json=bill)
            ).json() == {"recorded": True}
        async with host() as svc:
            assert (await svc.repo.get_task(str(identity.task)))["budget"][
                "used"
            ] == "0.000006"


@pytest.mark.parametrize(
    ("cached", "written", "expected"),
    [
        pytest.param(0, 0, "0.005920", id="recorded-kimi-usage"),
        pytest.param(80, 32, "0.004480", id="synthetic-cache-hit-and-write"),
    ],
)
async def test_kimi_receipt_reconciles_with_root_budget(
    db, make_client, monkeypatch, cached, written, expected
):
    # Only the zero-cache sample was observed at the provider. Replay into a
    # fresh local task, without a provider key or a connection to its endpoint.
    route = replace(
        ROUTE,
        provider="kimi",
        model="kimi-k3",
        price_key="kimi-k3",
        base_url="https://api.moonshot.cn/v1",
        context_tokens=1048576,
        output_tokens=64,
    )
    app = make_client.app
    prefix = "/api/internal/v1/instruction-control/"
    async with (
        gateway_app(
            db, app, monkeypatch, route=route, prices=DEFAULT_PRICES, budget="22"
        ) as (host, binding, issuer, body, settings),
        httpx.AsyncClient(
            base_url="https://localhost", transport=httpx.ASGITransport(app=app)
        ) as http,
    ):
        headers = {
            "Authorization": "Bearer " + issuer.instruction_ticket(binding).token,
            "X-Instruction-Gateway": str(binding.gateway),
            "X-Instruction-Attempt": str(binding.attempt),
        }
        response = await http.post(
            prefix + "claim",
            headers=headers,
            json={
                "credential_id": str(binding.credential),
                "provider": route.provider,
                "endpoint": route.base_url + "/responses",
                "turn_id": "replay",
                "body": body,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["outgoing"]["max_output_tokens"] == 64
        task, attempt = str(binding.task), str(binding.attempt)
        async with host() as svc:
            reserved = (await svc.repo.get_task(task))["budget"]["reserved"]
            assert Decimal(reserved) == Decimal("20.977920")
        settings.workbench_model_prices = lambda: (
            PRICES
        )  # settlement uses the original quote
        bill = {
            "turn_id": "replay",
            "response_id": "local-replay",
            "usage": {
                "input_tokens": 176,
                "output_tokens": 24,
                "total_tokens": 200,
                "input_tokens_details": {
                    "cached_tokens": cached,
                    "cache_write_tokens": written,
                },
                "output_tokens_details": {"reasoning_tokens": 18},
            },
        }
        for completed in (False, True):
            response = await http.post(prefix + "receipt", headers=headers, json=bill)
            assert response.status_code == 200, response.text
            if not completed:
                async with host() as svc:
                    await Ledger(svc.repo).attempt_transition(
                        attempt, AttemptState.COMPLETED
                    )
        async with host() as svc:
            budget = (await svc.repo.get_task(task))["budget"]
            assert Decimal(budget["used"]) == Decimal(expected)
            assert Decimal(budget["reserved"]) == 0
            charges = [
                row
                for row in await svc.repo.ledger_list(task)
                if row["entry"] == "consume"
            ]
            assert len(charges) == 1
            assert charges[0]["amount"] == Decimal(expected)
            assert charges[0]["tokens"] == {
                "input": 176 - cached - written,
                "cached_input": cached,
                "cache_write": written,
                "output": 24,
                "total": 200,
            }


@asynccontextmanager
async def https_control(app, directory):
    cert, key = directory / "tls.crt", directory / "tls.key"
    await asyncio.to_thread(
        subprocess.run,
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        config = uvicorn.Config(
            app,
            ssl_certfile=str(cert),
            ssl_keyfile=str(key),
            lifespan="off",
            ws="none",
            log_config=None,
            access_log=False,
        )
        server = uvicorn.Server(config)
        running = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if running.done():
                        await running
                    await asyncio.sleep(0.01)
            yield (
                f"https://127.0.0.1:{sock.getsockname()[1]}",
                ssl.create_default_context(cafile=str(cert)),
            )
        finally:
            server.should_exit = True
            await running


@pytest.mark.skipif(
    os.getenv("RUN_CODEX_INSTRUCTION_PROBE") != "1",
    reason="requires pinned local Codex",
)
async def test_codex_with_external_gateway_and_real_https_control(
    db, make_client, monkeypatch, tmp_path
):
    app = make_client.app
    monkeypatch.syspath_prepend(str(sandbox_relay().parent))
    monkeypatch.syspath_prepend(str(PROBES))
    gateway_type = importlib.import_module("instruction_control").InstructionControl
    fixtures = importlib.import_module("codex_instruction")

    # Use the existing SSE provider fixture, in the trusted host process.
    class AccountProvider(fixtures.InstructionProvider):
        def do_POST(self):
            if self.headers.get("Authorization") != "Bearer synthetic-only":
                self.send_error(401)
                return
            super().do_POST()

    provider = fixtures.TrialServer(("127.0.0.1", 0), AccountProvider)
    provider.first_request = len(fixtures.captures) + 1
    provider.ask = True
    provider.limit_one = False
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()

    def connect(host, port, *, timeout):
        assert (host, port) == ("instruction.example.test", 443)
        return http.client.HTTPConnection(
            "127.0.0.1", provider.server_port, timeout=timeout
        )

    try:
        async with (
            gateway_app(db, app, monkeypatch) as (host, identity, issuer, body, _),
            https_control(app, tmp_path) as (origin, ctx),
        ):
            ticket = tmp_path / "ticket"
            async with host() as svc:
                issued = await issue_instruction_ticket(
                    svc.repo,
                    issuer,
                    identity.owner,
                    identity.task,
                    identity.attempt,
                    gateway_uid=identity.gateway,
                )
            ticket.write_text(issued.token)
            credential = tmp_path / "provider.json"
            credential.write_text(
                json.dumps(
                    {
                        "credential_id": str(identity.credential),
                        "provider": ROUTE.provider,
                        "model": ROUTE.model,
                        "base_url": ROUTE.base_url,
                        "key": "synthetic-only",
                    }
                )
            )
            gateway = gateway_type(
                origin,
                gateway_uid=str(identity.gateway),
                attempt_id=str(identity.attempt),
                receipt_dir=tmp_path,
                ticket_file=ticket,
                credential_file=credential,
                tls_context=ctx,
            )
            job = {
                "input": json.loads(body["input"][0]["content"][0]["text"]),
                "output_schema": body["text"]["format"]["schema"],
            }
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "-i",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--tmpfs",
                "/tmp:exec",
                "-e",
                "HOME=/tmp",
                "-e",
                "CODEX_HOME=/tmp/probe-home",
                "-v",
                f"{PROBES}:/probe:ro",
                "--entrypoint",
                "python3",
                "luna-local/sandbox:codex-0.170.0",
                "/probe/codex_instruction.py",
                "--host-meter",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )

            async def reply(message):
                proc.stdin.write(json.dumps(message).encode() + b"\n")
                await proc.stdin.drain()

            try:
                async with asyncio.timeout(65):
                    await reply(job)
                    decisions = []
                    while True:
                        line = await proc.stdout.readline()
                        assert line, "Codex exited before reporting the metered turn"
                        event = json.loads(line)
                        if isinstance(event, list):
                            (run,) = event
                            break
                        data = bytearray()
                        try:
                            await asyncio.to_thread(
                                gateway.relay,
                                event["forward"],
                                turn_id=event["turn"],
                                start=lambda: None,
                                write=data.extend,
                                connect=connect,
                            )
                        except PermissionError:
                            decisions.append("refused")
                            await reply({"refused": True})
                        else:
                            decisions.append("sent")
                            await reply({"events": data.decode()})
                    assert await proc.wait() == 0
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            assert decisions == ["sent", "refused"]
            assert len(fixtures.captures) == provider.first_request
            assert fixtures.captures[-1]["max_output_tokens"] == ROUTE.output_tokens
            assert run["status"] == "failed" and run["usage"]["totalTokens"] == 2
            async with host() as svc:
                await Ledger(svc.repo).attempt_transition(
                    str(identity.attempt), AttemptState.FAILED
                )
                row = await svc.repo.get_attempt(str(identity.attempt))
                assert (
                    row["turn_ids"] == [run["turn"]]
                    and row["refs"]["instruction_usage"]["tokens"]["total"] == 2
                )
                assert (await svc.repo.get_task(str(identity.task)))["budget"][
                    "used"
                ] == "0.000006"
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join()


def test_instruction_route_configuration_uses_canonical_endpoints(monkeypatch):
    from dataclasses import asdict

    from pydantic import ValidationError

    from core.config import Settings

    entry = asdict(ROUTE)
    monkeypatch.setenv(
        "WORKBENCH_INSTRUCTION_ROUTES",
        json.dumps([entry | {"base_url": ROUTE.base_url + "/"}]),
    )
    assert Settings(_env_file=None).workbench_instruction_routes == (ROUTE,)
    for records in (
        [entry, entry | {"base_url": ROUTE.base_url + "/"}],
        [entry | {"output_tokens": 0}],
        [entry | {"output_tokens": True}],
        [entry | {"unrecognized": 1}],
        [entry | {"base_url": "http://localhost"}],
    ):
        monkeypatch.setenv("WORKBENCH_INSTRUCTION_ROUTES", json.dumps(records))
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


async def test_durable_usage_after_lost_ack_and_gateway_restart(
    db, make_client, monkeypatch, tmp_path
):
    app = make_client.app
    monkeypatch.syspath_prepend(str(sandbox_relay().parent))
    client_type = importlib.import_module("instruction_control").InstructionControl
    async with (
        gateway_app(db, app, monkeypatch) as (host, identity, issuer, body, _),
        https_control(app, tmp_path) as (origin, ctx),
    ):
        ticket = tmp_path / "ticket"
        ticket.write_text(issuer.instruction_ticket(identity).token)

        def restart():
            return client_type(
                origin,
                gateway_uid=str(identity.gateway),
                attempt_id=str(identity.attempt),
                receipt_dir=tmp_path,
                ticket_file=ticket,
                credential_file=tmp_path / "provider-no-longer-mounted",
                tls_context=ctx,
            )

        first = restart()
        assert not first.reconcile()
        untrusted = client_type(
            origin,
            gateway_uid=str(identity.gateway),
            attempt_id=str(identity.attempt),
            receipt_dir=tmp_path,
            ticket_file=ticket,
            credential_file=tmp_path / "unused",
        )
        with pytest.raises(ssl.SSLCertVerificationError):
            await asyncio.to_thread(untrusted._post, "claim", {})

        await asyncio.to_thread(
            first._post,
            "claim",
            {
                "credential_id": str(identity.credential),
                "provider": ROUTE.provider,
                "endpoint": ROUTE.base_url + "/responses",
                "turn_id": "t",
                "body": body,
            },
        )
        async with host() as svc:
            await Ledger(svc.repo).request_cancel(
                str(identity.task), owner_actor_id=OWNER
            )
        payload = {
            "turn_id": "t",
            "response_id": "r",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }
        first.retain_receipt(payload)
        post = first._post

        def lost_ack(op, data):
            assert op == "receipt"
            assert post(op, data) == {"recorded": True}
            raise ConnectionResetError("test peer lost acknowledgment")

        monkeypatch.setattr(first, "_post", lost_ack)
        with pytest.raises(ConnectionResetError):
            await asyncio.to_thread(first.reconcile)
        fresh = restart()
        ticket.write_text(issuer.instruction_ticket(identity).token)
        assert await asyncio.to_thread(fresh.reconcile)
        with pytest.raises(ValueError, match="Conflicting"):
            fresh.retain_receipt(payload | {"response_id": "other"})
        assert json.loads(fresh.proof.read_bytes()) == payload
        async with host() as svc:
            entries = await svc.repo.ledger_list(str(identity.task))
            assert len([r for r in entries if r["entry"] == "consume"]) == 1
            budget = (await svc.repo.get_task(str(identity.task)))["budget"]
            assert budget["used"] == "0.000006" and Decimal(budget["reserved"]) == 0
