"""Check lease bookkeeping on rollback and answer provenance on link loss."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, create_autospec

from app.application.ports.workbench import EventPublisher
from app.application.workbench.runner import Runner, SandboxLink


async def test_rolled_back_takeover_is_repeated_on_retry():
    repo = AsyncMock()
    repo.renew_leases.return_value = []
    repo.acquire_leases.return_value = ["sandbox"]
    repo.claim_commands.side_effect = [
        RuntimeError("queue temporarily unavailable"),
        [],
    ]
    commits = []

    @asynccontextmanager
    async def transaction():
        yield
        commits.append(True)

    @asynccontextmanager
    async def stores():
        yield repo

    repo.transaction = transaction
    runner = Runner(stores, connector=None, publisher=None, runner_id="local")
    observed = []
    runner._take_over = AsyncMock(return_value=[("session", {"kind": "expired"})])

    async def publish(events):
        assert commits == [True]
        observed.extend(events)

    runner._publish_expirations = publish
    runner._reconcile = AsyncMock(return_value=[])
    try:
        await runner.run_once()
    except RuntimeError as exc:
        assert str(exc) == "queue temporarily unavailable"
    assert commits == [] and observed == []
    await runner.run_once()
    assert commits == [True]
    assert runner._take_over.await_count == 2 and observed == [
        ("session", {"kind": "expired"})
    ]


async def test_lost_link_records_disconnect():
    runner = Runner(None, connector=None, publisher=None, runner_id="local")
    link = SandboxLink(runner, {"id": "sandbox"})
    runner.links["sandbox"] = link
    waiting = asyncio.create_task(link._wait_answer("question", timeout=1))
    await asyncio.sleep(0)
    await runner._drop_link("sandbox", "lease changed")
    assert await waiting == ({"decision": "decline"}, "connection_lost")


async def test_usage_retry_after_failed_append():
    repo = AsyncMock()
    repo.append_event.side_effect = [RuntimeError("write failed"), {}]

    @asynccontextmanager
    async def stores():
        yield repo

    repo.transaction = stores
    link = SandboxLink(
        Runner(
            stores,
            connector=None,
            publisher=create_autospec(EventPublisher, instance=True),
            runner_id="local",
        ),
        {"id": "sandbox"},
    )
    link.threads["thread"] = "session"
    link.models["thread"] = "kimi-k3"
    link.totals["thread"] = None
    report = {
        "threadId": "thread",
        "turnId": "turn",
        "tokenUsage": {"last": {"inputTokens": 41}, "total": {"inputTokens": 41}},
    }
    try:
        await link.on_notification("thread/tokenUsage/updated", report)
    except RuntimeError as exc:
        assert str(exc) == "write failed"
    await link.on_notification("thread/tokenUsage/updated", report)
    assert repo.append_event.await_count == 2
    await link.on_notification("thread/tokenUsage/updated", report)
    assert repo.append_event.await_count == 2
    assert link.turn_usage["turn"].input == 41


async def test_transient_mode_note_error_allows_retry():
    from app.application.ports.workbench import AppServerError
    from app.application.workbench.runner import TurnPlan

    link = SandboxLink(None, {"id": "sandbox"})
    client = AsyncMock()
    client.request.side_effect = [
        AppServerError("thread/inject_items", {"message": "transient"}),
        {},
    ]
    link.ensure_connected = AsyncMock(return_value=client)
    plan = TurnPlan(mode="chat", facts="fixture")
    await link.announce("thread", plan)
    await link.announce("thread", plan)
    assert client.request.await_count == 2


async def test_malformed_error_completes_pending_request():
    from unittest.mock import MagicMock

    import pytest

    from app.infrastructure.workbench.app_server_client import (
        AppServerClient,
        AppServerError,
    )

    client = AppServerClient(
        "ws://test", "test", on_notification=AsyncMock(), on_server_request=AsyncMock()
    )
    pending = asyncio.get_running_loop().create_future()
    client._pending[1] = ("test", pending, None)
    client._ws = MagicMock()
    client._ws.__aiter__.return_value = ['{"id":1,"error":"invalid"}']
    await client._read_loop()
    assert pending.done()
    with pytest.raises(AppServerError, match="Malformed"):
        pending.result()


async def test_native_answer_can_arrive_before_opened_notification_returns():
    runner = Runner(None, connector=None, publisher=None, runner_id="r")
    link = SandboxLink(runner, {"id": "sandbox"})
    link.active_turns["thread"] = "new"

    async def open_question(**kwargs):
        link._forget_turn("thread", "old")
        assert link.resolve_approval("request", {"decision": "accept"})
        return {"interaction_id": "question"}

    link._open_ask = open_question
    opened, answer = await link._ask_and_wait("request", "thread", "new")
    assert opened["interaction_id"] == "question" and answer == (
        {"decision": "accept"},
        "user",
    )
    assert not link.pending_approvals and not link.pending_threads
