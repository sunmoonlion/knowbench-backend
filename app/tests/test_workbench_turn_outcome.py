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


@pytest.mark.parametrize("receipt", ["stream", "full", "last_only", "replayed_receipt"])
async def test_planner_readback_reassembles_fragments_in_turn_order(receipt):
    import json

    link = SandboxLink(SimpleNamespace(), {"id": "sandbox"})
    w = {**waiter("thread-a"), "planning_messages": {}}
    link.turn_waiters = {"first": w}
    items = [
        {
            "type": "agentMessage",
            "id": "a",
            "phase": "commentary",
            "text": "Looking up slots",
        },
        {"type": "agentMessage", "id": "b", "text": '{"'},
        {
            "type": "agentMessage",
            "id": "c",
            "text": 'action":"inspect","methods":["m2"]}',
        },
    ]
    if receipt != "full":
        for item in [*items, items[-1]]:
            link._feed_turn_waiters(
                "item/completed",
                {"threadId": "thread-a", "turnId": "first", "item": item},
            )
    final = {
        "full": items,
        "last_only": items[-1:],
        "replayed_receipt": [*items, items[-1]],
        "stream": [],
    }[receipt]
    link._feed_turn_waiters(
        "turn/completed",
        {
            "threadId": "thread-a",
            "turn": {"id": "first", "status": "completed", "items": final},
        },
    )
    assert json.loads(w["text"]) == {"action": "inspect", "methods": ["m2"]}


async def test_turn_completion_merges_unidentified_stream_fragments():
    from app.application.workbench.turn import merge_turn_messages

    part = {"type": "agentMessage", "text": "A"}
    last = {"type": "agentMessage", "text": "B"}
    assert merge_turn_messages([part, part], [part, part, last]) == [part, part, last]
    for planning in (True, False):
        link = SandboxLink(SimpleNamespace(), {"id": "sandbox"})
        w = waiter("thread")
        if planning:
            w["planning_messages"] = {}
        link.turn_waiters = {"turn": w}
        item = {"type": "agentMessage", "text": '{"decision":"gap"}'}
        if planning:
            link._feed_turn_waiters(
                "item/completed", {"threadId": "thread", "turnId": "turn", "item": item}
            )
        link._feed_turn_waiters(
            "turn/completed",
            {
                "threadId": "thread",
                "turn": {"id": "turn", "status": "completed", "items": [item]},
            },
        )
        import json

        assert json.loads(w["text"]) == json.loads(item["text"])


def test_mcp_form_uses_the_selected_values():
    from app.domain.workbench.elicitation import form_questions, form_reply

    schema = {
        "type": "object",
        "required": ["publish"],
        "properties": {"publish": {"type": "boolean", "default": True}},
    }
    assert [o["label"] for o in form_questions(schema)[0]["options"]] == [
        "true",
        "false",
    ]
    assert form_reply(schema, {"answers": {"publish": {"answers": ["false"]}}}) == {
        "action": "accept",
        "content": {"publish": False},
    }
    assert form_reply(schema, {"answers": {"publish": {"answers": []}}}) == {
        "action": "decline"
    }
