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
