"""审查面：专家（或 Codex）停下来问我（PRD/apps/investment-expert.md 第八节；初稿 4.4 的五要素）。

五块，缺一不可：在哪、待决的是什么、决定的对象、时效、决定记录。
每个选项后面跟「选了会怎样」，这句话只在这里生成：页面不自己编。纯函数，不碰数据库。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.domain.workbench.packs import ExpertPack
from app.domain.workbench.step_view import brief, budget_view

AFTER_EXPIRY = "过期不等于同意，也不等于拒绝：委托停在等待，要重新请专家或重新发起"

KIND_WORDS = {
    "input": "验收没有通过",
    "resource": "预算",
    "tool_approval": "命令要你批准",
    "question": "Codex 想问你",
}


OUT_OF_SCOPE = "问题不归这位专家"


def consequence(
    option: str,
    kind: str,
    *,
    reserve: str | None,
    done: int | None,
    out_of_scope: bool = False,
) -> str:
    """选了会怎样。包括要不要再花钱。"""
    if out_of_scope:  # 受理关口（账 130）：还一步没做
        if option == "proceed":
            return "专家按你本次明确接受的范围继续，未承接部分仍会说明；不会因此获得新的方法或数据权限。费用实时显示"
        if option == "stop":
            return "委托取消，不再花钱。这个问题可以直接在对话里问"
    finished = "做完的部分" if done is None else f"做完的 {done} 步"
    if kind == "resource":
        if option == "topup":
            return "追加的金额计入预算上限，专家接着做"
        return f"专家停下，把{finished}交回给你。已花的不退"
    if kind == "tool_approval":
        return {
            "accept": "只允许这一次。你机器上的白名单仍然有效，白名单之外的写入照样会被拒",
            "accept_task": "这个任务里同样的动作不再问你，任务结束即失效。白名单之外的写入照样会被拒",
            "acceptForSession": "这段对话里同样的动作不再问你。白名单之外的写入照样会被拒",
            "decline": "不执行这一条。这一轮继续，它会接着做不依赖它的部分；之后同一步里再要做什么都会先问你",
            "cancel": "整轮停下。专家停在这一步等你决定：带着你的要求重做，还是停止",
        }.get(option, "")
    if kind == "question":
        return ""
    if option == "rework":
        # 不说「预留多少」：请专家已经不填预算上限了，花多少页面上实时看得到
        return "专家重做这一步。重做要再花钱，花了多少顶部实时显示"
    if option == "stop":
        return f"专家停下，把{finished}交回给你。已花的不退"
    return ""


def why(
    kind: str,
    step: dict[str, Any] | None,
    rejected: int,
    *,
    backs: int = 0,
    out_of_scope: bool = False,
) -> str:
    if out_of_scope:
        return "专家看这个问题不归它，还没开始做，先停下来问你"
    if kind == "resource":
        return "预算不够做下一步了。专家不会超出你给的上限"
    if kind == "tool_approval":
        return "这个动作超出了当前允许的范围，要你点头才做"
    if kind == "question":
        return "模型缺一点信息，问你一下再继续；不答它会按自己的假设做"
    if step is None:
        return "这一步没有通过验收"
    if backs:
        return (
            f"这一步做了 {rejected} 次都没有通过验收，其间退回前面重做过 {backs} 次。"
            "专家不再自己来回，停下来问你"
        )
    if step["after_rejection"]["reworks"] == 0:
        return "这一步没有通过验收。这一步不通过时专家不重做，直接问你"
    return f"这一步做了 {rejected} 次，都没有通过验收"


def review_view(
    interaction: dict[str, Any],
    *,
    now: datetime,
    project: dict[str, Any] | None,
    conversation: dict[str, Any],
    task: dict[str, Any] | None,
    pack: ExpertPack | None,
    step: dict[str, Any] | None,
    question: str,
    opened_cursor: int | None,
    summary: bool = False,
) -> dict[str, Any]:
    """step 是步骤视图里的那一步（带每一次的验收结果）；工具审批没有委托，也就没有步骤。"""
    prompt = interaction.get("prompt") or {}
    kind = str(interaction["kind"])
    subject = dict(prompt.get("subject") or {})
    declined = bool(subject.get("out_of_scope"))
    last = (step or {}).get("attempts") or []
    failed = [
        {"label": c["label"], "message": c["message"]}
        for c in (last[-1]["checks"] if last else [])
        if c["pass"] is False
    ]
    done = None
    if task is not None:
        done = int(task["current_step"])
    expires = interaction.get("expires_at")
    status = str(interaction["status"])
    if status == "pending" and expires is not None and expires <= now:
        status = "expired"  # 到点了还没人来改状态：按实际的说
    where = {
        "project": None
        if project is None
        else {"id": str(project["id"]), "title": project["title"]},
        "conversation": {
            "id": str(conversation["id"]),
            "title": conversation.get("title"),
            "kind": conversation.get("kind"),
        },
        "task": None
        if task is None
        else {
            "id": str(task["id"]),
            "question": question,
            "expert": (pack.name or pack.title) if pack else "",
        },
        "step": None
        if step is None
        else {"index": step["index"], "title": step["title"]},
        "about": OUT_OF_SCOPE if declined else KIND_WORDS.get(kind, kind),
    }
    view: dict[str, Any] = {
        "interaction_id": str(interaction["id"]),
        "kind": kind,
        "status": status,
        "where": where,
        "why": prompt.get("question", "")
        if subject.get("planning_failure")
        else why(
            kind,
            step,
            len([a for a in last if a["outcome"] == "rejected"]),
            backs=int(subject.get("backs") or 0),
            out_of_scope=declined,
        ),
        "failed": failed,
        "expires_at": expires,
        "created_at": interaction.get("created_at"),
    }
    if summary:
        return view
    reserve = step["reserve"] if step else None
    view["pending"] = {
        "title": prompt.get("title") or "",
        "question": prompt.get("question") or "",
        "options": [
            {
                "id": option.get("id"),
                "label": option.get("label"),
                "consequence": consequence(
                    str(option.get("id")),
                    kind,
                    reserve=reserve,
                    done=done,
                    out_of_scope=declined,
                ),
                "needs_amount": kind == "resource" and option.get("id") == "topup",
            }
            for option in prompt.get("options") or []
        ],
        "unknowns": list(prompt.get("unknowns") or []),
        "evidence": list(prompt.get("evidence") or []),
    }
    view["subject"] = {
        "digest": interaction.get("subject_digest"),
        "target_state_version": interaction.get("target_state_version"),
        "command": brief(subject["command"]) if subject.get("command") else None,
        "cwd": subject.get("cwd"),
        "returned": subject.get("last_output"),
        "budget": budget_view(subject["budget"]) if subject.get("budget") else None,
        # 工具级审批与提问卡多带的（approval.md「问人怎么问」第 2 条）
        "after_decline": bool(subject.get("after_decline")),
        "dependents": list(subject.get("dependents") or []),
        "questions": list(subject.get("questions") or []),
        "note": subject.get("note"),
    }
    view["validity"] = {
        "expires_at": expires,
        "seconds_left": None
        if expires is None
        else max(0, int((expires - now).total_seconds())),
        "after_expiry": AFTER_EXPIRY,
    }
    decided = status == "consumed"
    view["decision"] = {
        "decided": decided,
        "at": interaction.get("consumed_at") if decided else None,
        "by": str(interaction["responded_by"])
        if decided and interaction.get("responded_by")
        else None,
        "chosen": (interaction.get("response") or {}).get("decision")
        if decided
        else None,
        "amount": (interaction.get("response") or {}).get("amount")
        if decided
        else None,
        "cancelled_because": (interaction.get("response") or {}).get("cancelled")
        if status == "cancelled"
        else None,
    }
    # 答复要带令牌。令牌只在开出这件事的那条事件里：给出它在哪，页面去那里取
    view["opened_event"] = {
        "session_id": str(conversation["id"]),
        "cursor": opened_cursor,
    }
    return view
