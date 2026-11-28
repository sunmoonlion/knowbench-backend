"""Database concurrency and cleanup checks for onboarding (disposable PostgreSQL only)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from test_workbench_ledger_db import db as db

from app.infrastructure.workbench.repository import WorkbenchRepository


async def _create_pairing(factory, *, status="pending", expires_at=None):
    pairing_id = str(uuid.uuid4())
    row = {
        "id": pairing_id,
        "code_hash": uuid.uuid4().hex * 2,
        "device_secret_hash": "a" * 64,
        "machine_name": "test-machine",
        "os": "Windows",
        "agent_version": "0.2.0",
        "codex_version": "0.155.1",
        "source_ip": "unknown",
        "expires_at": expires_at or datetime.now(UTC) + timedelta(minutes=5),
    }
    async with factory() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            assert await repo.create_agent_pairing(row)
            if status != "pending":
                assert await repo.update_agent_pairing(
                    pairing_id,
                    expected_status="pending",
                    status=status,
                    owner_actor_id=str(uuid.uuid4()),
                    relay_user="relay-user",
                    relay_url="wss://relay.example.test",
                    token_ciphertext="ciphertext-only",
                    agent_token_expires_at=datetime.now(UTC) + timedelta(days=30),
                    decided_at=datetime.now(UTC),
                )
    return pairing_id


@pytest.mark.asyncio
async def test_concurrent_delivery_is_one_time_and_clears_ciphertext(db):
    pairing_id = await _create_pairing(db, status="approved")

    async def deliver():
        async with db() as session:
            repo = WorkbenchRepository(session)
            async with repo.transaction():
                return await repo.mark_agent_pairing_delivered(pairing_id)

    first, second = await asyncio.gather(deliver(), deliver())
    delivered = [row for row in (first, second) if row is not None]
    assert len(delivered) == 1
    assert delivered[0]["token_ciphertext"] == "ciphertext-only"
    async with db() as session:
        row = await WorkbenchRepository(session).get_agent_pairing(pairing_id)
    assert row["status"] == "delivered"
    assert row["token_ciphertext"] is None


@pytest.mark.asyncio
async def test_pending_cap_is_reserved_atomically(db):
    async def create(code_hash):
        async with db() as session:
            repo = WorkbenchRepository(session)
            async with repo.transaction():
                return await repo.create_agent_pairing(
                    {
                        "id": str(uuid.uuid4()),
                        "code_hash": code_hash,
                        "device_secret_hash": "b" * 64,
                        "machine_name": "test-machine",
                        "os": "Windows",
                        "agent_version": "0.2.0",
                        "codex_version": "0.155.1",
                        "source_ip": "unknown",
                        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
                    },
                    pending_limit=1,
                )

    results = await asyncio.gather(create("c" * 64), create("d" * 64))
    assert sum(results) == 1


@pytest.mark.asyncio
async def test_install_credential_use_limit_and_cleanup(db):
    owner = str(uuid.uuid4())
    token_hash = "e" * 64
    expired_hash = "f" * 64
    now = datetime.now(UTC)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await repo.create_agent_install_credential(
                owner_actor_id=owner,
                token_hash=token_hash,
                release_snapshot={"release": {"version": "1.0.0"}},
                expires_at=now + timedelta(minutes=10),
            )
            await repo.create_agent_install_credential(
                owner_actor_id=owner,
                token_hash=expired_hash,
                release_snapshot={"release": {"version": "1.0.0"}},
                expires_at=now - timedelta(seconds=1),
            )

    async with db() as session:
        repo = WorkbenchRepository(session)
        assert await repo.agent_install_release(token_hash, now=now) == {
            "release": {"version": "1.0.0"}
        }
        assert await repo.agent_install_release(expired_hash, now=now) is None

    async def consume():
        async with db() as session:
            repo = WorkbenchRepository(session)
            async with repo.transaction():
                return await repo.consume_agent_install_credential(token_hash, now=now)

    assert (
        len(
            [row for row in await asyncio.gather(*(consume() for _ in range(7))) if row]
        )
        == 5
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            assert await repo.agent_install_release(token_hash, now=now) is None
            result = await repo.cleanup_agent_onboarding(now=now)
    assert result == {"pairings": 0, "install_credentials": 1}
