"""Runner -> isolated worker websocket -> gateway -> KnowBench HTTPS/PG and synthetic provider."""

import asyncio
import http.client
import importlib
import json
import os
import subprocess
import threading
from decimal import Decimal
from functools import partial
from http.server import HTTPServer
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_formal_instruction_db import PROBES
from test_formal_instruction_trial_db import AuthorTrial
from test_instruction_control_db import https_control, sandbox_relay
from test_instruction_requests_db import PRICES, ROUTE
from test_instruction_runner_db import drive, stack
from test_workbench_ledger_db import db as db
from test_workbench_routes import make_client as make_client

from app.infrastructure.workbench.app_server_client import AppServerClient
from app.interfaces.endpoints.knowbot_runtime_routes import formal_methods
from app.interfaces.endpoints.workbench_routes import token_issuer
from app.interfaces.http.internal import instruction_control


@pytest.mark.skipif(
    os.getenv("RUN_CODEX_INSTRUCTION_PROBE") != "1",
    reason="requires pinned local Codex and Docker",
)
@pytest.mark.parametrize(
    "late_receipt,registered,review",
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ],
    ids=[
        "runner-restart",
        "receipt-delay",
        "registered-runner-restart",
        "branch-review-restart",
    ],
)
async def test_real_instruction_runner(
    db, make_client, monkeypatch, tmp_path, late_receipt, registered, review
):
    trial = AuthorTrial()
    if review:
        from branch_review_support import review_case

        s = await stack(db, setup_case=partial(review_case, monkeypatch=monkeypatch))
    else:
        s = await stack(db, author_trial=trial.start, registered=registered)
    image_dir = sandbox_relay().parent
    monkeypatch.syspath_prepend(str(image_dir))
    monkeypatch.syspath_prepend(str(PROBES))
    gateway = importlib.import_module("instruction_gateway")
    monkeypatch.setattr(gateway, "BINDING", tmp_path)
    fixtures = importlib.import_module("codex_instruction")
    app = make_client.app

    async def formal():
        async with s.host() as svc:
            yield svc.formal

    if review:
        from app.infrastructure.storage.postgres import get_db_session

        async def sessions():
            async with db() as session:
                yield session

        app.dependency_overrides[get_db_session] = sessions
    else:
        app.dependency_overrides[formal_methods] = formal
    app.dependency_overrides[token_issuer] = lambda: s.driver.issuer
    monkeypatch.setattr(
        instruction_control,
        "get_settings",
        lambda: SimpleNamespace(
            workbench_instruction_routes=(ROUTE,),
            workbench_model_prices=lambda: PRICES,
            workbench_sandbox_provider_base_url=ROUTE.base_url,
        ),
    )
    s.driver.connector = AppServerClient.instruction_worker
    account = {
        "provider": ROUTE.provider,
        "model": ROUTE.model,
        "credential_id": str(s.cred["id"]),
        "base_url": ROUTE.base_url,
        "key": "synthetic-only",
    }
    (tmp_path / "provider").write_text(json.dumps(account))
    (tmp_path / "token").write_text("ephemeral-runner-capability-for-tests")

    async def docker(*args):
        proc = await asyncio.to_thread(
            subprocess.run,
            ["docker", *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()

    name = "instruction-runner-" + uuid4().hex[:12]
    await docker("network", "create", "--internal", name)
    try:
        net = json.loads(await docker("network", "inspect", name))[0]
        host_ip = net["IPAM"]["Config"][0]["Gateway"]
        await docker(
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--network",
            name,
            "--read-only",
            "--tmpfs",
            "/tmp:exec",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pull=never",
            "--entrypoint",
            "python3",
            "-e",
            "CODEX_HOME=/tmp/codex",
            "-e",
            "MODEL=" + ROUTE.model,
            "-e",
            "GATEWAY_IP=" + host_ip,
            "-v",
            f"{image_dir / 'instruction_worker.py'}:/worker.py:ro",
            "-v",
            f"{tmp_path / 'token'}:/run/worker/token:ro",
            os.getenv(
                "CODEX_INSTRUCTION_TEST_IMAGE", "luna-local/sandbox:codex-0.170.0"
            ),
            "/worker.py",
        )
        worker_ip = json.loads(await docker("inspect", name))[0]["NetworkSettings"][
            "Networks"
        ][name]["IPAddress"]
        async with asyncio.timeout(15):
            while True:
                try:
                    _, w = await asyncio.open_connection(worker_ip, 47800)
                    w.close()
                    await w.wait_closed()
                    break
                except ConnectionError:
                    await asyncio.sleep(0.1)
        async with https_control(app, tmp_path) as (origin, tls):
            with (
                HTTPServer((host_ip, 47801), gateway.Gateway) as relay,
                fixtures.TrialServer(
                    ("127.0.0.1", 0), fixtures.InstructionProvider
                ) as provider,
            ):
                provider.ask = False
                provider.limit_one = False
                provider.output = {"result" if review else "summary": "ready"}
                provider.first_request = len(fixtures.captures) + 1
                initial = len(fixtures.captures)
                threads = [
                    threading.Thread(target=v.serve_forever, daemon=True)
                    for v in (relay, provider)
                ]
                for t in threads:
                    t.start()
                try:
                    request = s.units.request

                    async def unit(owner, task, method, path, **kwargs):
                        result = await request(owner, task, method, path, **kwargs)
                        if path.endswith("/binding"):
                            body = kwargs["body"]
                            (tmp_path / "ticket").write_text(body["ticket"])
                            (tmp_path / "turn").write_text(body["turn"] or "")
                            if not hasattr(relay, "control"):
                                control = gateway.InstructionControl(
                                    origin,
                                    gateway_uid=s.units.uid,
                                    attempt_id=body["attempt"],
                                    ticket_file=tmp_path / "ticket",
                                    credential_file=tmp_path / "provider",
                                    receipt_dir=tmp_path,
                                    tls_context=tls,
                                )

                                if late_receipt:
                                    normal_post = control._post

                                    def delay_receipt(operation, payload):
                                        if operation == "receipt":
                                            raise ConnectionResetError(
                                                "test receipt link unavailable"
                                            )
                                        return normal_post(operation, payload)

                                    monkeypatch.setattr(control, "_post", delay_receipt)
                                relay.gateway_control = control

                                def local_provider(host, port, *, timeout):
                                    assert (
                                        host == "instruction.example.test"
                                        and port == 443
                                    )
                                    return http.client.HTTPConnection(
                                        "127.0.0.1",
                                        provider.server_port,
                                        timeout=timeout,
                                    )

                                relay.control = SimpleNamespace(
                                    relay=partial(control.relay, connect=local_provider)
                                )
                        if path.endswith("/worker"):
                            return {
                                **result,
                                "ip": worker_ip,
                                "app_server_token": (tmp_path / "token").read_text(),
                            }
                        return result

                    s.units.request = unit
                    s.units.ready = True
                    # Lose the runner after Codex has finished but just before the
                    # final ledger transaction. The next drive has only persisted IDs.
                    commit_result = s.driver.finish

                    async def lose_runner(*args, **kwargs):
                        if kwargs.get("outputs") is not None:
                            raise asyncio.CancelledError
                        await commit_result(*args, **kwargs)

                    if late_receipt:
                        await asyncio.wait_for(drive(s), 45)
                        async with s.host() as svc:
                            pending_task = await svc.repo.get_task(s.task)
                            (pending,) = await svc.repo.list_attempts(s.task)
                            assert pending_task["state"] == "RUNNING"
                            assert pending["status"] == "RUNNING"
                            assert "instruction_usage" not in pending["refs"]
                            assert "formal_result" not in pending["refs"]
                            assert Decimal(pending_task["budget"]["reserved"]) > 0
                        assert relay.gateway_control.proof.exists()
                        fresh_control = gateway.InstructionControl(
                            origin,
                            gateway_uid=s.units.uid,
                            attempt_id=str(pending["id"]),
                            ticket_file=tmp_path / "ticket",
                            credential_file=tmp_path / "provider",
                            receipt_dir=tmp_path,
                            tls_context=tls,
                        )
                        assert await asyncio.to_thread(fresh_control.reconcile)
                    else:
                        s.driver.finish = lose_runner
                        with pytest.raises(asyncio.CancelledError):
                            await asyncio.wait_for(drive(s), 45)
                        s.driver.finish = commit_result
                    for _ in range(5):
                        await s.runner.run_once()
                        if active := s.runner.driving.get(s.task):
                            await active
                        if s.runner.instruction_cleanup:
                            await s.runner.instruction_cleanup
                        async with s.host() as svc:
                            if (await svc.repo.get_task(s.task))["state"] != "RUNNING":
                                break
                        await asyncio.sleep(0.2)
                    async with s.host() as svc:
                        task = await svc.repo.get_task(s.task)
                        (attempt,) = await svc.repo.list_attempts(s.task)
                    assert task["state"] == "SUCCEEDED", attempt["refs"].get(
                        "formal_result"
                    )
                    assert Decimal(task["budget"]["reserved"]) == 0
                    assert Decimal(task["budget"]["used"]) == Decimal("0.000006")
                    assert len(fixtures.captures) == initial + 1
                    assert (
                        attempt["refs"]["formal_result"]["outputs"] == provider.output
                    )
                    assert (
                        attempt["refs"]["instruction_usage"]["turn_id"]
                        == (tmp_path / "turn").read_text()
                    )
                    assert (
                        json.loads(
                            (tmp_path / (str(attempt["id"]) + ".json")).read_bytes()
                        )["response_id"]
                        == attempt["refs"]["instruction_usage"]["response_id"]
                    )
                    if review:
                        assert task["pack_snapshot"]["review"]["revision"] == 1
                        assert attempt["refs"]["instruction_thread"]["thread"]
                        from app.domain.workbench.instruction_requests import (
                            leaf_responses_body,
                        )
                        from app.domain.workbench.source_tasks import instruction_input

                        expected = instruction_input(
                            "Check the approved output independently.",
                            {"receipt": "test@example.test"},
                        )
                        assert fixtures.captures[-1] == leaf_responses_body(
                            expected, ROUTE.model, ROUTE.output_tokens
                        )
                    else:
                        await trial.finish(db)
                finally:
                    for v in (relay, provider):
                        await asyncio.to_thread(v.shutdown)
                    for t in threads:
                        t.join()
    finally:
        await asyncio.to_thread(
            subprocess.run, ["docker", "rm", "--force", name], capture_output=True
        )
        await docker("network", "rm", name)
