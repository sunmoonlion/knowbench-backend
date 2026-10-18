"""Actual HTTP + signed workload identity; database cases use disposable schemas."""

from __future__ import annotations

import asyncio
import json
import time
from threading import BoundedSemaphore
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from sqlalchemy import text
from test_delivery_observation import dump, snapshot
from test_durable_delivery_db import db as db
from test_durable_delivery_db import request, runtime, sql

from app.bootstrap.api import create_app
from app.infrastructure.messaging import delivery_observation as observation
from app.infrastructure.security.oidc import OidcProviderClient
from app.infrastructure.security.service_identity import ServiceIdentityVerifier
from app.interfaces.http.internal import delivery_metrics as endpoint
from app.interfaces.http.internal.host_guard import is_internal_host
from app.interfaces.http.middleware import auth
from core.config import Settings

PATH = "/api/internal/v1/delivery/metrics"
ISSUER = "https://identity.example.test"
AUDIENCE = "delivery-test-internal"


@pytest.fixture
def identity(monkeypatch):
    key = RSAKey.generate_key(parameters={"kid": "metrics-test"})
    other_key = RSAKey.generate_key(parameters={"kid": "metrics-test"})
    config = Settings(
        _env_file=None,
        casdoor_endpoint=ISSUER,
        service_auth_audience=AUDIENCE,
        service_auth_subject_bindings_json=json.dumps(
            {"metrics-reader": ["delivery:observe", "profile:read"]}
        ),
    )

    def oidc_response(req):
        if req.url.path.endswith("/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": ISSUER + "/authorize",
                    "token_endpoint": ISSUER + "/token",
                    "jwks_uri": ISSUER + "/jwks",
                },
            )
        assert req.url.path == "/jwks"
        return httpx.Response(200, json={"keys": [key.as_dict(private=False)]})

    oidc = OidcProviderClient(
        config,
        config.browser_profile("admin"),
        transport=httpx.MockTransport(oidc_response),
    )
    monkeypatch.setattr(
        auth,
        "service_identity_verifier",
        ServiceIdentityVerifier(config, oidc_client=oidc),
    )
    monkeypatch.setattr(endpoint, "_collection_slot", BoundedSemaphore(1))

    def headers(*, invalid_signature=False, **overrides):
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "sub": "metrics-reader",
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + 300,
            "scope": "delivery:observe",
            **overrides,
        }
        encoded = jwt.encode(
            {"alg": "RS256", "kid": "metrics-test"},
            claims,
            other_key if invalid_signature else key,
            algorithms=["RS256"],
        )
        return {"Authorization": "Bearer " + encoded}

    return headers


def client(base_url: str = "http://127.0.0.1"):
    # ASGITransport intentionally does not run the shared API lifespan: tests inject
    # only the disposable session factory, never initialize a configured business DB.
    # 内部接口只认集群内与回环地址（审读第 1 条）；测试的 TrustedHost 放回环地址
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url=base_url
    )


async def test_public_hosts_do_not_see_internal_routes(monkeypatch, identity):
    """公网入口把整个 /api 转进来：不是集群内地址的，当没有这个接口，连令牌都不看。"""
    monkeypatch.setattr(endpoint, "get_postgres", lambda: pytest.fail("no pool"))
    async with client("http://testserver") as http:
        response = await http.get(PATH, headers=identity())
    assert response.status_code == 404
    assert "sunmoonai_delivery_" not in response.text


@pytest.mark.parametrize(
    "host",
    [
        "knowbench-tls.app-platform-dev.svc.cluster.local",
        "knowbench-api.app-platform-dev.svc.cluster.local",
        "localhost",
        "127.0.0.1",
        "::1",
    ],
)
def test_cluster_and_loopback_hosts_are_internal(host):
    assert is_internal_host(host)
    assert not is_internal_host("knowbench.sunmoonai.com")
    assert not is_internal_host("svc.cluster.local.evil.example")
    assert not is_internal_host(None)


@pytest.mark.parametrize(
    "case,status",
    [
        ("missing", 401),
        ("cookie", 401),
        ("basic", 401),
        ("malformed", 401),
        ("signature", 401),
        ("issuer", 401),
        ("audience", 401),
        ("expired", 401),
        ("unbound", 403),
        ("scope_missing", 403),
        ("scope_escalated", 403),
    ],
)
async def test_rejected_identity_never_accesses_database(
    monkeypatch,
    identity,
    case,
    status,
):
    accessed = []

    def forbidden_database():
        accessed.append(True)
        raise AssertionError("must authenticate before touching pool")

    monkeypatch.setattr(endpoint, "get_postgres", forbidden_database)
    headers = {
        "missing": {},
        "cookie": {"Cookie": "sunmoonai_tpl_admin_sid=admin-session"},
        "basic": {"Authorization": "Basic not-a-service-token"},
        "malformed": {"Authorization": "Bearer token extra"},
        "signature": identity(invalid_signature=True),
        "issuer": identity(iss="https://wrong.example.test"),
        "audience": identity(aud="browser-client"),
        "expired": identity(exp=int(time.time()) - 3600),
        "unbound": identity(sub="metrics-reader-other"),
        "scope_missing": identity(scope="profile:read"),
        "scope_escalated": identity(scope="delivery:observe admin:write"),
    }[case]
    async with client() as http:
        response = await http.get(PATH, headers=headers)
    assert response.status_code == status
    assert response.headers["Cache-Control"] == "no-store"
    assert not accessed
    assert "sunmoonai_delivery_" not in response.text


async def test_signed_scrape_uses_real_observers_and_is_read_only(
    db, monkeypatch, identity
):
    await request(db, payload={"private": "secret-payload-marker"})
    before = await dump(db)
    postgres = SimpleNamespace(
        session_factory=db,
        init=AsyncMock(),
        shutdown=AsyncMock(),
    )
    monkeypatch.setattr(endpoint, "get_postgres", lambda: postgres)
    expected = observation.render_prometheus(
        await observation.collect_delivery_snapshot(
            db,
            endpoint.get_delivery_observers(db),
        )
    )
    async with client() as http:
        response = await http.get(PATH, headers=identity())
    assert response.status_code == 200
    assert (
        response.headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
    )
    assert response.headers["Cache-Control"] == "no-store"

    # Only the transaction observation timestamp changes between the two reads.
    def without_timestamp(value):
        return [
            line
            for line in value.splitlines()
            if not line.startswith("sunmoonai_delivery_snapshot_timestamp_seconds ")
        ]

    assert without_timestamp(response.text) == without_timestamp(expected)
    assert "sunmoonai_delivery_unregistered_messages 1" in response.text
    assert "secret-payload-marker" not in response.text
    assert await dump(db) == before
    postgres.init.assert_not_awaited()
    postgres.shutdown.assert_not_awaited()


async def test_database_lock_returns_503_then_fresh_scrape_recovers(
    db, monkeypatch, identity
):
    monkeypatch.setattr(
        endpoint, "get_postgres", lambda: SimpleNamespace(session_factory=db)
    )
    async with client() as http:
        with monkeypatch.context() as fault:
            fault.setattr(observation, "OBSERVATION_TIMEOUT_SECONDS", 0.05)
            async with db() as blocker, blocker.begin():
                await blocker.execute(
                    text("LOCK TABLE outbox_message IN ACCESS EXCLUSIVE MODE")
                )
                failed = await http.get(PATH, headers=identity())
        assert failed.status_code == 503
        assert failed.json()["code"] == "delivery_observation_failed"
        assert failed.headers["Cache-Control"] == "no-store"
        assert "sunmoonai_delivery_" not in failed.text
        # Recovery is outside the injected short budget and uses the real 2 seconds.
        assert (await http.get(PATH, headers=identity())).status_code == 200


async def test_malformed_db_value_is_not_partial_metrics_or_leaked_error(
    db,
    monkeypatch,
    identity,
    caplog,
):
    await request(db)
    await sql(
        db,
        "UPDATE outbox_message SET headers="
        '\'{"sunmoonai.not_before.v1":"private-invalid-date"}\'::jsonb',
    )
    monkeypatch.setattr(
        endpoint, "get_postgres", lambda: SimpleNamespace(session_factory=db)
    )
    monkeypatch.setattr(
        endpoint,
        "get_delivery_observers",
        lambda sessions: {"tasks": runtime(sessions)},
    )
    async with client() as http:
        response = await http.get(PATH, headers=identity())
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert "private-invalid-date" not in response.text + caplog.text
    assert "sunmoonai_delivery_" not in response.text


@pytest.mark.parametrize("stage", ["pool", "policies", "collect", "render"])
async def test_failure_at_each_stage_is_redacted_and_releases_slot(
    monkeypatch,
    identity,
    caplog,
    stage,
):
    monkeypatch.setattr(
        endpoint, "get_postgres", lambda: SimpleNamespace(session_factory=None)
    )
    monkeypatch.setattr(endpoint, "get_delivery_observers", lambda sessions: {})
    monkeypatch.setattr(
        endpoint, "collect_delivery_snapshot", AsyncMock(return_value=snapshot())
    )

    def failure(*args):
        raise RuntimeError("secret://private-dsn-password")

    with monkeypatch.context() as fault:
        target = {
            "pool": "get_postgres",
            "policies": "get_delivery_observers",
            "collect": "collect_delivery_snapshot",
            "render": "render_prometheus",
        }[stage]
        fault.setattr(endpoint, target, failure)
        async with client() as http:
            response = await http.get(PATH, headers=identity())
        assert response.status_code == 503
        assert response.json()["code"] == "delivery_observation_failed"
        assert "private-dsn-password" not in response.text + caplog.text
        assert "sunmoonai_delivery_" not in response.text
    async with client() as http:
        assert (await http.get(PATH, headers=identity())).status_code == 200


async def test_overlapping_scrape_is_rejected_without_waiting_or_second_collection(
    monkeypatch,
    identity,
):
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    async def blocking_collect(*args):
        calls.append(True)
        entered.set()
        await finish.wait()
        return snapshot()

    monkeypatch.setattr(
        endpoint, "get_postgres", lambda: SimpleNamespace(session_factory=None)
    )
    monkeypatch.setattr(endpoint, "get_delivery_observers", lambda sessions: {})
    monkeypatch.setattr(endpoint, "collect_delivery_snapshot", blocking_collect)
    async with client() as http:
        first = asyncio.create_task(http.get(PATH, headers=identity()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            second = await asyncio.wait_for(http.get(PATH, headers=identity()), 2)
            assert second.status_code == 503
            assert second.json()["code"] == "delivery_observation_busy"
            assert second.headers["Cache-Control"] == "no-store"
            assert calls == [True]
        finally:
            finish.set()
            response = await first
        assert response.status_code == 200
        assert (await http.get(PATH, headers=identity())).status_code == 200


async def test_cancellation_propagates_and_frees_admission(monkeypatch, identity):
    entered = asyncio.Event()

    async def blocking_collect(*args):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(
        endpoint, "get_postgres", lambda: SimpleNamespace(session_factory=None)
    )
    monkeypatch.setattr(endpoint, "get_delivery_observers", lambda sessions: {})
    with monkeypatch.context() as fault:
        fault.setattr(endpoint, "collect_delivery_snapshot", blocking_collect)
        task = asyncio.create_task(endpoint.delivery_metrics())
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    monkeypatch.setattr(
        endpoint, "collect_delivery_snapshot", AsyncMock(return_value=snapshot())
    )
    async with client() as http:
        assert (await http.get(PATH, headers=identity())).status_code == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_metrics_has_no_mutating_method(method, identity):
    async with client() as http:
        response = await http.request(method, PATH, headers=identity())
    assert response.status_code == 405
    assert response.headers["Cache-Control"] == "no-store"
