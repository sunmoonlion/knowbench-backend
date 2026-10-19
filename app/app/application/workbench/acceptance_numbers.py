"""数值验收（SDD 0030 §3.3）：结构类验收（acceptance.judge）之后，在工作台应用层接着判。

- `computed`：path 处每项带 `compute_id` 与 `value`。这件委托的计算记录里有这个编号，记录的结果
  与 value 之差不超过 `tolerance`；规则写了 `expression` 的，记录里的算式必须就是它（比语法树）。
- `identity`：path 处每项带 `left_id` 与 `right_id`，两次计算的结果之差不超过 `tolerance`。
- `sourced`：path 处每项带 `address` 与 `value`，重新向 knowlab 取这一格，与 value 一致。
  取不到（没配读用户数据的关系、knowlab 拒了）是平台的事，不是数对不上：这一项记
  `platform`，测试结果里那条用例记出错而不是没过（审读第 3 条）。
- `quoted`（SDD 0036）：path 处每项带 `quotes`，一串 `{dataset_id, page, text}`；引了的都交
  knowlab 在那份 PDF 的第 page 页找原话，都要找到。写了 `field`、`equals` 的，那样的项至少引
  一条（如判「支持」的必须引到原文）；别的项可以不引。判成哪一种这条验收不管。取不到同上记
  `platform`。页码、原文的形状先按契约核，不合的是交回物的错，不送 knowlab（审读 10-19）。
- `covers`（审读 10-19）：path（加 `also` 各处）各项的 `field`，与这件委托上游交回物 `source`
  一一对应：不多、不少、不重。如第 6 步每条结论对上第 5 步的 findings。

path 处可以是一项（对象）或一串；空的一串算通过，要求非空另配 `list_min`（同 `all_equal`）。
交回物没解析成 JSON 时 judge 已把它们记成「没有验」，这里不再判。
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from app.application.ports.workbench import KnowbotDataSource, WorkbenchStore
from app.application.workbench.acceptance import Verdict, value_at
from app.application.workbench.knowbot_compute import computations, fetch_cells
from app.application.workbench.knowbot_data import data_scope, same_binding
from app.domain.workbench import compute as calc
from app.domain.workbench.errors import (
    NotFound,
    RecordsRefused,
    UserDataUnavailable,
    WorkbenchError,
)
from app.domain.workbench.packs import NUMERIC_RULES, AcceptanceRule

SHOWN = 5  # 不通过时列出前几项
QUOTES_PER_CALL = 100  # knowbot-data/v1 一次核引文最多这么多条
# knowbot-data/v1 QuoteRef 的形状（knowlab domain/knowbots/data_read.py）：不合的先挡下，记交回物的错
_DATASET_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
QUOTE_PAGE_MAX = 1_000_000
QUOTE_TEXT_MAX = 4_000


def _items(value: Any) -> list[Any] | None:
    if isinstance(value, dict):
        return [value]
    return value if isinstance(value, list) else None


def _close(left: Decimal, right: Decimal, tolerance: float) -> bool:
    return abs(left - right) <= Decimal(str(tolerance))


def _computed(
    rule: AcceptanceRule, items: list[Any], records: dict[str, dict[str, Any]]
) -> list[str]:
    bad = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            bad.append(f"#{i} is not an object")
            continue
        cid = str(item.get("compute_id") or "")
        record = records.get(cid)
        value = calc.decimal_of(item.get("value"))
        # 算出来是 0 也是数：不能拿真假判有没有（K19 第 3 步勾稽差额为 0，NaN 一比就崩）
        computed = None if record is None else calc.decimal_of(record.get("value"))
        if record is None:
            bad.append(f"#{i} {cid or 'no compute_id'} was not computed in this task")
        elif value is None:
            bad.append(f"#{i} {cid} has no number")
        elif computed is None:
            bad.append(f"#{i} {cid} computed {record.get('value')}, not one number")
        elif not _close(computed, value, rule.tolerance):
            bad.append(
                f"#{i} {cid} says {item.get('value')} but computed {record.get('value')}"
            )
        elif rule.expression and not calc.same_expression(
            str(record.get("expression") or ""), rule.expression
        ):
            bad.append(
                f"#{i} {cid} used {record.get('expression')!r}, not {rule.expression!r}"
            )
    return bad


def _identity(
    rule: AcceptanceRule, items: list[Any], records: dict[str, dict[str, Any]]
) -> list[str]:
    bad = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            bad.append(f"#{i} is not an object")
            continue
        left = records.get(str(item.get("left_id") or ""))
        right = records.get(str(item.get("right_id") or ""))
        if left is None or right is None:
            bad.append(
                f"#{i} {item.get('left_id')}/{item.get('right_id')} not computed in this task"
            )
            continue
        lv, rv = calc.decimal_of(left.get("value")), calc.decimal_of(right.get("value"))
        if lv is None or rv is None or not _close(lv, rv, rule.tolerance):
            bad.append(
                f"#{i} {item.get('left_id')}={left.get('value')} vs "
                f"{item.get('right_id')}={right.get('value')}"
            )
    return bad


def _platform(exc: WorkbenchError) -> bool:
    """取不到数是平台或配置的事（关系没配、knowlab 拒了、这一版没绑数据）；
    地址写错、格子没有、不是数，是交回物的错。"""
    return isinstance(exc, UserDataUnavailable | RecordsRefused)


async def _sourced(
    rule: AcceptanceRule,
    items: list[Any],
    source: KnowbotDataSource | None,
    repo: WorkbenchStore,
    task: dict[str, Any],
) -> tuple[list[str], bool]:
    """没对上的各项，以及是不是平台没能取数（不是数不对）。"""
    if not items:
        return [], False
    if source is None:
        return ["the user's data cannot be read here"], True
    addresses, bad = [], []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("address"), str):
            bad.append(f"#{i} has no address")
        else:
            addresses.append(item["address"])
    if bad:
        return bad, False
    try:
        purpose, bound = await data_scope(repo, str(task["owner_actor_id"]), task)
        cells = await fetch_cells(source, purpose, bound, addresses)
    except WorkbenchError as exc:
        return [f"{exc.code}: {exc.message}"], _platform(exc)
    for i, item in enumerate(items):
        value = calc.decimal_of(item.get("value"))
        actual = cells[item["address"]]
        if value is None or not _close(actual, value, rule.tolerance):
            bad.append(
                f"#{i} {item['address']} says {item.get('value')} but the data has {calc.text_of(actual)}"
            )
    return bad, False


def _quote_refs(
    rule: AcceptanceRule, items: list[Any]
) -> tuple[list[tuple[int, dict[str, Any]]], list[str]]:
    """各项引的原文（项的序号，引文），以及形状不对、该引没引的。"""
    refs: list[tuple[int, dict[str, Any]]] = []
    bad: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            bad.append(f"#{i} is not an object")
            continue
        quotes = item.get("quotes") or []
        if not isinstance(quotes, list):
            bad.append(f"#{i} quotes is not a list")
            continue
        for q in quotes:
            if (
                not isinstance(q, dict)
                or not isinstance(q.get("dataset_id"), str)
                or not isinstance(q.get("page"), int)
                or isinstance(q.get("page"), bool)
                or not isinstance(q.get("text"), str)
            ):
                bad.append(f"#{i} has a quote that is not {{dataset_id, page, text}}")
                break
            problem = _quote_problem(q)
            if problem:
                bad.append(f"#{i} page {q['page']}: {problem}")
                continue
            refs.append(
                (
                    i,
                    {
                        "dataset_id": q["dataset_id"],
                        "page": q["page"],
                        "text": q["text"],
                    },
                )
            )
        else:
            if rule.field and item.get(rule.field) == rule.equals and not quotes:
                bad.append(f"#{i} says {rule.equals} but quotes nothing")
    return refs, bad


def _quote_problem(q: dict[str, Any]) -> str | None:
    if not _DATASET_ID.fullmatch(q["dataset_id"]):
        return "bad dataset id"
    if not 1 <= q["page"] <= QUOTE_PAGE_MAX:
        return "bad page number"
    if not q["text"].strip() or len(q["text"]) > QUOTE_TEXT_MAX:
        return "quote text empty or too long"
    return None


async def _quoted(
    rule: AcceptanceRule,
    items: list[Any],
    source: KnowbotDataSource | None,
    repo: WorkbenchStore,
    task: dict[str, Any],
) -> tuple[list[str], bool]:
    """没找到的引文、该引没引的，以及是不是平台没能核（不是引错了）。"""
    refs, bad = _quote_refs(rule, items)
    if bad or not refs:
        return bad, False
    if source is None:
        return ["the user's data cannot be read here"], True
    try:
        purpose, bound = await data_scope(repo, str(task["owner_actor_id"]), task)
        if any(q["dataset_id"] not in bound for _, q in refs):
            raise NotFound("a quote cites data this version does not bind")
        found: list[dict[str, Any]] = []
        for start in range(0, len(refs), QUOTES_PER_CALL):
            found += await source.quotes(
                purpose, [q for _, q in refs[start : start + QUOTES_PER_CALL]]
            )
    except WorkbenchError as exc:
        return [f"{exc.code}: {exc.message}"], _platform(exc)
    if len(found) != len(refs):  # knowlab 回的条数不对：平台的事，记进这一项
        return ["knowlab answered a different number of quotes"], True
    for (i, q), got in zip(refs, found, strict=True):
        if not same_binding(got, bound):
            bad.append(f"#{i} page {q['page']}: not the bound version of the data")
        elif not got.get("found"):
            bad.append(f"#{i} page {q['page']}: {got.get('reason') or 'not_found'}")
    return bad, False


def _covers(
    rule: AcceptanceRule, parsed: dict[str, Any], upstream: dict[str, Any]
) -> list[str]:
    name, _, where = str(rule.source).partition(".")
    if name not in upstream:
        return [f"upstream {name} is missing"]
    source = value_at(upstream[name], where)
    if not isinstance(source, list):
        return [f"upstream {rule.source} is not a list"]
    wanted = [
        s.get(rule.source_field) if rule.source_field and isinstance(s, dict) else s
        for s in source
    ]
    got: list[Any] = []
    for path in (rule.path or "", *rule.also):
        items = _items(value_at(parsed, path))
        if items is None:
            return [f"{path} is not a list"]
        got += [i.get(rule.field or "") if isinstance(i, dict) else None for i in items]
    bad = [f"missing {w!r}" for w in wanted if w not in got]
    bad += [f"not upstream {g!r}" for g in got if g not in wanted]
    bad += [
        f"twice {g!r}" for g in {repr(x): x for x in got}.values() if got.count(g) > 1
    ]
    return bad


async def judge_numbers(
    verdict: Verdict,
    rules: tuple[AcceptanceRule, ...],
    *,
    repo: WorkbenchStore,
    task: dict[str, Any],
    source: KnowbotDataSource | None,
) -> Verdict:
    numeric = [r for r in rules if r.kind in NUMERIC_RULES]
    if not numeric or not isinstance(verdict.parsed, dict):
        return verdict
    records = await computations(repo, str(task["id"]))
    upstream = (
        {
            a["name"]: a["content"]
            for a in await repo.list_artifacts_with_content(str(task["id"]))
            if a.get("kind") == "step"
        }
        if any(r.kind == "covers" for r in numeric)
        else {}
    )
    for r in numeric:
        items = _items(value_at(verdict.parsed, r.path))
        platform = False
        if r.kind == "covers":
            bad = _covers(r, verdict.parsed, upstream)
        elif items is None:
            bad = ["not a list"]
        elif r.kind == "computed":
            bad = _computed(r, items, records)
        elif r.kind == "identity":
            bad = _identity(r, items, records)
        elif r.kind == "quoted":
            bad, platform = await _quoted(r, items, source, repo, task)
        else:
            bad, platform = await _sourced(r, items, source, repo, task)
        ok = not bad
        detail = (
            f"len={len(items)}" if ok and items is not None else "; ".join(bad[:SHOWN])
        )
        verdict.checks.append(
            {
                "rule": r.kind,
                "path": r.path,
                "pass": ok,
                "detail": detail,
                "label": r.label,
                "message": "" if ok else r.message,
                **({"platform": True} if platform else {}),
            }
        )
        if not ok:
            verdict.ok = False
            # 哪个数对不上要让专家看到（重做时据此改），规则的说明在前
            reason = f"{r.message} — {detail}" if r.message else detail
            verdict.failures.append(f"{r.kind}({r.path}): {reason}")
    return verdict
