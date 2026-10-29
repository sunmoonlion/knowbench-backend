"""底稿（PRD/apps/investment-expert.md 第七节）。

从上到下是：问了什么、答了什么、凭什么、哪些没做到、我怎么看。
每个专家包登记自己的底稿怎么排；没登记的用通用的排法。表与数取各步验过的交回物，
不取成稿里模型重抄的那一份。结论栏是用户的，这里永远不预填。纯函数，不碰数据库。
"""

from __future__ import annotations

import json
from typing import Any

from app.domain.workbench.packs import DossierBlock, ExpertPack
from app.domain.workbench.states import TaskState

SECTIONS = (
    ("answer", "回答"),
    ("data", "数据"),
    ("verified", "专家验过什么"),
    ("notes", "观察与提醒"),
    ("limits", "局限"),
    ("sources", "出处"),
)

NOT_REACHED = "没有做到这一步"

# 通用的排法：成稿里常见的几项各排到哪
GENERIC = (
    ("answer", "回答", "text", "answer"),
    ("table", "结果表", "table", "data"),
    ("tables", "表", "tables", "data"),
    ("limitations", "局限", "list", "limits"),
    ("citations", "出处", "list", "sources"),
)
HIDDEN_IN_GENERIC = frozenset({"conclusion"})


def pick(content: Any, path: str | None) -> Any:
    if not path:
        return content
    current = content
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def layout_of(pack: ExpertPack) -> tuple[DossierBlock, ...]:
    if pack.dossier:
        return pack.dossier
    last = pack.workflow[-1]
    shape = last.output_schema
    blocks = [
        DossierBlock(
            key=key,
            title=title,
            kind=kind,  # type: ignore[arg-type]
            artifact=last.output_artifact,
            path=key,
            section=section,  # type: ignore[arg-type]
        )
        for key, title, kind, section in GENERIC
        if key in shape
    ]
    return tuple(blocks)


def checks_summary(content: dict[str, Any]) -> dict[str, Any]:
    """勾稽验了多少、平了多少。数的是交回物里写的，不另算。"""
    checks = [c for c in content.get("checks") or [] if isinstance(c, dict)]
    periods = max((int(c.get("periods_checked") or 0) for c in checks), default=0)
    unbalanced = [c for c in checks if c.get("unbalanced")]
    breaks = content.get("continuity") or []
    unexplained = content.get("unexplained_breaks") or []
    said = f"{len(checks)} 条规则 × {periods} 期，"
    said += "全部平" if not unbalanced else f"有 {len(unbalanced)} 条不平"
    if breaks:
        said += f"；跨期断点 {len(breaks)} 处，"
        said += "都有解释" if not unexplained else f"{len(unexplained)} 处没有解释"
    return {
        "rules": len(checks),
        "periods": periods,
        "unbalanced": len(unbalanced),
        "breaks": len(breaks),
        "unexplained": len(unexplained),
        "text": said,
    }


def coverage_summary(content: dict[str, Any]) -> dict[str, Any]:
    matched = content.get("matched") or []
    mismatched = content.get("mismatched") or []
    missing = content.get("not_covered") or []
    total = len(matched) + len(mismatched)
    said = f"与公司披露的关键数字对了 {total} 项，{len(matched)} 项一致"
    if mismatched:
        said += f"，{len(mismatched)} 项对不上"
    return {
        "compared": total,
        "matched": len(matched),
        "mismatched": len(mismatched),
        "not_covered": len(missing),
        "coverage": content.get("coverage") or "",
        "text": said,
    }


def block_view(
    block: DossierBlock,
    pack: ExpertPack,
    artifacts: dict[str, dict[str, Any]],
    provenance: dict[str, Any] | None,
) -> dict[str, Any]:
    source = next(
        (
            (i, s)
            for i, s in enumerate(pack.workflow)
            if s.output_artifact == block.artifact
        ),
        None,
    )
    view: dict[str, Any] = {
        "key": block.key,
        "title": block.title,
        "kind": block.kind,
        "folded": block.folded,
        "from": None
        if source is None
        else {"step": source[0] + 1, "title": source[1].title},
        "status": "not_reached",
        "note": NOT_REACHED,
        "content": None,
        "columns": [{"key": k, "title": t} for k, t in block.columns],
        "also": {},
        "summary": None,
        "artifact": None,
        "source": None,
    }
    found = artifacts.get(block.artifact)
    if found is None:
        return view
    content = found.get("content")
    view["status"] = "done"
    view["note"] = None
    # 小结的块只给小结；明细在它后面各自的表里
    view["content"] = None if block.kind == "coverage" else pick(content, block.path)
    view["also"] = {name: pick(content, path) for name, path in block.also.items()}
    view["artifact"] = {
        "name": found["name"],
        "version": found["version"],
        "digest": found["digest"],
    }
    if isinstance(content, dict):
        if block.kind == "checks":
            view["summary"] = checks_summary(content)
        elif block.kind == "coverage":
            view["summary"] = coverage_summary(content)
        # 表里每个数的出处：哪个数据集、哪个版本、数据到哪天。口径在各行自己的 basis 里
        if block.kind in ("table", "tables") and block.section == "data":
            version = content.get("data_version") or (provenance or {}).get(
                "data_version"
            )
            if provenance or version:
                view["source"] = {**(provenance or {}), "data_version": version}
    return view


def head_of(task: dict[str, Any], position: dict[str, Any] | None) -> dict[str, Any]:
    state = task["state"]
    if state == TaskState.SUCCEEDED:
        return {"kind": "done", "text": "专家做完了"}
    if state == TaskState.REJECTED:
        return {"kind": "refused", "text": "专家没有接"}
    where = f"第 {position['step']} 步" if position else "中途"
    if state in (TaskState.FAILED, TaskState.CANCELLED):
        return {"kind": "stopped", "text": f"做到{where}停下了"}
    return {"kind": "running", "text": "专家还在做"}


def dossier_view(
    pack: ExpertPack,
    sheet: dict[str, Any],
    task: dict[str, Any],
    steps: list[dict[str, Any]],
    position: dict[str, Any] | None,
    artifacts: dict[str, dict[str, Any]],
    conclusion: dict[str, Any] | None,
) -> dict[str, Any]:
    """artifacts：每个交回物名字对应它通过验收的最新一版（带内容）。"""
    data = sheet.get("data")
    provenance = (
        None
        if not data
        else {k: data.get(k) for k in ("dataset", "data_version", "as_of")}
    )
    blocks = [block_view(b, pack, artifacts, provenance) for b in layout_of(pack)]
    sections = []
    for key, title in SECTIONS:
        mine = [
            b
            for b, laid in zip(blocks, layout_of(pack), strict=True)
            if laid.section == key
        ]
        if mine:
            sections.append({"key": key, "title": title, "blocks": mine})
    text = ""
    if conclusion is not None and isinstance(conclusion.get("content"), dict):
        text = str(conclusion["content"].get("text") or "")
    return {
        "task": sheet,
        "head": head_of(task, position),
        "sections": sections,
        "how": [
            {
                k: step[k]
                for k in (
                    "index",
                    "title",
                    "summary",
                    "status",
                    "times",
                    "rejected",
                    "spent",
                )
            }
            for step in steps
        ],
        # 结论栏是用户的：只放用户自己存过的，没存过就是空的
        "conclusion": {
            "text": text,
            "kind": "user_draft",
            "saved_at": conclusion.get("created_at") if conclusion else None,
            "version": conclusion.get("version") if conclusion else None,
        },
    }


# ---------------- 导出成 Markdown ----------------
def cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, dict | list):
        value = json.dumps(value, ensure_ascii=False)
    return str(value).replace("|", "\\|").replace("\n", " ")


def table_md(rows: Any, columns: list[dict[str, str]] | None = None) -> list[str]:
    rows = [r for r in rows or [] if isinstance(r, dict)]
    if not rows:
        return ["（没有）"]
    if columns:
        # 登记过的列按登记的顺序；整列都没有值的不画
        shown = [c for c in columns if any(r.get(c["key"]) is not None for r in rows)]
    else:
        names: list[str] = []
        for row in rows:
            for name in sorted(row):
                if name not in names:
                    names.append(name)
        shown = [{"key": n, "title": n} for n in names]
    lines = [
        "| " + " | ".join(c["title"] for c in shown) + " |",
        "| " + " | ".join("---" for _ in shown) + " |",
    ]
    lines += [
        "| " + " | ".join(cell(row.get(c["key"])) for c in shown) + " |" for row in rows
    ]
    return lines


def block_md(block: dict[str, Any], *, titled: bool = True) -> list[str]:
    if block["status"] != "done":
        return [f"**{block['title']}**：{NOT_REACHED}", ""]
    content, kind = block["content"], block["kind"]
    lines = [f"**{block['title']}**", ""] if titled else []
    if block.get("summary"):
        lines += [block["summary"]["text"], ""]
        if block["summary"].get("coverage"):
            lines += [block["summary"]["coverage"], ""]
    if kind == "text":
        lines += [str(content or ""), ""]
    elif kind == "list":
        lines += [f"- {cell(item)}" for item in content or []] or ["（没有）"]
        lines.append("")
    elif kind in ("table", "checks"):
        lines += [*table_md(content, block.get("columns")), ""]
    elif kind == "tables":
        for one in content or []:
            if isinstance(one, dict):
                lines += [
                    str(one.get("title") or ""),
                    "",
                    *table_md(one.get("rows")),
                    "",
                ]
    if block.get("source"):
        source = block["source"]
        lines += [
            f"数据：{source.get('dataset') or ''} · 版本 {source.get('data_version') or ''}"
            f" · 数据到 {source.get('as_of') or ''}",
            "",
        ]
    return lines


def dossier_markdown(view: dict[str, Any]) -> str:
    sheet = view["task"]
    budget = sheet["budget"]
    lines = [
        f"# {sheet['question']}",
        "",
        f"{sheet['expert']['name']} v{sheet['expert']['version']} · "
        f"{sheet.get('state_word') or view['head']['text']} · "
        f"花了 {budget['spent']}"
        + (f" / {budget['limit']}" if budget.get("limit") else "")
        + f" {budget['currency']}（估算）",
        "",
    ]
    if view["head"]["kind"] != "done":
        lines += [f"> {view['head']['text']}", ""]
    number = "一二三四五六七八九十"
    n = 0
    for section in view["sections"]:
        lines += [f"## {number[n]}、{section['title']}", ""]
        n += 1
        for block in section["blocks"]:
            # 这一节只有一块、名字又和节名一样：不重复写一遍
            alone = len(section["blocks"]) == 1 and block["title"] == section["title"]
            lines += block_md(block, titled=not alone)
    lines += [f"## {number[n]}、它是怎么做的", ""]
    n += 1
    words = {
        "accepted": "做完，验收通过",
        "not_reached": NOT_REACHED,
        "failed": "在这一步停下",
        "cancelled": "取消时停在这一步",
        "waiting": "停下来等你",
        "went_back": "验收没过，退回了前面",
    }
    for step in view["how"]:
        said = words.get(step["status"], "没有做完")
        if step["rejected"]:
            said += f"（验收没过 {step['rejected']} 次）"
        lines.append(f"{step['index']}. {step['title']}：{said}")
    lines += ["", f"## {number[n]}、我的结论", "", "（用户草稿）", ""]
    lines += [view["conclusion"]["text"] or "（还没有写）", ""]
    return "\n".join(lines)
