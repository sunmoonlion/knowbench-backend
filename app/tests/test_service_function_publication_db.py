"""Internal publication with real identity binding, routes and PostgreSQL."""

import asyncio
import time

from httpx import ASGITransport, AsyncClient
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.errors.exceptions import UnauthorizedError
from app.bootstrap.api import create_app
from app.infrastructure.security import relation_auth
from app.infrastructure.storage.postgres import get_db_session
from core.config import Settings

BASE = "/api/internal/v1/method-skill-center/functions/query_facts"
CONTRACT = {
    "description": "查询选定范围内的财报事实",
    "operation": "query_facts",
    "scenes": ["incubation", "step"],
    "required_permissions": ["facts.read"],
    "input_schema": {"type": "object"},
    "output_schema": {"type": "object"},
}


class Claims:
    async def verify_access_token(self, token, *, audience):
        if audience != "publish" or token not in (
            "knowbase",
            "knowfeed",
            "knowbench",
            "author",
            "wrong-scope",
        ):
            raise UnauthorizedError(code="audience_mismatch")
        now = time.time()
        return {
            "iss": "local-test",
            "sub": "knowbase" if token == "wrong-scope" else token,
            "iat": now,
            "exp": now + 60,
            "scope": "knowbench:incubate"
            if token == "wrong-scope"
            else "knowbench:function-publish",
        }


async def test_publication_retries_identity_and_pinned_revisions(db, monkeypatch):
    app = create_app(Settings())

    async def session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = session
    env = {"SERVICE_AUTH_FUNCTION_PUBLISH_CALLERS": "knowbase,knowfeed"}
    for provider in ("knowbase", "knowfeed"):
        prefix = f"SERVICE_AUTH_FUNCTION_PUBLISH_{provider.upper()}_"
        env.update(
            {
                prefix + "SUBJECT_ALLOWLIST": provider,
                prefix + "AUDIENCE": "publish",
                prefix + "REQUIRED_SCOPE": "knowbench:function-publish",
            }
        )
    verifier = relation_auth.RelationServiceVerifier(
        "function-publish", settings=Settings(), environ=env, oidc_client=Claims()
    )
    monkeypatch.setitem(relation_auth._verifiers, "function-publish", verifier)
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://127.0.0.1"
    ) as http:

        def token(name):
            return {"Authorization": "Bearer " + name}

        def put(body=CONTRACT, provider="knowbase", version=1):
            return http.put(f"{BASE}/{version}", json=body, headers=token(provider))

        assert (await http.put(BASE + "/1", json=CONTRACT)).status_code == 401
        for identity in ("author", "wrong-scope"):
            assert (await put(provider=identity)).status_code == 403
        assert (
            await http.put(
                "http://testserver" + BASE + "/1",
                json=CONTRACT,
                headers=token("knowbase"),
            )
        ).status_code == 404
        for change in (
            {"provider": "knowfeed"},
            {"operation": "https://other/api"},
            {"transport": "mcp"},
            {"input_schema": {"$ref": "https://remote/schema"}},
        ):
            assert (await put(CONTRACT | change)).status_code == 422
        first, retry = await asyncio.gather(put(), put())
        assert first.status_code == retry.status_code == 200
        assert first.json() == retry.json()
        assert (
            await http.get(BASE + "/1", headers=token("knowfeed"))
        ).status_code == 404
        other = await put(provider="knowfeed")
        assert other.status_code == 200 and other.json()["provider"] == "knowfeed"
        assert other.json()["digest"] != first.json()["digest"]
        change = CONTRACT | {"operation": "fetch_facts"}
        assert (await put(change)).status_code == 409
        assert (await put(change, version=2)).status_code == 200
        assert (
            await http.get(BASE + "/1", headers=token("knowbase"))
        ).json() == first.json()
        # A shared audience with an ambiguous subject must fail closed.
        env["SERVICE_AUTH_FUNCTION_PUBLISH_KNOWFEED_SUBJECT_ALLOWLIST"] = (
            "knowbase,knowfeed"
        )
        assert (await put()).status_code == 403


def test_published_contract_matches_route_models():
    import json
    from pathlib import Path

    from app.domain.workbench.service_functions import FunctionRevision, ServiceFunction

    contracts = Path(__file__).resolve().parents[1] / "contracts"
    for model, document in (
        (ServiceFunction, "publication"),
        (FunctionRevision, "revision"),
    ):
        path = contracts / f"service-function-{document}-v1.json"
        assert json.loads(path.read_text()) == model.model_json_schema()
