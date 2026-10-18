"""数值验收（SDD 0030 §3.3）：结构类验收（acceptance.judge）之后，在工作台应用层接着判。

- `computed`：path 处每项带 `compute_id` 与 `value`。这件委托的计算记录里有这个编号，记录的结果
  与 value 之差不超过 `tolerance`；规则写了 `expression` 的，记录里的算式必须就是它（比语法树）。
- `identity`：path 处每项带 `left_id` 与 `right_id`，两次计算的结果之差不超过 `tolerance`。
- `sourced`：path 处每项带 `address` 与 `value`，重新向 knowlab 取这一格，与 value 一致。
  取不到（没配读用户数据的关系、knowlab 拒了）是平台的事，不是数对不上：这一项记
  `platform`，测试结果里那条用例记出错而不是没过（审读第 3 条）。

path 处可以是一项（对象）或一串；空的一串算通过，要求非空另配 `list_min`（同 `all_equal`）。
交回物没解析成 JSON 时 judge 已把它们记成「没有验」，这里不再判。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.application.ports.workbench import KnowbotDataSource, WorkbenchStore
from app.application.workbench.acceptance import Verdict, value_at
from app.application.workbench.knowbot_compute import computations, fetch_cells
from app.application.workbench.knowbot_data import data_scope
from app.domain.workbench import compute as calc
from app.domain.workbench.errors import (
    RecordsRefused,
    UserDataUnavailable,
    WorkbenchError,
)
from app.domain.workbench.packs import NUMERIC_RULES, AcceptanceRule

SHOWN = 5  # 不通过时列出前几项


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
        if record is None:
            bad.append(f"#{i} {cid or 'no compute_id'} was not computed in this task")
        elif value is None:
            bad.append(f"#{i} {cid} has no number")
        elif not _close(
            calc.decimal_of(record.get("value")) or Decimal("NaN"),
            value,
            rule.tolerance,
        ):
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
    for r in numeric:
        items = _items(value_at(verdict.parsed, r.path))
        platform = False
        if items is None:
            bad = ["not a list"]
        elif r.kind == "computed":
            bad = _computed(r, items, records)
        elif r.kind == "identity":
            bad = _identity(r, items, records)
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
