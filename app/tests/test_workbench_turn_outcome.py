"""Protocol failures must stay scoped to their turn and cannot become success."""

import asyncio
from types import SimpleNamespace

import pytest

from app.application.workbench.runner import SandboxLink


def waiter(thread):
    return {
        "thread_id": thread,
        "future": asyncio.get_running_loop().create_future(),
        "text": None,
        "reported": False,
        "error": None,
        "env_lost": False,
        "interrupted": False,
        "files": [],
        "timed_out": False,
    }


async def test_error_is_scoped_to_the_matching_thread_and_turn():
    link = SandboxLink(SimpleNamespace(), {"id": "sandbox"})
    first, other = waiter("thread-a"), waiter("thread-b")
    link.turn_waiters = {"turn-a": first, "turn-b": other}
    link._feed_turn_waiters(
        "error",
        {
            "threadId": "thread-a",
            "turnId": "turn-a",
            "willRetry": False,
            "error": {"message": "tool call result missing"},
        },
    )
    assert first["error"] == "tool call result missing"
    assert other["error"] is None


async def test_retry_notification_does_not_poison_successful_turn():
    link = SandboxLink(SimpleNamespace(), {"id": "sandbox"})
    w = waiter("thread-a")
    link.turn_waiters = {"turn-a": w}
    link._feed_turn_waiters(
        "error",
        {
            "threadId": "thread-a",
            "turnId": "turn-a",
            "willRetry": True,
            "error": {"message": "temporary upstream failure"},
        },
    )
    link._feed_turn_waiters(
        "turn/completed",
        {
            "threadId": "thread-a",
            "turn": {"id": "turn-a", "status": "completed"},
        },
    )
    assert w["future"].done()
    assert w["error"] is None


@pytest.mark.parametrize(
    "thread,turn", [("thread-b", "turn-a"), ("thread-a", "old-turn"), (None, None)]
)
async def test_unrelated_or_unscoped_error_cannot_change_active_turn(thread, turn):
    link = SandboxLink(SimpleNamespace(), {"id": "sandbox"})
    w = waiter("thread-a")
    link.turn_waiters = {"turn-a": w}
    link._feed_turn_waiters(
        "error",
        {
            "threadId": thread,
            "turnId": turn,
            "willRetry": False,
            "error": {"message": "unrelated failure"},
        },
    )
    assert w["error"] is None


async def test_terminal_failure_preserves_error_and_partial_text():
    link = SandboxLink(SimpleNamespace(), {"id": "sandbox"})
    w = waiter("thread-a")
    link.turn_waiters = {"turn-a": w}
    link._feed_turn_waiters(
        "item/completed",
        {
            "threadId": "thread-a",
            "turnId": "turn-a",
            "item": {"type": "agentMessage", "text": "I will look up the company."},
        },
    )
    link._feed_turn_waiters(
        "turn/completed",
        {
            "threadId": "thread-a",
            "turn": {
                "id": "turn-a",
                "status": "failed",
                "error": {"message": "tool call result missing"},
            },
        },
    )
    assert w["future"].done()
    assert w["text"] == "I will look up the company."
    assert w["error"] == "tool call result missing"


async def test_interruption_remains_explicit_after_an_error_notification():
    link = SandboxLink(SimpleNamespace(), {"id": "sandbox"})
    w = waiter("thread-a")
    w["error"] = "earlier error"
    link.turn_waiters = {"turn-a": w}
    link._feed_turn_waiters(
        "turn/completed",
        {
            "threadId": "thread-a",
            "turn": {"id": "turn-a", "status": "interrupted"},
        },
    )
    assert w["interrupted"]
