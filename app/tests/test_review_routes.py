"""Route preview is catalog evidence only, including on a one-provider installation."""

import json

import httpx
import pytest
from pydantic import ValidationError

from app.bootstrap.api import create_app
from app.domain.workbench.review_routes import (
    ReviewRouteQuery,
    RouteKey,
    preview_routes,
)
from app.infrastructure.security.relation_auth import ServiceCaller
from app.interfaces.endpoints import knowbot_runtime_routes as routes
from core.config import Settings

PRIMARY = {"provider": "one", "model": "kimi-k3"}
STRICT = "different_model_family"
ISOLATED = "isolated_reviewer"


def entry(provider="one", model="kimi-k3", family="first", **extra):
    return dict(provider=provider, model=model, family=family, **extra)


@pytest.mark.parametrize(
    ("catalog", "policy", "reviewer", "expected", "gap"),
    [
        ([], ISOLATED, None, [PRIMARY], None),
        (
            [entry(), entry(provider="two", family="second", price_key="kimi-k3")],
            STRICT,
            None,
            [{"provider": "two", "model": "kimi-k3"}],
            None,
        ),
        ([], STRICT, None, [], "family_unverified"),
        ([entry()], STRICT, None, [], "different_family_unavailable"),
        (
            [entry(), entry(model="alias")],
            STRICT,
            None,
            [],
            "different_family_unavailable",
        ),
        (
            [entry(), entry(model="other", family="second", price_key="kimi-k3")],
            STRICT,
            None,
            [{"provider": "one", "model": "other"}],
            None,
        ),
        (
            [entry(), entry(provider="two")],
            STRICT,
            None,
            [],
            "different_family_unavailable",
        ),
        (
            [entry(), entry(provider="two", family="second")],
            STRICT,
            None,
            [],
            "price_missing",
        ),
        (
            [entry(), entry(model="other", family="second")],
            STRICT,
            None,
            [],
            "price_missing",
        ),
        (
            [entry()],
            ISOLATED,
            {"provider": "missing", "model": "unused"},
            [],
            "reviewer_unknown",
        ),
    ],
)
def test_route_matrix(catalog, policy, reviewer, expected, gap, monkeypatch):
    monkeypatch.setenv("WORKBENCH_REVIEW_ROUTES", json.dumps(catalog))
    settings = Settings(_env_file=None)
    result = preview_routes(
        ReviewRouteQuery(requirement=policy, reviewer=reviewer),
        settings.workbench_review_routes,
        RouteKey(**PRIMARY),
        settings.workbench_model_prices(),
    )
    assert [
        r.model_dump(include={"provider", "model"}) for r in result.candidates
    ] == expected
    assert result.catalog_gap == gap and not result.isolated_execution_available


def test_configuration_rejects_duplicate_route_metadata():
    with pytest.raises(ValidationError, match="Duplicate review"):
        Settings(WORKBENCH_REVIEW_ROUTES=[entry(), entry(family="other")])


async def test_internal_route_preview_has_no_dispatch_authority(monkeypatch):
    settings = Settings(
        WORKBENCH_SANDBOX_MODEL_PROVIDER="one", WORKBENCH_SANDBOX_MODEL="kimi-k3"
    )
    app = create_app(settings)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        url = "/api/internal/v1/knowbot-runtime/formal-review-route-preview"
        app.dependency_overrides[routes.require_testrun_service] = lambda: (
            ServiceCaller(caller="other", principal=None)
        )
        assert (
            await client.post(url, json={"requirement": ISOLATED})
        ).status_code == 403
        app.dependency_overrides[routes.require_testrun_service] = lambda: (
            ServiceCaller(caller="knowlab", principal=None)
        )
        resp = await client.post(url, json={"requirement": ISOLATED})
        assert resp.status_code == 200 and resp.json()["candidates"] == [
            {**PRIMARY, "family": None, "price_key": "kimi-k3"}
        ]
        assert resp.json()["isolated_execution_available"] is False
        assert (
            await client.post(url, json={"requirement": STRICT, "family": "asserted"})
        ).status_code == 422
