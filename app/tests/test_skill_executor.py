import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.domain.workbench.capabilities import CapabilityError
from app.infrastructure.workbench.skill_executor import SkillExecutor


def test_script_contract_matches_provider_lock():
    app = Path(__file__).resolve().parents[1]
    consumer = app / "app/domain/workbench/script_contract_generated.py"
    lock = json.loads(consumer.with_suffix(".lock.json").read_text())
    source = consumer.read_bytes().split(b"\n", 1)[1]
    assert hashlib.sha256(source).hexdigest() == lock["sha256"]
    provider = (
        app.parents[2]
        / "sunmoonun/infrastructure/applications/component-images/sandbox-platform/provisioner/script_contract.py"
    )
    if provider.exists():
        assert source == provider.read_bytes()


async def test_executor_authentication_strict_result_and_no_redirects():
    owner, run = str(uuid4()), str(uuid4())
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer private-provider-token"
        assert request.url.path == f"/v1/script-executions/{owner}/{run}"
        return httpx.Response(200, json={"state": "finished"})

    executor = SkillExecutor(
        "http://provisioner.test",
        "private-provider-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ValueError):
        await executor.status(owner, run)
    assert len(calls) == 1


@pytest.mark.parametrize("receipt", [{"stopped": False}, {}, {"accepted": True}])
async def test_stop_requires_confirmed_termination(receipt):
    executor = SkillExecutor(
        "http://provisioner.test",
        "private-provider-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=receipt)
        ),
    )
    with pytest.raises(CapabilityError, match="termination is not confirmed"):
        await executor.cancel(str(uuid4()), str(uuid4()))


async def test_lost_upload_response_recovers_through_status_without_sending_again():
    from app.domain.workbench.script_contract_generated import (
        ScriptExecution,
        execution_frame,
    )

    owner, run = str(uuid4()), str(uuid4())
    calls = []
    payload = {
        "path": "main.py",
        "files": [
            {"path": name, "sha256": hashlib.sha256(b"").hexdigest(), "data_base64": ""}
            for name in ("SKILL.md", "main.py")
        ],
    }
    expected, _ = execution_frame(ScriptExecution.model_validate(payload))

    def handler(request):
        last = request.url.path.rsplit("/", 1)[1]
        calls.append((request.method, last))
        if last == "prepare":
            assert json.loads(request.content) == expected.model_dump()
        if last == "input":
            raise httpx.ReadTimeout("response lost after input")
        return httpx.Response(200, json={"state": "running"})

    executor = SkillExecutor(
        "http://test", "test-only", transport=httpx.MockTransport(handler)
    )
    await executor.prepare(owner, run, payload)
    assert (await executor.start(owner, run, payload))["state"] == "running"
    await executor.status(owner, run)
    assert calls == [("POST", "prepare"), ("PUT", "input"), ("GET", run)]


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_input_connection_failure_is_definite_and_never_retried(failure):
    from app.domain.workbench.capabilities import ScriptInputNotSent

    seen = []

    def refused(request):
        seen.append(request.method)
        raise failure("test connect failure", request=request)

    executor = SkillExecutor(
        "http://test", "test-only", transport=httpx.MockTransport(refused)
    )
    body = {
        "path": "run.py",
        "files": [
            {"path": name, "sha256": hashlib.sha256(b"").hexdigest(), "data_base64": ""}
            for name in ("run.py", "SKILL.md")
        ],
    }
    with pytest.raises(ScriptInputNotSent):
        await executor.start(str(uuid4()), str(uuid4()), body)
    assert seen == ["PUT"]
