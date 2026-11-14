"""One-time authority invalidation keeps evidence and cannot revive implicit grants."""

import json

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_calls_db import migration
from test_capabilities_db import OWNER, approval, prepared, service
from test_method_authorization_db import setup
from test_workbench_ledger_db import db as db  # noqa: F401
from tool_invocation_support import request_call

from app.application.workbench.ledger import Ledger
from app.domain.workbench.capabilities import SelectionPut
from app.domain.workbench.method_authorization import MethodAuthorization
from app.domain.workbench.states import AttemptState
from app.infrastructure.workbench.repository import WorkbenchRepository


async def test_upgrade_invalidates_implicit_authority_preserves_evidence_and_chat(db):
    _, sid, key, token, remote, opened = await setup(db)
    chat_sid, _, chat_key, chat_token, chat_remote = await prepared(db)
    module = migration("20261115_0044_direct_method_grants.py")
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        pending = await request_call(svc, sid, token, key, {"to": "test@example.test"})
        chat_call = await request_call(
            service(repo, chat_remote),
            chat_sid,
            chat_token,
            chat_key,
            {"to": "test@example.test"},
        )
        rows = (
            (await s.execute(text("SELECT * FROM workbench_method_authorizations")))
            .mappings()
            .all()
        )
        before = {}
        for row in rows:
            legacy = row["document"]
            for grant in legacy["grants"]:
                grant.pop("recipient_call_id")
                grant["delegable"] = True
            before[str(row["call_id"])] = legacy
            await s.execute(
                text(
                    "UPDATE workbench_method_authorizations SET document=cast(:d as jsonb) WHERE call_id=:id"
                ),
                {"id": row["call_id"], "d": json.dumps(legacy)},
            )
        await s.commit()

        def upgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                module.upgrade()

        await (await s.connection()).run_sync(upgrade)
        await s.commit()
        for call_id, original in before.items():
            raw = await repo.method_authorization(OWNER, call_id)
            auth = MethodAuthorization.model_validate(raw)
            assert auth.revoked and auth.revision == original["revision"] + 1
            assert all(
                not g.delegable and g.recipient_call_id is None for g in auth.grants
            )
            evidence = await s.scalar(
                text(
                    "SELECT detail->'authorization_before_0044' FROM workbench_calls WHERE id=:id"
                ),
                {"id": call_id},
            )
            assert evidence == original
        selected = await repo.capability_selection(OWNER, sid)
        assert selected["runtime"]["aborted"]
        attempt = await repo.get_attempt(opened["attempt_id"])
        assert (
            attempt["refs"]["permission_revocation"]["context"]
            == selected["runtime"]["method"]["context"]
        )
        expired = await repo.capability_call(OWNER, pending["call_id"])
        assert expired["status"] == "expired"
        assert (await repo.get_interaction(expired["interaction_id"]))[
            "status"
        ] == "expired"
        assert (await svc.decide(OWNER, expired["id"], approval(expired)))[
            "status"
        ] == "expired"
        assert not remote.calls
        assert (await repo.capability_call(OWNER, chat_call["call_id"]))[
            "status"
        ] == "awaiting_approval"
        assert not (await repo.capability_selection(OWNER, chat_sid))["runtime"].get(
            "aborted"
        )

        # Once the stopped attempt is settled, an explicit confirmation issues
        # only new exact-call grants. The archived documents are never read.
        await Ledger(repo).attempt_transition(opened["attempt_id"], AttemptState.FAILED)
        panel = await svc.selection(OWNER, sid)
        await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=panel["revision"],
                method_context=panel["method"]["context"],
                tools=[key],
                skills=[],
                purpose="Deliver the notice",
            ),
        )
        child_id = attempt["refs"]["method_call_id"]
        child = MethodAuthorization.model_validate(
            await repo.method_authorization(OWNER, child_id)
        )
        assert not child.revoked
        assert all(
            not g.delegable and str(g.recipient_call_id) == child_id
            for g in child.grants
        )
        await s.commit()

        def downgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                module.downgrade()

        with pytest.raises(
            DBAPIError, match="direct method grants require forward repair"
        ):
            await (await s.connection()).run_sync(downgrade)
        await s.rollback()
        assert (await repo.method_authorization(OWNER, child_id))["revoked"] is False
