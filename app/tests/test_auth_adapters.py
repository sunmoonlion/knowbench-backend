"""The implementations behind browser authentication, and how they are wired."""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

import app.infrastructure.repositories.auth_user as auth_user_module
import app.infrastructure.storage.session_store as session_store_module
from app.application.services.auth_service import AuthService
from app.bootstrap.auth import build_auth_service
from app.infrastructure.repositories.auth_user import SqlUserDirectory
from app.infrastructure.security import OidcProviderClient
from app.infrastructure.storage.postgres import make_session_factory
from app.infrastructure.storage.session_store import RedisSessionStore
from tests.test_auth_service import settings

ROOT = Path(__file__).resolve().parents[1]


class RecordingRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, **kwargs: object) -> bool | None:
        self.calls.append(("set", (key, value), kwargs))
        if key in self.values:
            return None  # what redis-py returns when NX refuses
        self.values[key] = value
        return True

    async def get(self, key: str) -> str | None:
        self.calls.append(("get", (key,), {}))
        return self.values.get(key)

    async def getdel(self, key: str) -> str | None:
        self.calls.append(("getdel", (key,), {}))
        return self.values.pop(key, None)

    async def delete(self, key: str) -> int:
        self.calls.append(("delete", (key,), {}))
        return int(self.values.pop(key, None) is not None)


@pytest.mark.asyncio
async def test_session_store_always_sets_an_expiry_and_never_overwrites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = RecordingRedis()
    monkeypatch.setattr(
        session_store_module, "get_redis", lambda: SimpleNamespace(client=redis)
    )
    store = RedisSessionStore()

    assert await store.create("k", "first", ttl_seconds=30) is True
    assert await store.create("k", "second", ttl_seconds=30) is False
    assert redis.values == {"k": "first"}
    assert [call[2] for call in redis.calls] == [{"ex": 30, "nx": True}] * 2

    assert await store.read("k") == "first"
    assert await store.take("k") == "first"
    assert await store.take("k") is None
    await store.delete("k")
    assert [call[0] for call in redis.calls[2:]] == [
        "get",
        "getdel",
        "getdel",
        "delete",
    ]


def test_wiring_binds_each_surface_and_touches_no_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden():
        raise AssertionError("connections must not be opened while wiring")

    monkeypatch.setattr(session_store_module, "get_redis", forbidden)
    monkeypatch.setattr(auth_user_module, "get_postgres", forbidden)
    config = settings()
    for surface in ("admin", "web"):
        service = build_auth_service(surface, config)  # type: ignore[arg-type]
        assert type(service) is AuthService
        assert service.surface == surface
        assert type(service._oidc) is OidcProviderClient
        assert service._oidc._profile == config.browser_profile(surface)  # type: ignore[arg-type]
        assert type(service._sessions) is RedisSessionStore
        assert type(service._users) is SqlUserDirectory


def migrate(connection) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        for path in sorted((ROOT / "alembic/versions").glob("20*.py")):
            spec = importlib.util.spec_from_file_location(path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.upgrade()


@pytest_asyncio.fixture
async def db():
    url = os.environ.get("DELIVERY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set DELIVERY_TEST_DATABASE_URL to a disposable *_tests database")
    parsed = make_url(url)
    assert parsed.database and parsed.database.endswith("_tests")
    schema = "auth_test_" + uuid.uuid4().hex
    engine = create_async_engine(
        parsed.set(drivername="postgresql+asyncpg"),
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as c:
            await c.execute(text(f'CREATE SCHEMA "{schema}"'))
            await c.execute(
                text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public')
            )
            await c.run_sync(migrate)
        yield make_session_factory(engine)
    finally:
        async with engine.begin() as c:
            await c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


@pytest.mark.asyncio
async def test_user_directory_creates_once_then_refreshes_the_same_user(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        auth_user_module, "get_postgres", lambda: SimpleNamespace(session_factory=db)
    )
    directory = SqlUserDirectory()
    created = await directory.upsert(
        issuer="https://identity.example.test",
        subject="user-123",
        username="first",
        email="first@example.test",
        display_name="First",
        roles=["editor"],
        scopes=["profile:read"],
    )
    assert isinstance(created["id"], uuid.UUID)
    assert set(created) == {"id", "email", "display_name", "roles", "scopes"}
    assert (created["roles"], created["scopes"]) == (["editor"], ["profile:read"])

    refreshed = await directory.upsert(
        issuer="https://identity.example.test",
        subject="user-123",
        username="second",
        email=None,
        display_name="Second",
        roles=[],
        scopes=["profile:read", "profile:write"],
    )
    assert refreshed["id"] == created["id"]
    assert refreshed["email"] is None and refreshed["display_name"] == "Second"
    assert refreshed["roles"] == []
    assert refreshed["scopes"] == ["profile:read", "profile:write"]

    other = await directory.upsert(
        issuer="https://other.example.test",
        subject="user-123",
        username="third",
        email=None,
        display_name=None,
        roles=[],
        scopes=[],
    )
    # 换了 issuer 还是同一个人（账 175：换域名不再切出第二份）
    assert other["id"] == created["id"]
    async with db() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT issuer, username FROM auth_user"
                    " ORDER BY created_at, username"
                )
            )
        ).all()
        assert [tuple(row) for row in rows] == [("https://other.example.test", "third")]


@pytest.mark.asyncio
async def test_login_stores_the_user_through_the_directory(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole service against a real table: claims are filtered before storing."""
    from urllib.parse import parse_qs, urlsplit

    from tests.test_auth_service import FakeOidc, FakeRedis, FakeRedisHolder

    redis = FakeRedis()
    monkeypatch.setattr(
        session_store_module, "get_redis", lambda: FakeRedisHolder(redis)
    )
    monkeypatch.setattr(
        auth_user_module, "get_postgres", lambda: SimpleNamespace(session_factory=db)
    )
    service = AuthService(
        "web",
        settings(),
        FakeOidc(),
        sessions=RedisSessionStore(),
        users=SqlUserDirectory(),
    )
    start = await service.begin_login("/")
    state = parse_qs(urlsplit(start.authorization_url).query)["state"][0]
    created, _ = await service.complete_login(
        code="code", state=state, transaction_id=start.transaction_id
    )
    session = await service.get_browser_session(created.session_id)
    assert session is not None
    principal = session.principal
    assert principal.roles == ("editor",)
    assert principal.scopes == frozenset({"profile:read"})
    async with db() as s:
        row = (
            (await s.execute(text("SELECT id, username, roles, scopes FROM auth_user")))
            .mappings()
            .one()
        )
    assert row["id"] == principal.actor_id
    assert (row["username"], row["roles"], row["scopes"]) == (
        "Test User",
        ["editor"],
        ["profile:read"],
    )
