"""一段对话的记录，给专家读（PRD/apps/investment.md 7.4、F-PROJ-06）。

谁说了什么、做了什么。命令与改动只给摘要；答复用的令牌、模型 key、专家每一步发给模型的
方法原文都不在里面。纯函数：进来的是账里的事件，出去的是一条条记录。
"""

from __future__ import annotations

import json
from typing import Any

from app.domain.workbench.projects import TOOL_RESULT_MAX_BYTES

PAGE_ENTRIES = 40
TEXT_CHARS = 2000
BRIEF_CHARS = 200
FIRST_CHARS = 200
DOSSIER_CHARS = 30000


def clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def one_line(value: Any, limit: int = BRIEF_CHARS) -> str:
    return clip(" ".join(str(value or "").split()), limit)


def entry_of(event: dict[str, Any]) -> dict[str, Any] | None:
    """一条事件变成一条记录。不该给专家看的、没有内容的，返回 None。"""
    kind, payload = event["type"], event.get("payload") or {}
    at = event.get("created_at")
    if kind == "turn/requested":
        # 用户的话只从这里取。Codex 回显的 userMessage 里混着专家每一步的方法原文，不用
        if payload.get("by", "user") != "user" or not payload.get("text"):
            return None
        return {"at": at, "who": "user", "said": clip(payload["text"], TEXT_CHARS)}
    if kind == "task/received":
        return {
            "at": at,
            "who": "user",
            "did": "asked_the_expert",
            "expert": payload.get("profile_id"),
        }
    if kind in ("step/accepted", "step/rejected"):
        return {
            "at": at,
            "who": "expert",
            "did": "step",
            "step": payload.get("step_id"),
            "outcome": "accepted" if kind == "step/accepted" else "rejected",
            "failures": [one_line(f) for f in payload.get("failures") or []],
        }
    if kind == "task/state":
        if payload.get("to") not in ("SUCCEEDED", "REJECTED", "FAILED", "CANCELLED"):
            return None
        return {"at": at, "who": "expert", "did": "ended", "state": payload.get("to")}
    if kind == "interaction/opened":
        # 只给标题。事件里还有答复用的令牌，那个不给
        title = (payload.get("prompt") or {}).get("title")
        return (
            {
                "at": at,
                "who": "expert",
                "did": "asked_the_user",
                "about": one_line(title),
            }
            if title
            else None
        )
    if kind != "item/completed":
        return None
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    what = item.get("type")
    if what == "agentMessage":
        if not item.get("text"):
            return None
        return {"at": at, "who": "assistant", "said": clip(item["text"], TEXT_CHARS)}
    if what == "commandExecution":
        return {
            "at": at,
            "who": "assistant",
            "did": "command",
            "brief": one_line(item.get("command")),
            "ok": item.get("status") == "completed"
            and item.get("exitCode") in (0, None),
        }
    if what == "fileChange":
        paths = [
            str(c.get("path"))
            for c in item.get("changes") or []
            if isinstance(c, dict) and c.get("path")
        ]
        return {
            "at": at,
            "who": "assistant",
            "did": "file_change",
            "brief": one_line(", ".join(paths)),
            "ok": item.get("status") == "completed",
        }
    if what == "mcpToolCall":
        arguments = item.get("arguments")
        said = arguments if isinstance(arguments, dict) else {}
        return {
            "at": at,
            "who": "assistant",
            "did": "tool",
            "name": str(item.get("tool") or ""),
            "brief": one_line(said.get("sql") or said.get("dataset") or ""),
            "ok": item.get("status") == "completed",
        }
    return None


def record_of(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in (entry_of(event) for event in events) if e is not None]


def page_of(entries: list[dict[str, Any]], page: Any) -> dict[str, Any]:
    """分页，每页有上限。页码不对就给第一页，不报错：模型常常乱填。"""
    blocks: list[list[dict[str, Any]]] = [[]]
    used = 0
    for entry in entries:
        size = len(json.dumps(entry, ensure_ascii=False, default=str).encode()) + 2
        if blocks[-1] and (
            len(blocks[-1]) >= PAGE_ENTRIES
            or used + size > TOOL_RESULT_MAX_BYTES - 4096
        ):
            blocks.append([])
            used = 0
        blocks[-1].append(entry)
        used += size
    pages = len(blocks)
    try:
        number = int(page)
    except (TypeError, ValueError):
        number = 1
    number = min(max(1, number), pages)
    return {
        "page": number,
        "pages": pages,
        "entries": blocks[number - 1],
    }
