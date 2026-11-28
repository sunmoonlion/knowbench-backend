"""长期记忆：跨对话、跨项目记住的关于这个用户的几条事实（0019 §四）。纯函数。

第一期只有「关于用户的事实」：关注的行业与公司、口径偏好、风险偏好、输出习惯。
每段对话开头带一段 ≤ 300 字；用户随时能停用、改、删。不用 Codex 自带的 memories。
"""

from __future__ import annotations

from typing import Any

NOTE_LIMIT = 300
MAX_ITEMS = 20


def memory_note(
    memories: list[dict[str, Any]], *, limit: int = NOTE_LIMIT
) -> str | None:
    """把启用的事实拼成给模型看的一段。超出上限就截在整条边界，并说明还有几条没带。"""
    facts = [
        str(m["text"]).strip()
        for m in memories
        if m.get("enabled", True) and str(m.get("text", "")).strip()
    ]
    if not facts or limit <= 0:
        return None
    heading = "[workbench] About this user (facts they confirmed; use them, do not restate them):"
    lines = [heading]
    for index, fact in enumerate(facts[:MAX_ITEMS]):
        remaining = len(facts) - index - 1
        suffix = f"\n- ({remaining} more facts not shown)" if remaining else ""
        prefix = "\n".join(lines) + "\n- "
        if len(prefix) + len(fact) + len(suffix) > limit:
            if index == 0:
                available = limit - len(prefix) - len(suffix) - 1
                if available < 1:
                    return None
                return prefix + fact[:available] + "…" + suffix
            lines.append(f"- ({remaining + 1} more facts not shown)")
            break
        lines.append("- " + fact)
    else:
        if len(facts) > MAX_ITEMS:
            lines.append(f"- ({len(facts) - MAX_ITEMS} more facts not shown)")
    return "\n".join(lines)
