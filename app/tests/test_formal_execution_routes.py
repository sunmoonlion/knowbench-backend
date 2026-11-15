"""Real HTTP/service identity and persisted-owner lookup; no runtime execution.

Dispatch spies cover only routing. Status uses the actual formal service against
an in-memory pending record; it never claims a completed execution or permission.
"""

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from test_formal_method_routes import (
    ASSET,
    BASE,
    HEADERS,
    OWNER,
    RUN,
    MemoryRepository,
    validation_body,
)
from test_formal_method_routes import app_and_repo as app_and_repo  # noqa: F401

from app.application.workbench.formal_methods import FormalMethods
from app.application.workbench.formal_test_runs import FormalExecutions
from app.application.workbench.test_runs import KnowbotTestService
from app.domain.workbench.formal_execution import case_key
from app.domain.workbench.formal_method_execution_generated import (
    EXECUTION_CONTRACT,
    FormalMethodExecutionRequest,
    FormalTestSuiteDraft,
)
from app.interfaces.endpoints import knowbot_runtime_routes

URL = BASE + "/formal-method-executions"
CALL = "6b1de2f0-ce80-4509-8818-6bff3d76ca2b"


def execution_body():
    body = validation_body()
    body["contract"] = EXECUTION_CONTRACT
    body["draft"]["source"]["kind"] = "mcp"
    procedure = body["draft"]["definition"]["procedure"]
    procedure["implementation"]["kind"] = "mcp"
    procedure["max_model_turns"] = 0
    suite = FormalTestSuiteDraft.model_validate(
        {
            "suite_id": ASSET,
            "version": 1,
            "cases": [
                {
                    "case_id": "positive",
                    "input": {"values": {}},
                    "repetitions": 1,
                    "minimum_passes": 1,
                    "expected": {
                        "outcome": "succeeded",
                        "assertions": [
                            {"kind": "equals", "pointer": "/summary", "value": "ok"}
                        ],
                    },
                }
            ],
        }
    ).freeze()
    body["execution"] = {
        "suite": suite.model_dump(mode="json"),
        "runner": {"kind": "user", "environment_id": ASSET, "workspace_root": "/safe"},
        "budget": {"limit": "1.00", "currency": "CNY"},
        "timeout_seconds": 300,
        "max_wait_seconds": 600,
    }
    return FormalMethodExecutionRequest.model_validate(body).model_dump(
        mode="json", by_alias=True
    )


def decision_body():
    return {
        "owner_subject": "author",
        "case_id": "positive",
        "repetition": 1,
        "token": "exact-approval-token",
        "subject_digest": "exact-call-digest",
        "decision": "accept",
    }


class ExecutionRepository(MemoryRepository):
    def __init__(self):
        super().__init__()
        self.reads = 0
        request = FormalMethodExecutionRequest.model_validate(execution_body())
        self.run = {
            "run_id": RUN,
            "owner_subject": "author",
            "caller": "knowlab",
            "status": "accepted",
            "digest": request.fingerprint().removeprefix("sha256:"),
            "request": request.model_dump(mode="json", by_alias=True),
        }
        self.validations[RUN] = {
            "owner_actor_id": OWNER,
            "caller": "knowlab",
            "result": {
                "contract": "knowlab/formal-method-validation/v1",
                "run_id": RUN,
                "status": "pending",
                "receipt": None,
                "missing_checks": ["outcome"],
                "details": [],
            },
        }

    async def lock_formal_execution(self, run_id):
        pass

    async def get_test_run(self, run_id, *, for_update=False):
        self.reads += 1
        return self.run if run_id == RUN else None

    async def list_test_cases(self, run_id):
        return [{"case_id": case_key("positive", 1), "result": None}]

    async def test_run_spent(self, run_id):
        return Decimal("0")


@pytest.fixture
def execution_app(app_and_repo):
    app, _ = app_and_repo
    repo = ExecutionRepository()
    svc = FormalExecutions(repo, SimpleNamespace(formal_sources=FormalMethods(repo)))
    app.dependency_overrides[knowbot_runtime_routes.formal_executions] = lambda: svc
    return app, repo, svc


@pytest.mark.parametrize(
    ("token", "status", "code"),
    [
        (None, 401, "auth_required"),
        ("other-service", 403, "formal_method_caller_denied"),
        ("wrong-scope", 403, "service_scope_denied"),
        ("browser-subject", 403, "service_subject_unbound"),
        ("unknown", 401, "audience_mismatch"),
    ],
)
async def test_execution_routes_authenticate_before_loading(
    execution_app, token, status, code
):
    app, repo, _ = execution_app
    headers = {"Authorization": "Bearer " + token} if token else {}
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://127.0.0.1", headers=headers
    ) as client:
        responses = [
            await client.post(URL, json=execution_body()),
            await client.get(f"{URL}/{RUN}?owner_subject=author"),
            await client.post(f"{URL}/{RUN}/cancel?owner_subject=author"),
            await client.post(f"{URL}/{RUN}/actions/{CALL}", json=decision_body()),
        ]
    assert all(r.status_code == status and r.json()["code"] == code for r in responses)
    assert repo.reads == repo.accesses == 0


async def test_formal_status_is_owner_caller_scoped_and_no_store(execution_app):
    app, repo, _ = execution_app
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://127.0.0.1", headers=HEADERS
    ) as client:
        own = await client.get(f"{URL}/{RUN}?owner_subject=author")
        assert own.status_code == 200, own.text
        assert own.headers["cache-control"] == "no-store"
        assert own.json()["contract"] == EXECUTION_CONTRACT
        assert own.json()["status"] == "pending"
        assert own.json()["admission"]["receipt"] is None
        assert "exact-approval-token" not in own.text
        for subject in ("other", "unknown"):
            assert (
                await client.get(f"{URL}/{RUN}?owner_subject={subject}")
            ).status_code == 404
            assert (
                await client.post(f"{URL}/{RUN}/cancel?owner_subject={subject}")
            ).status_code == 404
            assert (
                await client.post(
                    f"{URL}/{RUN}/actions/{CALL}",
                    json=decision_body() | {"owner_subject": subject},
                )
            ).status_code == 404
        repo.run["caller"] = "knowmart"
        assert (
            await client.get(f"{URL}/{RUN}?owner_subject=author")
        ).status_code == 404
        repo.run["caller"] = "knowlab"
        repo.run["request"]["contract"] = "knowbot-test-run/v1"
        assert (
            await client.get(f"{URL}/{RUN}?owner_subject=author")
        ).status_code == 404


async def test_formal_routes_dispatch_exact_typed_identity(execution_app, monkeypatch):
    app, _, svc = execution_app
    response = await svc.status("author", RUN, caller="knowlab")
    accept, cancel, decide = (
        AsyncMock(return_value=deepcopy(response)) for _ in range(3)
    )
    monkeypatch.setattr(svc, "accept", accept)
    monkeypatch.setattr(svc, "cancel", cancel)
    monkeypatch.setattr(svc, "decide", decide)
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://127.0.0.1", headers=HEADERS
    ) as client:
        created = await client.post(URL, json=execution_body())
        cancelled = await client.post(f"{URL}/{RUN}/cancel?owner_subject=author")
        answered = await client.post(
            f"{URL}/{RUN}/actions/{CALL}", json=decision_body()
        )
    assert [created.status_code, cancelled.status_code, answered.status_code] == [
        202,
        200,
        200,
    ]
    assert all(
        r.headers["cache-control"] == "no-store" for r in (created, cancelled, answered)
    )
    request = accept.call_args.args[0]
    assert request.run_id == UUID(RUN)
    assert request.execution.suite.cases[0].execution_input() == {"values": {}}
    assert accept.call_args.kwargs == {"caller": "knowlab"}
    cancel.assert_awaited_once_with("author", RUN, caller="knowlab")
    assert decide.call_args.args[:3] == ("author", RUN, CALL)
    assert decide.call_args.args[3].model_dump() == decision_body()
    assert decide.call_args.kwargs == {"caller": "knowlab"}


@pytest.mark.parametrize("extra", ["passed", "receipt", "owner_actor_id", "runtime"])
async def test_execution_intake_rejects_client_runtime_claims(execution_app, extra):
    app, repo, _ = execution_app
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://127.0.0.1", headers=HEADERS
    ) as client:
        result = await client.post(URL, json=execution_body() | {extra: True})
    assert result.status_code == 422
    assert repo.reads == repo.accesses == 0


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", f"/{RUN}", None),
        ("GET", "/not-a-uuid?owner_subject=author", None),
        ("POST", f"/{RUN}/cancel", None),
        ("POST", f"/{RUN}/actions/not-a-uuid", decision_body()),
        ("POST", f"/{RUN}/actions/{CALL}", decision_body() | {"passed": True}),
        ("POST", f"/{RUN}/actions/{CALL}", decision_body() | {"repetition": 0}),
    ],
)
async def test_formal_routes_reject_invalid_identity_or_decision(
    execution_app, method, path, body
):
    app, repo, _ = execution_app
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://127.0.0.1", headers=HEADERS
    ) as client:
        response = await client.request(method, URL + path, json=body)
    assert response.status_code == 422
    assert repo.reads == repo.accesses == 0


async def test_public_host_hides_execution_routes(execution_app):
    app, repo, _ = execution_app
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://testserver", headers=HEADERS
    ) as client:
        response = await client.get(f"{URL}/{RUN}?owner_subject=author")
    assert response.status_code == 404
    assert repo.reads == repo.accesses == 0


@pytest.mark.parametrize("state", ["accepted", "passed", "cancelled"])
@pytest.mark.parametrize("token", ["author-service", "other-service"])
async def test_legacy_routes_never_project_or_mutate_formal_runs(
    execution_app, state, token
):
    app, repo, _ = execution_app
    repo.run["status"] = state
    app.dependency_overrides[knowbot_runtime_routes.service] = lambda: (
        KnowbotTestService(repo)
    )
    legacy = f"{BASE}/test-runs/{RUN}"
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + token},
    ) as client:
        responses = [
            await client.get(legacy),
            await client.post(legacy + "/cancel"),
            await client.get(legacy + "/cases/positive/outputs"),
            await client.get(legacy + "/execution?owner_subject=author"),
            await client.post(
                legacy + "/answers",
                json={
                    "contract": "knowbot-test-run/v1",
                    "answers": [
                        {
                            "question_id": "fixture",
                            "decision": "approve",
                            "answered_by": "user",
                        }
                    ],
                },
            ),
        ]
    assert [r.status_code for r in responses] == [404] * len(responses)
    assert repo.run["status"] == state
