"""专家处理期间一直告诉用户的三样：现在在干什么、为什么、没有卡住（所有者 2026-10-04）。

只用我们自己写的话和真的信号。模型的思考不放出来：它会复述专家包的方法。
不另外调模型来写解说：要花用户的钱，还可能说出没在发生的事。
纯函数，不碰数据库。
"""

from __future__ import annotations

from typing import Any

from app.domain.workbench.states import TaskState, WaitingReason

LIVE = (TaskState.QUEUED, TaskState.RUNNING, TaskState.WAITING)

DOING = {
    "queued": "排着队，马上开始这一步",
    "thinking": "在想这一步怎么做",
    "data": "在查数据",
    "file": "在读项目里的文件",
    "writing": "在写这一步的交回物",
    "checking": "这一步做完了，在逐条验收",
    "waiting": "停下来了，在等你决定",
    "offline": "你的机器断开了，在等它回来",
}


def doing_of(events: list[dict[str, Any]]) -> tuple[str, str | None]:
    """从这一次开做之后的事件里看它这会儿在干什么。返回（哪一种，细节）。"""
    code, detail = "queued", None
    for event in events:
        kind = event["type"]
        if kind == "turn/started":
            code, detail = "thinking", None
        elif kind == "turn/completed":
            code, detail = "checking", None
        elif kind in ("item/started", "item/completed"):
            item = event["payload"].get("item") or {}
            if kind == "item/completed":
                code, detail = "thinking", None  # 做完一件事，接着想
            elif item.get("type") == "mcpToolCall":
                dataset = (item.get("arguments") or {}).get("dataset")
                code, detail = "data", str(dataset) if dataset else None
            elif item.get("type") == "commandExecution":
                code, detail = "file", None
            elif item.get("type") == "agentMessage":
                code, detail = "writing", None
            elif item.get("type") == "reasoning":
                code, detail = "thinking", None
    return code, detail


def now_view(
    task: dict[str, Any],
    steps: list[dict[str, Any]],
    *,
    events: list[dict[str, Any]],
    since: Any,
    held: bool,
) -> dict[str, Any] | None:
    """steps 是步骤视图；events 是这一次开做之后这段对话的事件；since 是这一次开做的时间。

    委托已经结束时返回 None：没有「现在」。
    """
    state = task["state"]
    if state not in LIVE or not steps:
        return None
    index = min(int(task["current_step"]), len(steps) - 1)
    step = steps[index]
    if state == TaskState.WAITING:
        offline = task.get("waiting_reason") == WaitingReason.ENVIRONMENT
        code, detail = ("offline" if offline else "waiting"), None
    else:
        code, detail = doing_of(events)
    data_wait = (
        state == TaskState.WAITING and task.get("waiting_reason") == WaitingReason.DATA
    )
    text = DOING[code] + (f"：{detail}" if detail else "")
    times = int(step.get("times") or 0)
    rejected = int(step.get("rejected") or 0)
    redo = None
    if state != TaskState.WAITING and rejected:
        # 验收没过再做一次，是专家按规矩办事。说出来，用户才不会以为出了错
        redo = f"上一次没有通过验收，这是第 {times or rejected + 1} 次做。重做是按规矩来，不是出错"
    last = events[-1]["created_at"] if events else since
    return {
        "step": {
            "index": step["index"],
            "of": len(steps),
            "title": "数据准备" if data_wait else step["title"],
            "summary": "等待数据确认或补采完成"
            if data_wait
            else step.get("summary") or "",
            "why": (
                "需要先核对并确认本次的数据范围；获准的数据准备好后，专家才继续分析"
                if data_wait
                else step.get("why") or ""
            ),
        },
        "doing": {"code": code, "text": text},
        "redo": redo,
        # 两个时间给页面算「这一步做了多久」「最近一次动静在几秒前」
        "since": since,
        "last_event_at": last,
        # runner 还握着沙箱：它在干活，没有掉线。等你决定的时候不握着是正常的
        "held": held,
    }
