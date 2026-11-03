"""一个委托的步骤视图（PRD/apps/investment-expert.md 第六节、第十节第 4 项）。

由后端算一次，页面照着画：页面不自己从事件里推状态，免得两处算得不一样。
这里只有纯函数：进来的是账里的行，出去的是给页面的结构。方法的原文不在返回值里。
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

from app.domain.workbench.field_words import labels_of
from app.domain.workbench.pack_view import step_view
from app.domain.workbench.packs import ExpertPack
from app.domain.workbench.states import TASK_TERMINAL, AttemptState, TaskState

# 步骤轨上的记号（6.1）
PENDING = "pending"  # ○ 还没做
RUNNING = "running"  # ● 正在做
ACCEPTED = "accepted"  # ✓ 做完，验收通过
REWORKING = "reworking"  # ↻ 验收没过，正在重做
WENT_BACK = "went_back"  # ↩ 验收没过，退回到了前面的某一步
REDO = "redo"  # 做过并且通过了，但后面的步骤退回到了它之前，要重做
WAITING = "waiting"  # ⏸ 停下来等你
FAILED = "failed"  # ✕ 这一步失败，委托终止
CANCELLED = "cancelled"  # 取消时停在这一步
NOT_REACHED = "not_reached"  # 委托已经结束，没有做到这一步

TERMINAL_WORDS = {
    TaskState.SUCCEEDED: "已完成",
    TaskState.REJECTED: "打回",
    TaskState.FAILED: "失败",
    TaskState.CANCELLED: "已取消",
}


def reason_text(state: str, reason: dict[str, Any] | None) -> str:
    """委托为什么停在这个终态，给用户看的一句话。账里记的是内部的词。"""
    reason = reason or {}
    if state == TaskState.SUCCEEDED:
        return ""
    if reason.get("code") == "no_expert_pack":
        return "这个问题不在专家的范围里，可以自己在对话里接着做"
    if reason.get("code") == "pack_version_gone":
        return "这个委托是按专家的旧版建的，旧版已经换下，请重新请一次专家"
    if reason.get("code") == "no_thread":
        return "这段对话还没有准备好"
    if reason.get("code") == "missing_input":
        return "前面步骤的交回物缺了，做不下去"
    if reason.get("code") == "out_of_scope":
        return "专家看这个问题不归它，你选择了不做；可以直接在对话里问"
    if state == TaskState.CANCELLED:
        return "你取消了这个委托"
    if reason.get("by") == "user stopped":
        return "专家停下来问你，你选择了停止"
    if reason.get("budget"):
        return "预算用完了，你没有追加"
    if reason.get("error"):
        return "专家那一头出错了，这一步没有做完"
    if state == TaskState.REJECTED:
        return str(reason.get("message") or "专家没有接这个委托")
    return "没有做完" if state == TaskState.FAILED else ""


BRIEF_CHARS = 200
RAW_CHARS = 20000


def amount(value: Any) -> Decimal:
    """预算账里的数：可能是 {"amount": "0.05"}、"0.05"、空。"""
    if isinstance(value, dict):
        value = value.get("amount") or value.get("consumed") or value.get("used")
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal("0")
    except ArithmeticError:
        return Decimal("0")


def money(value: Any) -> str:
    """金额的写法：至少两位小数，多出来的零去掉。账里存的是六位小数。"""
    number = amount(value)
    if not number.is_finite():
        return "0.00"
    text = format(number.normalize(), "f")
    whole, _, decimals = text.partition(".")
    return f"{whole}.{decimals.ljust(2, '0')}"


def budget_view(budget: dict[str, Any], *, running: Any = None) -> dict[str, Any]:
    """花了多少。`used` 是做完的各步记下的；`running` 是正在做的这一步到现在花的；
    `spent` 是两样相加，页面上跳动的就是它。上限可以没有：没有就不显示上限与剩余。
    """
    used = amount(budget.get("used"))
    reserved = amount(budget.get("reserved"))
    now = amount(running)
    capped = budget.get("limit") is not None
    limit = amount(budget.get("limit"))
    return {
        "currency": budget.get("currency") or "CNY",
        "limit": money(limit) if capped else None,
        "used": money(used),
        "reserved": money(reserved),
        "left": money(limit - used - reserved) if capped else None,
        "running": money(now),
        "spent": money(used + now),
        "estimated": True,
    }


def attempt_outcome(attempt: dict[str, Any]) -> str:
    status = attempt["status"]
    if status == AttemptState.COMPLETED:
        return "accepted"
    if status == AttemptState.FAILED:
        return "rejected" if attempt.get("failure_code") == "acceptance" else "failed"
    if status == AttemptState.BUDGET_EXCEEDED:
        return "over_budget"
    if status in (AttemptState.CANCELLED, AttemptState.ABANDONED):
        return "cancelled"
    return "running"


def check_lines(
    recorded: list[dict[str, Any]] | None, step: dict[str, Any]
) -> list[dict[str, Any]]:
    """验收清单：每条一行。名字优先用当时记下的；老账里没有名字的，按顺序对回专家包。"""
    labels = [c["label"] for c in step["checks"]]
    if not recorded:
        return [{"label": label, "pass": None, "message": ""} for label in labels]
    lines = []
    for i, check in enumerate(recorded):
        label = check.get("label") or (labels[i] if i < len(labels) else "")
        lines.append(
            {
                "label": label or str(check.get("rule") or ""),
                "pass": check.get("pass"),
                "message": "" if check.get("pass") else str(check.get("message") or ""),
            }
        )
    return lines


def brief(value: Any) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= BRIEF_CHARS else text[:BRIEF_CHARS].rstrip() + "…"


def process_line(event: dict[str, Any]) -> dict[str, Any] | None:
    """过程里的一行：一次查询、一次读文件。只给摘要，不给结果的内容。"""
    item = (event.get("payload") or {}).get("item") or {}
    kind = item.get("type")
    if kind == "mcpToolCall":
        arguments = item.get("arguments") or {}
        said = arguments if isinstance(arguments, dict) else {"input": arguments}
        return {
            "at": event.get("created_at"),
            "kind": "tool",
            "name": str(item.get("tool") or ""),
            "dataset": said.get("dataset"),
            "brief": brief(
                said.get("sql")
                or said.get("metrics")
                or said.get("table")
                or said.get("metric")
                or ""
            ),
            "ok": item.get("status") == "completed" and not item.get("error"),
        }
    if kind == "commandExecution":
        return {
            "at": event.get("created_at"),
            "kind": "command",
            "name": "命令",
            "dataset": None,
            "brief": brief(item.get("command") or ""),
            "ok": item.get("status") == "completed"
            and item.get("exitCode") in (0, None),
        }
    return None


def data_of(content: Any) -> dict[str, Any] | None:
    """交回物里写明的数据集、版本、数据时点。没写就是不知道，不猜。"""
    if not isinstance(content, dict):
        return None
    found = {
        key: content[key]
        for key in ("dataset", "data_version", "as_of", "security_code")
        if content.get(key) not in (None, "")
    }
    return found if "dataset" in found or "data_version" in found else None


def attempts_view(
    step: dict[str, Any],
    attempts: list[dict[str, Any]],
    verdicts: dict[str, dict[str, Any]],
    contents: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    full: bool,
) -> list[dict[str, Any]]:
    out = []
    for n, attempt in enumerate(attempts, start=1):
        attempt_id = str(attempt["id"])
        verdict = verdicts.get(attempt_id) or {}
        outcome = attempt_outcome(attempt)
        turns = {str(t) for t in attempt.get("turn_ids") or []}
        lines = [
            line
            for line in (
                process_line(e)
                for e in items
                if str((e.get("payload") or {}).get("turnId")) in turns
            )
            if line is not None
        ]
        entry: dict[str, Any] = {
            "attempt_id": attempt_id,
            "n": n,
            "outcome": outcome,
            "failure": attempt.get("failure_code"),
            "spent": money(attempt.get("budget_consumed")),
            "started_at": attempt.get("started_at") or attempt.get("created_at"),
            "ended_at": attempt.get("ended_at"),
            "checks": check_lines(verdict.get("checks"), step)
            if outcome in ("accepted", "rejected")
            else check_lines(None, step),
            "tools": dict(Counter(line["name"] for line in lines)),
        }
        if full:
            returned = None
            if outcome == "accepted":
                for ref in attempt.get("output_artifacts") or []:
                    returned = {
                        "artifact": ref.get("name"),
                        "version": ref.get("version"),
                        "content": contents.get(str(ref.get("id"))),
                    }
            elif outcome == "rejected":
                kept = verdict.get("returned") or {}
                returned = {
                    "artifact": step["returns"],
                    "version": None,
                    "content": kept.get("content"),
                    "raw": kept.get("raw"),
                }
            if returned is not None:
                # 字段各叫什么：页面上不让用户看英文字段名
                returned["labels"] = labels_of(returned.get("content"))
            entry["returned"] = returned
            entry["process"] = lines
        out.append(entry)
    return out


def step_status(
    index: int,
    attempts: list[dict[str, Any]],
    task: dict[str, Any],
) -> str:
    state = task["state"]
    current = int(task["current_step"])
    last = attempt_outcome(attempts[-1]) if attempts else None
    if state == TaskState.RECEIVED:
        return PENDING
    if state in TASK_TERMINAL:
        if last == "accepted" and (state == TaskState.SUCCEEDED or index < current):
            return ACCEPTED
        if index == current and state == TaskState.FAILED:
            return FAILED
        if index == current and state == TaskState.CANCELLED:
            return CANCELLED
        if last == "rejected" and index > current:
            return WENT_BACK
        return NOT_REACHED
    if index == current:
        if state == TaskState.WAITING:
            return WAITING
        if last == "rejected":
            return REWORKING
        return RUNNING
    if index < current:
        return ACCEPTED if last == "accepted" else PENDING
    if last == "rejected":
        return WENT_BACK
    if last == "accepted":
        return REDO
    return PENDING


def steps_view(
    pack: ExpertPack,
    task: dict[str, Any],
    attempts: list[dict[str, Any]],
    verdicts: dict[str, dict[str, Any]],
    contents: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    only: int | None = None,
) -> list[dict[str, Any]]:
    """每一步一项。only 给了第几步（从 1 起），就只算那一步并带上交回物与过程。"""
    out = []
    for index, contract in enumerate(pack.workflow):
        if only is not None and index + 1 != only:
            continue
        shown = step_view(contract, pack, index)
        mine = [a for a in attempts if a.get("step_id") == contract.step_id]
        tries = attempts_view(
            shown, mine, verdicts, contents, items, full=only is not None
        )
        rejected = sum(1 for t in tries if t["outcome"] == "rejected")
        data = None
        for attempt in reversed(mine):
            if attempt_outcome(attempt) != "accepted":
                continue
            for ref in attempt.get("output_artifacts") or []:
                data = data_of(contents.get(str(ref.get("id"))))
            break
        out.append(
            {
                **shown,
                "status": step_status(index, mine, task),
                "attempts": tries,
                "times": len(tries),
                "rejected": rejected,
                "spent": money(sum((Decimal(t["spent"]) for t in tries), Decimal("0"))),
                "data": data,
            }
        )
    return out


def position(pack: ExpertPack, task: dict[str, Any]) -> dict[str, Any]:
    """做到第几步，共几步。做完了就是最后一步。"""
    total = len(pack.workflow)
    current = min(int(task["current_step"]), total - 1) if total else 0
    step = pack.workflow[current] if total else None
    return {
        "step": current + 1 if total else 0,
        "of": total,
        "title": step.title if step else "",
        "left": max(0, total - int(task["current_step"])),
    }
