"""Business rules for browser-approved local-agent pairing and install capabilities."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.application.ports.workbench import Cipher, WorkbenchStore
from app.application.workbench.provisioning import SandboxProvisioning
from app.domain.workbench.errors import (
    AgentPairingBusy,
    AgentPairingRejected,
    AgentPairingSlowDown,
    AgentPairingUnavailable,
)

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_TTL = timedelta(minutes=5)
POLL_INTERVAL = timedelta(seconds=3)
INSTALL_TTL = timedelta(minutes=10)
INSTALL_MAX_USES = 5


def normalized_code(value: str) -> str:
    return value.replace("-", "").strip().upper()


def code_digest(value: str, key: bytes) -> str:
    return hmac.new(
        key, normalized_code(value).encode("ascii"), hashlib.sha256
    ).hexdigest()


def secret_digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def install_digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def valid_pairing_code(value: str) -> str | None:
    normalized = normalized_code(value)
    if len(normalized) != 8 or any(
        character not in CODE_ALPHABET for character in normalized
    ):
        return None
    return normalized


class AgentOnboarding:
    def __init__(
        self,
        repo: WorkbenchStore,
        *,
        cipher: Cipher,
        pairing_hmac_key: bytes,
        provisioning: SandboxProvisioning | None = None,
    ) -> None:
        if len(pairing_hmac_key) < 32:
            raise AgentPairingUnavailable("pairing key is not configured")
        self.repo = repo
        self.cipher = cipher
        self.pairing_hmac_key = pairing_hmac_key
        self.provisioning = provisioning

    async def create_request(
        self,
        *,
        machine_name: str,
        os_name: str,
        agent_version: str,
        codex_version: str,
        device_secret_sha256: str,
        source_ip: str,
        verify_url: str | Callable[[str], str],
    ) -> dict[str, Any]:
        if len(device_secret_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in device_secret_sha256
        ):
            raise AgentPairingRejected("invalid pairing request")
        for _ in range(8):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
            pairing_id = str(uuid.uuid4())
            now = datetime.now(UTC)
            inserted = await self.repo.create_agent_pairing(
                {
                    "id": pairing_id,
                    "code_hash": code_digest(code, self.pairing_hmac_key),
                    "device_secret_hash": device_secret_sha256,
                    "machine_name": machine_name.strip(),
                    "os": os_name,
                    "agent_version": agent_version,
                    "codex_version": codex_version,
                    "source_ip": source_ip,
                    "expires_at": now + PAIRING_TTL,
                }
            )
            if inserted:
                return {
                    "request_id": pairing_id,
                    "user_code": f"{code[:4]}-{code[4:]}",
                    "expires_in": int(PAIRING_TTL.total_seconds()),
                    "interval": int(POLL_INTERVAL.total_seconds()),
                    "verify_url": verify_url(f"{code[:4]}-{code[4:]}")
                    if callable(verify_url)
                    else verify_url,
                }
        raise AgentPairingUnavailable("pairing capacity unavailable")

    async def lookup(self, *, owner_actor_id: str, user_code: str) -> dict[str, Any]:
        normalized = valid_pairing_code(user_code)
        if normalized is None:
            raise AgentPairingRejected("码不对或已过期")
        row = await self.repo.get_pending_agent_pairing_by_code(
            code_digest(normalized, self.pairing_hmac_key)
        )
        if row is None:
            raise AgentPairingRejected("码不对或已过期")
        current = await self.repo.current_online_environment(owner_actor_id)
        created_at = row["created_at"]
        age = max(0, int((datetime.now(UTC) - created_at).total_seconds()))
        return {
            "id": str(row["id"]),
            "machine_name": row["machine_name"],
            "os": row["os"],
            "agent_version": row["agent_version"],
            "codex_version": row["codex_version"],
            "source_ip": row["source_ip"],
            "requested_seconds_ago": age,
            "replaces_machine": current["name"] if current else None,
        }

    async def approve(
        self,
        *,
        owner_actor_id: str,
        pairing_id: str,
        user_code: str,
    ) -> dict[str, Any]:
        if self.provisioning is None:
            raise AgentPairingUnavailable("relay provisioning is not configured")
        normalized = valid_pairing_code(user_code)
        if normalized is None:
            raise AgentPairingRejected("码不对或已过期")
        digest = code_digest(normalized, self.pairing_hmac_key)
        async with self.repo.relay_identity_operation(owner_actor_id):
            async with self.repo.transaction():
                row = await self.repo.get_agent_pairing(pairing_id, for_update=True)
                if (
                    row is None
                    or row["status"] != "pending"
                    or row["expires_at"] <= datetime.now(UTC)
                    or not hmac.compare_digest(row["code_hash"], digest)
                ):
                    raise AgentPairingRejected("码不对或已过期")
                previous = await self.repo.get_relay_identity(owner_actor_id)
                if previous is None:
                    (
                        identity,
                        agent_token,
                    ) = await self.provisioning.ensure_relay_identity(owner_actor_id)
                    if not agent_token:
                        raise AgentPairingUnavailable("could not issue agent identity")
                    expires_at = self.provisioning.identity_metadata(identity)[
                        "agent_token_expires_at"
                    ]
                    relay_user = str(identity["relay_user"])
                else:
                    revision = self.provisioning.identity_metadata(previous)[
                        "identity_revision"
                    ]
                    rotated = (
                        await self.provisioning.rotate_relay_identity_in_operation(
                            owner_actor_id, revision
                        )
                    )
                    agent_token = rotated["relay"]["agent_token"]
                    expires_at = rotated["relay"]["agent_token_expires_at"]
                    relay_user = rotated["relay"]["user"]
                parsed_expiry = (
                    datetime.fromisoformat(expires_at) if expires_at else None
                )
                ok = await self.repo.update_agent_pairing(
                    pairing_id,
                    expected_status="pending",
                    status="approved",
                    owner_actor_id=owner_actor_id,
                    relay_user=relay_user,
                    relay_url=self.provisioning.relay_public_url,
                    agent_token_expires_at=parsed_expiry,
                    token_ciphertext=self.cipher.encrypt(agent_token.encode()).decode(),
                    decided_at=datetime.now(UTC),
                )
                if not ok:
                    raise AgentPairingBusy("pairing request was already decided")
                await self.repo.append_agent_audit_event(
                    owner_actor_id=owner_actor_id,
                    event_type="agent/paired",
                    pairing_id=pairing_id,
                    metadata={
                        "machine_name": row["machine_name"],
                        "source_ip": row["source_ip"],
                        "requested_at": row["created_at"].isoformat(),
                        "request_id": pairing_id,
                    },
                )
        return {"status": "approved", "machine_name": row["machine_name"]}

    async def deny(self, *, owner_actor_id: str, pairing_id: str) -> None:
        async with self.repo.transaction():
            row = await self.repo.get_agent_pairing(pairing_id, for_update=True)
            if (
                row is None
                or row["status"] != "pending"
                or row["expires_at"] <= datetime.now(UTC)
            ):
                raise AgentPairingRejected("pairing request not found or expired")
            await self.repo.update_agent_pairing(
                pairing_id,
                expected_status="pending",
                status="denied",
                owner_actor_id=owner_actor_id,
                decided_at=datetime.now(UTC),
            )

    async def poll(self, *, pairing_id: str, device_secret: str) -> dict[str, Any]:
        expected = secret_digest(device_secret)
        async with self.repo.transaction():
            row = await self.repo.get_agent_pairing(pairing_id, for_update=True)
            if row is None or not hmac.compare_digest(
                row["device_secret_hash"], expected
            ):
                raise AgentPairingRejected("pairing request not found")
            now = datetime.now(UTC)
            if await self._expire_pairing(pairing_id, row, now):
                return {"status": "expired"}
            if row["status"] in ("pending", "approved"):
                previous = row["last_polled_at"]
                if previous and now - previous < POLL_INTERVAL:
                    raise AgentPairingSlowDown("poll interval is three seconds")
                if row["status"] == "pending":
                    await self.repo.update_agent_pairing(
                        pairing_id,
                        expected_status="pending",
                        attempts=int(row["attempts"]) + 1,
                        last_polled_at=now,
                    )
                    return {"status": "pending"}
                await self.repo.update_agent_pairing(
                    pairing_id,
                    expected_status="approved",
                    attempts=int(row["attempts"]) + 1,
                    last_polled_at=now,
                )
            if row["status"] == "approved":
                delivered = await self.repo.mark_agent_pairing_delivered(pairing_id)
                if delivered is None or not delivered.get("token_ciphertext"):
                    return {"status": "delivered"}
                token = self.cipher.decrypt(
                    delivered["token_ciphertext"].encode()
                ).decode()
                return {
                    "status": "approved",
                    "relay_url": delivered["relay_url"],
                    "relay_user": delivered["relay_user"],
                    "agent_token": token,
                    "agent_token_expires_at": (
                        delivered["agent_token_expires_at"].isoformat()
                        if delivered["agent_token_expires_at"]
                        else None
                    ),
                }
            return {"status": str(row["status"])}

    async def _expire_pairing(self, pairing_id, row, now) -> bool:
        if row["status"] in ("pending", "approved") and row["expires_at"] <= now:
            await self.repo.update_agent_pairing(
                pairing_id,
                expected_status=str(row["status"]),
                status="expired",
                token_ciphertext=None,
            )
            return True
        return False

    async def cancel(self, *, pairing_id: str, device_secret: str) -> dict[str, str]:
        expected = secret_digest(device_secret)
        async with self.repo.transaction():
            row = await self.repo.get_agent_pairing(pairing_id, for_update=True)
            if row is None or not hmac.compare_digest(
                row["device_secret_hash"], expected
            ):
                raise AgentPairingRejected("pairing request not found")
            if await self._expire_pairing(pairing_id, row, datetime.now(UTC)):
                return {"status": "expired"}
            if row["status"] == "pending":
                await self.repo.update_agent_pairing(
                    pairing_id,
                    expected_status="pending",
                    status="cancelled",
                    decided_at=datetime.now(UTC),
                )
                return {"status": "cancelled"}
            return {"status": str(row["status"])}


def new_install_credential() -> str:
    return secrets.token_urlsafe(32)
