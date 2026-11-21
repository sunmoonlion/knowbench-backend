"""一段对话的记录，给专家读（PRD/apps/investment.md 7.4）：什么进记录，什么不进。"""

from __future__ import annotations

import json

from app.domain.workbench.native_functions import project_read_revision
from app.domain.workbench.records import (
    PAGE_ENTRIES,
    TEXT_CHARS,
    entry_of,
    page_of,
    record_of,
)
from app.domain.workbench.tool_schema import schema_validator

METHOD = "# Advisor step: 勾稽\nYou are executing one step ... ## Method for this step"


def event(kind, payload, at="2026-09-29T10:00:00Z"):
    return {"type": kind, "payload": payload, "created_at": at}


def item(**fields):
    return event("item/completed", {"turnId": "t1", "item": fields})


def test_who_said_what_and_what_was_done():
    events = [
        event("turn/requested", {"by": "user", "text": "恒瑞的毛利率怎么样"}),
        item(
            type="userMessage", content=[{"type": "text", "text": "恒瑞的毛利率怎么样"}]
        ),
        item(type="reasoning", summary=["想一想"]),
        item(type="agentMessage", text="我先看看项目里有什么"),
        item(type="commandExecution", command="ls -la", status="completed", exitCode=0),
        item(
            type="mcpToolCall",
            tool="run_sql",
            status="completed",
            arguments={"dataset": "sh600276-financials", "sql": "select 1"},
            result={"content": [{"type": "text", "text": "ROWS"}]},
        ),
        item(
            type="fileChange",
            status="completed",
            changes=[{"path": "notes.md", "diff": "+secret line"}],
        ),
        event("turn/completed", {"turn": {"id": "t1"}}),
    ]
    assert record_of(events) == [
        {"at": "2026-09-29T10:00:00Z", "who": "user", "said": "恒瑞的毛利率怎么样"},
        {
            "at": "2026-09-29T10:00:00Z",
            "who": "assistant",
            "said": "我先看看项目里有什么",
        },
        {
            "at": "2026-09-29T10:00:00Z",
            "who": "assistant",
            "did": "command",
            "brief": "ls -la",
            "ok": True,
        },
        {
            "at": "2026-09-29T10:00:00Z",
            "who": "assistant",
            "did": "tool",
            "name": "run_sql",
            "brief": "select 1",
            "ok": True,
        },
        {
            "at": "2026-09-29T10:00:00Z",
            "who": "assistant",
            "did": "file_change",
            "brief": "notes.md",
            "ok": True,
        },
    ]


def test_what_never_gets_into_a_record():
    events = [
        # 专家每一步发给模型的话：Codex 回显成 userMessage，里面是方法的原文
        item(type="userMessage", content=[{"type": "text", "text": METHOD}]),
        # 专家发起的轮次不算用户说的话
        event("turn/requested", {"by": "advisor", "text": METHOD}),
        # 答复用的令牌
        event(
            "interaction/opened",
            {
                "interaction_id": "i1",
                "token": "TOKEN-THAT-MUST-NOT-LEAK",
                "prompt": {"title": "第「勾稽」步没有通过验收", "subject": {"x": 1}},
            },
        ),
        # 工具审批的待办没有标题：整条不进
        event("interaction/opened", {"token": "TOKEN-2", "subject": {"command": "rm"}}),
        event("session/mode_announced", {"note": "[workbench] ..."}),
        event("thread/tokenUsage/updated", {"tokenUsage": {}}),
        item(
            type="mcpToolCall",
            tool="run_sql",
            status="completed",
            arguments={"sql": "select * from t"},
            result={"content": [{"type": "text", "text": "ROWS-THAT-MUST-NOT-LEAK"}]},
        ),
        item(
            type="fileChange",
            status="completed",
            changes=[{"path": "a", "diff": "DIFF"}],
        ),
    ]
    shown = json.dumps(record_of(events), ensure_ascii=False)
    for secret in ("TOKEN", "Advisor step", "Method for this step", "ROWS-", "DIFF"):
        assert secret not in shown, secret
    assert "第「勾稽」步没有通过验收" in shown
    assert len(record_of(events)) == 3


def test_the_experts_steps_are_told_in_short():
    assert entry_of(
        event(
            "step/rejected",
            {
                "step_id": "reconcile",
                "failures": ["all_equal(checks): 不平"],
                "checks": [],
            },
        )
    ) == {
        "at": "2026-09-29T10:00:00Z",
        "who": "expert",
        "did": "step",
        "step": "reconcile",
        "outcome": "rejected",
        "failures": ["all_equal(checks): 不平"],
    }
    assert entry_of(event("task/state", {"to": "RUNNING"})) is None
    assert entry_of(event("task/state", {"to": "SUCCEEDED"}))["did"] == "ended"


def test_long_words_are_cut_and_pages_have_a_limit():
    long = entry_of(event("turn/requested", {"text": "长" * (TEXT_CHARS + 50)}))
    assert len(long["said"]) == TEXT_CHARS + 1 and long["said"].endswith("…")
    entries = [{"n": n} for n in range(PAGE_ENTRIES * 2 + 3)]
    first = page_of(entries, 1)
    assert (first["page"], first["pages"], len(first["entries"])) == (
        1,
        3,
        PAGE_ENTRIES,
    )
    last = page_of(entries, 3)
    assert [e["n"] for e in last["entries"]] == [80, 81, 82]
    # 页码乱填：不报错，给能给的
    assert page_of(entries, 99)["page"] == 3
    assert page_of(entries, 0)["page"] == 1
    assert page_of(entries, "x")["page"] == 1
    assert page_of([], None) == {"page": 1, "pages": 1, "entries": []}


def test_registered_page_covers_record_variants_and_rejects_internal_fields():
    entries = record_of(
        [
            event("task/received", {}),
            event("step/accepted", {}),
            event("task/state", {"to": "CANCELLED"}),
            event("interaction/opened", {"prompt": {"title": "confirm"}}),
            event("turn/requested", {"text": "hello"}),
            item(type="agentMessage", text="reply"),
            item(type="fileChange"),
            item(type="commandExecution"),
            item(type="mcpToolCall"),
        ]
    )
    page = {
        "conversation": "s1",
        "kind": "expert",
        "title": None,
        **page_of(entries, 1),
    }
    check = schema_validator(project_read_revision().contract.output_schema)
    check.validate(page)
    assert not check.is_valid(page | {"entries": [entries[0] | {"token": "internal"}]})
