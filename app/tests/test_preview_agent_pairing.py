"""Record install-command, pairing lookup and approve the same way as the download fixture.

PREVIEW_AGENT_DOWNLOAD_OUT=<web>/app/preview/fixtures pytest tests/test_preview_agent_pairing.py
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from test_workbench_routes import A, principal

from app.application.workbench import agent_onboarding as onboarding
from app.application.workbench.agent_onboarding import code_digest
from app.bootstrap.api import create_app
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.endpoints import agent_onboarding_routes as routes
from app.interfaces.endpoints.workbench_routes import (
    credential_cipher,
    provisioning_backends,
    require_workbench_enabled,
    token_issuer,
)
from app.interfaces.http.middleware.auth import get_web_current_user
from core.config import AgentDownload, AgentReleaseStorage, Settings

PAIRING_ID = "11111111-1111-4111-8111-111111111111"
CODE = "K3NP-Q7R2"
HMAC = "k" * 32
FIXED = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)
CREDENTIAL = "preview-install-credential-not-a-live-secret-0001"
TOKEN = "preview-agent-token-must-not-be-recorded"


class Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED


class Store:
    def __init__(self, *, replaces: str | None):
        self.replaces = replaces
        self.row = {
            "id": PAIRING_ID,
            "status": "pending",
            "expires_at": FIXED + timedelta(minutes=5),
            "code_hash": code_digest(CODE, HMAC.encode()),
            "machine_name": "家里的电脑",
            "os": "windows",
            "agent_version": "0.2.4",
            "codex_version": "0.155.1",
            "source_ip": "203.0.113.8",
            "created_at": FIXED - timedelta(seconds=12),
        }

    @asynccontextmanager
    async def transaction(self):
        yield

    @asynccontextmanager
    async def relay_identity_operation(self, owner):
        yield

    async def get_pending_agent_pairing_by_code(self, digest):
        return self.row if digest == self.row["code_hash"] else None

    async def current_online_environment(self, owner):
        return {"name": self.replaces} if self.replaces else None

    async def get_agent_pairing(self, pairing_id, *, for_update=False):
        return self.row if pairing_id == PAIRING_ID else None

    async def get_relay_identity(self, owner):
        return {"relay_user": "u-fixture"}

    async def update_agent_pairing(self, pairing_id, *, expected_status, **fields):
        self.row.update(fields)
        return True

    async def append_agent_audit_event(self, **fields):
        return None

    async def create_agent_install_credential(self, **fields):
        return None


class Cipher:
    def encrypt(self, value: bytes) -> bytes:
        return b"enc:" + value


class Provision:
    def __init__(self, store: Store):
        self.repo = store

    relay_public_url = "wss://relay.example"

    def identity_metadata(self, identity):
        return {
            "identity_revision": "a" * 64,
            "agent_token_expires_at": "2026-11-08T00:00:00+00:00",
        }

    async def rotate_relay_identity_in_operation(self, owner, revision):
        return {
            "relay": {
                "agent_token": TOKEN,
                "agent_token_expires_at": "2026-11-08T00:00:00+00:00",
                "user": "u-fixture",
            }
        }


def _settings() -> Settings:
    base = "https://downloads.example"
    return Settings(
        web_frontend_base_url=base,
        WORKBENCH_ENABLED=True,
        WORKBENCH_AGENT_PAIRING_HMAC_KEY=HMAC,
        WORKBENCH_AGENT_INSTALL_SCRIPT_TEMPLATE="unused-template",
        WORKBENCH_AGENT_DOWNLOAD=AgentDownload(
            mode="object-storage",
            object_key="windows-x64/0.2.4/windows-x64.zip",
            url=base + "/api/workbench/agent/package",
            version="0.2.4",
            codex_version="0.155.1",
            zip_sha256="a" * 64,
            manifest_sha256="b" * 64,
            size_bytes=174243923,
        ),
        WORKBENCH_AGENT_RELEASE_STORAGE=AgentReleaseStorage(
            endpoint="https://storage.example",
            ca_file="preview-ca.pem",
            access_key=SecretStr("preview-access"),
            secret_key=SecretStr("preview-secret"),
        ),
    )


def _write(scenario: str, name: str, method: str, path: str, body: dict) -> None:
    root = os.environ.get("PREVIEW_AGENT_DOWNLOAD_OUT")
    if not root:
        return
    directory = Path(root) / scenario
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["responses"] = [
        item for item in manifest["responses"] if item["path"] != path
    ]
    manifest["responses"].append(
        {
            "method": method,
            "path": path,
            "query": "",
            "status": 200,
            "content_type": "application/json",
            "file": name,
        }
    )
    page = "/zh-CN/workbench/settings#computer"
    if not any(item.get("path") == page for item in manifest["pages"]):
        manifest["pages"].append({"title": "我的电脑", "path": page})
    (directory / name).write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")


@pytest.mark.parametrize(
    ("scenario", "replaces"),
    [("empty", None), ("full", "办公室的电脑"), ("offline", None)],
)
async def test_record_pairing_samples(monkeypatch, scenario, replaces):
    settings = _settings()
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.interfaces.endpoints.workbench_routes.get_settings", lambda: settings
    )
    monkeypatch.setattr(routes, "datetime", Frozen)
    monkeypatch.setattr(onboarding, "datetime", Frozen)
    monkeypatch.setattr(routes, "new_install_credential", lambda: CREDENTIAL)
    store = Store(replaces=replaces)

    async def allow(*_args, **_kwargs):
        return True

    monkeypatch.setattr(routes, "allow_install_issue", allow)
    monkeypatch.setattr(routes, "allow_user_lookup", allow)
    monkeypatch.setattr(routes, "workbench_store", lambda _session: store)
    monkeypatch.setattr(routes, "_provisioning", lambda *args: Provision(store))

    class Redis:
        client = object()

    monkeypatch.setattr(routes, "get_redis", lambda: Redis())
    app = create_app(settings)
    app.dependency_overrides[require_workbench_enabled] = lambda: None
    app.dependency_overrides[get_web_current_user] = lambda: principal(A)
    app.dependency_overrides[get_db_session] = lambda: None
    app.dependency_overrides[credential_cipher] = lambda: Cipher()
    app.dependency_overrides[provisioning_backends] = lambda: None
    app.dependency_overrides[token_issuer] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        issued = await client.post("/api/workbench/agent/install-command")
        looked = await client.post(
            "/api/workbench/agent-pairing/lookup", json={"user_code": CODE}
        )
        approved = await client.post(
            f"/api/workbench/agent-pairing/{PAIRING_ID}/approve",
            json={"user_code": CODE},
        )
    assert issued.status_code == 200, issued.text
    assert looked.status_code == 200, looked.text
    assert approved.status_code == 200, approved.text
    command = issued.json()
    found = looked.json()
    decision = approved.json()
    assert command["command"] == (
        f"irm 'https://downloads.example/api/agent-install/{CREDENTIAL}/script' | iex"
    )
    assert command["expires_at"].startswith("2026-10-10T10:10:00")
    assert found["requested_seconds_ago"] == 12
    assert found["replaces_machine"] == replaces
    assert decision == {"status": "approved", "machine_name": "家里的电脑"}
    assert TOKEN not in json.dumps(decision)
    _write(
        scenario,
        "agent-install-command.json",
        "POST",
        "/api/workbench/agent/install-command",
        command,
    )
    _write(
        scenario,
        "agent-pairing-lookup.json",
        "POST",
        "/api/workbench/agent-pairing/lookup",
        found,
    )
    _write(
        scenario,
        "agent-pairing-approve.json",
        "POST",
        f"/api/workbench/agent-pairing/{PAIRING_ID}/approve",
        decision,
    )


async def test_install_ticket_serves_issued_script_and_retries_after_storage_recovers(
    monkeypatch,
):
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.application.workbench.agent_download import ReleaseUnavailable

    settings = _settings()
    issued = settings.workbench_agent_download
    template = " ".join(
        "{{" + key + "}}"
        for key in (
            "PACKAGE_URL",
            "VERSION",
            "SIZE_BYTES",
            "ZIP_SHA256",
            "MANIFEST_SHA256",
            "CODEX_VERSION",
        )
    )

    class Tickets(Store):
        uses = 0

        async def agent_install_release(self, digest, *, now):
            return {"release": issued.model_dump(mode="json"), "template": template}

        async def consume_agent_install_credential(self, digest, *, now):
            self.uses += 1
            return {"use_count": self.uses}

    class Objects:
        available = False

        def check(self, identity):
            assert (
                identity.key == issued.object_key
                and identity.sha256 == issued.zip_sha256
            )
            if not self.available:
                raise ReleaseUnavailable()
            return "object-version"

        def close(self):
            pass

    tickets, objects = Tickets(replaces=None), Objects()
    updated = settings.model_copy(update={"workbench_agent_download": None})
    monkeypatch.setattr(routes, "get_settings", lambda: updated)
    monkeypatch.setattr(routes, "require_workbench_enabled", lambda: None)
    monkeypatch.setattr(routes, "workbench_store", lambda _: tickets)
    monkeypatch.setattr(
        "app.bootstrap.workbench.agent_release_store", lambda _: objects
    )
    request = Request(
        {"type": "http", "method": "HEAD", "headers": [], "query_string": b""}
    )
    script = await routes.agent_install_script(CREDENTIAL, request, session=None)
    assert (
        issued.version.encode() in script.body
        and issued.zip_sha256.encode() in script.body
    )
    assert tickets.uses == 1
    with pytest.raises(HTTPException) as unavailable:
        await routes.agent_install_package(CREDENTIAL, request, session=None)
    assert unavailable.value.status_code == 503 and tickets.uses == 1
    objects.available = True
    package = await routes.agent_install_package(CREDENTIAL, request, session=None)
    assert (
        package.status_code == 200
        and package.headers["x-checksum-sha256"] == issued.zip_sha256
    )
    assert tickets.uses == 2
