"""登记 CLI：按邮箱找用户，幂等登记环境与沙箱，不回显令牌。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from test_workbench_ledger_db import db as db  # noqa: F401

from app.cli.workbench_register import parse_args, register


async def test_register_is_idempotent_and_needs_a_logged_in_user(db, capsys):  # noqa: F811
    email = f"demo-{uuid.uuid4().hex[:6]}@example.com"
    args = parse_args(
        [
            "--email",
            email,
            "--root",
            "/home/u/research",
            "--sandbox-url",
            "ws://sandbox-demo.sandbox-pool.svc:47800",
            "--token-ref",
            "env:SANDBOX_DEMO_APP_SERVER_TOKEN",
        ]
    )
    with pytest.raises(SystemExit, match="log in once first"):
        await register(db, args)
    async with db() as s:
        await s.execute(
            text(
                "insert into auth_user (id, issuer, subject, username, email, display_name, roles, scopes) "
                "values (:id, 'https://casdoor.test', :sub, 'demo', :email, 'Demo', '[]'::jsonb, '[]'::jsonb)"
            ),
            {"id": str(uuid.uuid4()), "sub": f"sub-{email}", "email": email},
        )
        await s.commit()
    first = await register(db, args)
    second = await register(db, args)
    assert first["environment_id"] == second["environment_id"]
    assert first["sandbox_id"] == second["sandbox_id"]
    assert not first["environment_reused"] and second["environment_reused"]
    assert not first["sandbox_reused"] and second["sandbox_reused"]
    assert "SANDBOX_DEMO_APP_SERVER_TOKEN" not in str(first)

    async with db() as s:
        await s.execute(
            text(
                "update workbench_sandboxes set operator_registered=false, token_ref='inline:stale'"
            )
        )
        await s.commit()
    revalidated = await register(db, args)
    assert revalidated["sandbox_id"] == first["sandbox_id"]
    async with db() as s:
        row = (
            await s.execute(
                text("select operator_registered, token_ref from workbench_sandboxes")
            )
        ).one()
        assert row == (True, args.token_ref)
        await s.execute(
            text(
                "insert into auth_user(id, issuer, subject, email, roles, scopes) "
                "values (:id, 'other', 'second-user', :email, '[]', '[]')"
            ),
            {"id": str(uuid.uuid4()), "email": email},
        )
        await s.commit()
    with pytest.raises(SystemExit, match="multiple accounts"):
        await register(db, args)
    args.email = None
    args.subject = f"sub-{email}"
    assert (await register(db, args))["owner_actor_id"] == first["owner_actor_id"]


def test_unverified_connection_is_rejected_before_credentials_are_resolved(monkeypatch):
    from unittest.mock import Mock

    from app.infrastructure.workbench import app_server_client

    resolve = Mock(return_value="test")
    monkeypatch.setattr(app_server_client, "resolve_token_ref", resolve)
    connect = app_server_client.WsAppServerConnector()
    callbacks = {"on_notification": None, "on_server_request": None}
    with pytest.raises(RuntimeError, match="operator registration"):
        connect.open({"operator_registered": False, "provisioned": False}, **callbacks)
    resolve.assert_not_called()
    for trust in ("operator_registered", "provisioned"):
        connect.open(
            {trust: True, "app_server_url": "ws://test", "token_ref": "test"},
            **callbacks,
        )
    assert resolve.call_count == 2
