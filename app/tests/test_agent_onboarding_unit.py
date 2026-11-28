from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.application.sensitive_paths import redact_sensitive_path
from app.application.workbench.agent_onboarding import (
    AgentOnboarding,
    code_digest,
    install_digest,
    new_install_credential,
    secret_digest,
)
from app.bootstrap.api import create_app
from app.domain.workbench.errors import AgentPairingRejected, AgentPairingSlowDown
from app.infrastructure.logging.logging import _AccessPathRedactor
from app.interfaces.endpoints.agent_onboarding_routes import pairing_verify_url
from app.interfaces.http.client_ip import source_ip
from core.config import Settings


def test_pairing_verify_url_points_at_the_settings_computer_anchor():
    assert (
        pairing_verify_url("https://knowbench.example/")
        == "https://knowbench.example/zh-CN/workbench/settings/computer#computer"
    )
    assert (
        pairing_verify_url("https://knowbench.example")
        == "https://knowbench.example/zh-CN/workbench/settings/computer#computer"
    )


def test_pairing_verify_url_carries_the_code_so_the_user_only_confirms():
    assert (
        pairing_verify_url("https://knowbench.example", "AB3D-7K9Q")
        == "https://knowbench.example/zh-CN/workbench/settings/computer?code=AB3D-7K9Q#computer"
    )


class Cipher:
    def encrypt(self, value: bytes) -> bytes:
        return b"enc:" + value

    def decrypt(self, value: bytes) -> bytes:
        return value.removeprefix(b"enc:")


class PairingStore:
    def __init__(self, row=None):
        self.row = row
        self.inserted = None
        if self.row is not None:
            self.row.setdefault("last_polled_at", None)
            self.row.setdefault("attempts", 0)

    @asynccontextmanager
    async def transaction(self):
        yield

    async def create_agent_pairing(self, row, *, pending_limit=500):
        self.inserted = row
        now = datetime.now(UTC)
        self.row = row | {
            "created_at": now,
            "status": "pending",
            "attempts": 0,
            "last_polled_at": None,
        }
        return True

    async def get_agent_pairing(self, pairing_id, *, for_update=False):
        if self.row and self.row["id"] == pairing_id:
            return self.row
        return None

    async def update_agent_pairing(self, pairing_id, *, expected_status, **fields):
        if not self.row or self.row["status"] != expected_status:
            return False
        self.row.update(fields)
        return True

    async def mark_agent_pairing_delivered(self, pairing_id):
        if not self.row or self.row["status"] != "approved":
            return None
        delivered = self.row.copy()
        self.row.update(status="delivered", token_ciphertext=None)
        return delivered


class UnusedProvisioning:
    pass


@pytest.mark.asyncio
async def test_create_pairing_persists_only_code_digest_and_secret_digest():
    store = PairingStore()
    service = AgentOnboarding(
        store,
        cipher=Cipher(),
        pairing_hmac_key=b"k" * 32,
        provisioning=UnusedProvisioning(),
    )
    device_secret = "d" * 43
    result = await service.create_request(
        machine_name="workstation",
        os_name="Windows 11",
        agent_version="0.2.0",
        codex_version="0.155.1",
        device_secret_sha256=secret_digest(device_secret),
        source_ip="unknown",
        verify_url=lambda code: f"https://example.test/settings?code={code}#computer",
    )
    assert len(result["user_code"].replace("-", "")) == 8
    assert result["verify_url"] == (
        f"https://example.test/settings?code={result['user_code']}#computer"
    )
    assert store.inserted["code_hash"] == code_digest(result["user_code"], b"k" * 32)
    assert result["user_code"] not in str(store.inserted)
    assert store.inserted["device_secret_hash"] == secret_digest(device_secret)
    assert device_secret not in str(store.inserted)


@pytest.mark.asyncio
async def test_poll_delivers_token_once_and_clears_ciphertext():
    device_secret = "s" * 43
    store = PairingStore(
        {
            "id": "request-id",
            "status": "approved",
            "device_secret_hash": secret_digest(device_secret),
            "expires_at": datetime.now(UTC) + timedelta(minutes=4),
            "token_ciphertext": "enc:agent-token",
            "relay_url": "wss://relay.example.test",
            "relay_user": "relay-user",
            "agent_token_expires_at": datetime.now(UTC) + timedelta(days=30),
        }
    )
    service = AgentOnboarding(
        store,
        cipher=Cipher(),
        pairing_hmac_key=b"k" * 32,
        provisioning=UnusedProvisioning(),
    )
    delivered = await service.poll(pairing_id="request-id", device_secret=device_secret)
    assert delivered["status"] == "approved"
    assert delivered["agent_token"] == "agent-token"
    assert store.row["status"] == "delivered"
    assert store.row["token_ciphertext"] is None
    assert await service.poll(pairing_id="request-id", device_secret=device_secret) == {
        "status": "delivered"
    }


@pytest.mark.asyncio
async def test_poll_treats_empty_delivery_ciphertext_as_already_delivered():
    device_secret = "s" * 43
    store = PairingStore(
        {
            "id": "request-id",
            "status": "approved",
            "device_secret_hash": secret_digest(device_secret),
            "expires_at": datetime.now(UTC) + timedelta(minutes=4),
            "token_ciphertext": None,
            "relay_url": "wss://relay.example.test",
            "relay_user": "relay-user",
            "agent_token_expires_at": datetime.now(UTC) + timedelta(days=30),
        }
    )
    service = AgentOnboarding(
        store,
        cipher=Cipher(),
        pairing_hmac_key=b"k" * 32,
        provisioning=UnusedProvisioning(),
    )
    assert await service.poll(pairing_id="request-id", device_secret=device_secret) == {
        "status": "delivered"
    }


@pytest.mark.asyncio
async def test_wrong_device_secret_is_not_found():
    store = PairingStore(
        {
            "id": "request-id",
            "status": "pending",
            "device_secret_hash": secret_digest("a" * 43),
            "expires_at": datetime.now(UTC) + timedelta(minutes=4),
        }
    )
    service = AgentOnboarding(
        store,
        cipher=Cipher(),
        pairing_hmac_key=b"k" * 32,
        provisioning=UnusedProvisioning(),
    )
    with pytest.raises(AgentPairingRejected) as error:
        await service.poll(pairing_id="request-id", device_secret="b" * 43)
    assert error.value.http_status == 404


@pytest.mark.asyncio
async def test_poll_interval_and_expiry_are_enforced():
    device_secret = "s" * 43
    now = datetime.now(UTC)
    store = PairingStore(
        {
            "id": "request-id",
            "status": "pending",
            "device_secret_hash": secret_digest(device_secret),
            "expires_at": now + timedelta(minutes=4),
            "last_polled_at": now - timedelta(seconds=1),
            "attempts": 1,
        }
    )
    service = AgentOnboarding(
        store,
        cipher=Cipher(),
        pairing_hmac_key=b"k" * 32,
        provisioning=UnusedProvisioning(),
    )
    with pytest.raises(AgentPairingSlowDown):
        await service.poll(pairing_id="request-id", device_secret=device_secret)

    store.row["expires_at"] = now - timedelta(seconds=1)
    store.row["status"] = "approved"
    store.row["token_ciphertext"] = "enc:expired-token"
    assert await service.poll(pairing_id="request-id", device_secret=device_secret) == {
        "status": "expired"
    }
    assert store.row["token_ciphertext"] is None


def test_install_credential_is_high_entropy_and_only_its_digest_is_persisted():
    credential = new_install_credential()
    assert len(credential) >= 40
    digest = install_digest(credential)
    assert digest == hashlib.sha256(credential.encode("ascii")).hexdigest()
    assert credential not in digest


@pytest.mark.asyncio
async def test_invalid_unicode_approval_code_returns_not_found():
    store = PairingStore()
    service = AgentOnboarding(
        store,
        cipher=Cipher(),
        pairing_hmac_key=b"k" * 32,
        provisioning=UnusedProvisioning(),
    )
    with pytest.raises(AgentPairingRejected) as error:
        await service.approve(
            owner_actor_id="owner", pairing_id="request-id", user_code="💥💥💥💥"
        )
    assert error.value.http_status == 404


def test_forwarded_address_is_ignored_without_explicit_trust():
    assert source_ip("192.0.2.10", "198.51.100.44", "") == "192.0.2.10"
    assert source_ip("192.0.2.10", "198.51.100.44", "192.0.2.0/24") == "198.51.100.44"
    assert source_ip("192.0.2.10", None, "192.0.2.0/24") == "unknown"
    assert source_ip("192.0.2.10", "spoofed, bad-address", "192.0.2.0/24") == "unknown"
    assert source_ip("203.0.113.5", "198.51.100.44", "192.0.2.0/24") == "203.0.113.5"
    assert source_ip(None, "198.51.100.44", "") == "unknown"


def test_forwarded_chain_skips_trusted_hops_from_right():
    assert (
        source_ip(
            "10.20.0.8",
            "198.51.100.44, 127.0.0.1, 10.20.0.9",
            "127.0.0.1/32,10.20.0.0/24",
        )
        == "198.51.100.44"
    )


def test_forwarded_chain_does_not_trust_a_forged_leftmost_address():
    assert (
        source_ip(
            "10.20.0.8",
            "203.0.113.200, 198.51.100.44, 127.0.0.1, 10.20.0.9",
            "127.0.0.1/32,10.20.0.0/24",
        )
        == "198.51.100.44"
    )


def test_forwarded_chain_accepts_multiple_header_fields_in_wire_order():
    assert (
        source_ip(
            "10.20.0.8",
            ["198.51.100.44, 127.0.0.1", "10.20.0.9"],
            "127.0.0.1/32,10.20.0.0/24",
        )
        == "198.51.100.44"
    )


def test_fully_trusted_chain_uses_leftmost_and_invalid_chain_is_unknown():
    trusted = "127.0.0.1/32,10.20.0.0/24"
    assert source_ip("10.20.0.8", "10.20.0.2, 127.0.0.1", trusted) == "10.20.0.2"
    assert (
        source_ip("10.20.0.8", "198.51.100.44, invalid, 10.20.0.9", trusted)
        == "unknown"
    )


def test_onboarding_routes_are_registered_and_config_defaults_to_no_proxy_trust():
    app = create_app(Settings())
    paths = {route.path for route in app.routes}
    assert "/api/agent-pairing/requests" in paths
    assert "/api/agent-pairing/requests/{pairing_id}/poll" in paths
    assert "/api/agent-pairing/requests/{pairing_id}/cancel" in paths
    assert "/api/workbench/agent-pairing/lookup" in paths
    assert "/api/workbench/agent-pairing/{pairing_id}/approve" in paths
    assert "/api/workbench/agent-pairing/{pairing_id}/deny" in paths
    assert "/api/workbench/agent/install-command" in paths
    assert "/api/agent-install/{credential}/script" in paths
    assert "/api/agent-install/{credential}/package" in paths
    schema = app.openapi()["paths"]
    for path in (
        "/api/workbench/agent/package",
        "/api/agent-install/{credential}/package",
    ):
        assert schema[path]["get"]["operationId"] != schema[path]["head"]["operationId"]
    for path, method in (
        ("/api/workbench/sessions", "post"),
        ("/api/workbench/sessions/{session_id}/turns", "post"),
        ("/api/workbench/sessions/{session_id}/stream", "get"),
    ):
        assert method in schema[path]
    assert Settings().workbench_trusted_proxy_cidrs == ""
    with pytest.raises(ValidationError):
        Settings(WORKBENCH_TRUSTED_PROXY_CIDRS="not-a-network")


def test_install_capability_path_redaction_removes_token_and_query():
    value = "/api/agent-install/Abcdefghijklmnopqrstuvwxyz0123456789_ABCDE/script?secret=query"
    redacted = redact_sensitive_path(value)
    assert "Abcdefgh" not in redacted
    assert "secret=query" not in redacted
    assert redacted == "/api/agent-install/[redacted]/script"


def test_uvicorn_access_log_redacts_install_capability():
    credential = "Abcdefghijklmnopqrstuvwxyz0123456789_ABCDE"
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "server.py",
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", f"/api/agent-install/{credential}/package", "1.1", 200),
        None,
    )
    assert _AccessPathRedactor().filter(record)
    line = record.getMessage()
    assert credential not in line
    assert "/api/agent-install/[redacted]/package" in line
