"""确定性验收（F-ACCEPT-01 的第一层）：纯函数，输入交回物文本与规则，输出 pass/fail 与原因。不调模型。

数值类三种（computed、identity、sourced，0030 §3.3）与引文 quoted（0036）要查计算记录与用户
数据，不在这里判：judge 跳过它们，由 acceptance_numbers 在工作台应用层接着判。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.domain.workbench.packs import NUMERIC_RULES, AcceptanceRule

# F-POS-04：对具体证券的评级、目标价、买卖建议措辞，云端确定性拦截并留痕
_POSITIONING = re.compile(
    r"(强烈?(买入|卖出|增持|减持|推荐)|目标价|建议(买入|卖出|建仓|清仓|加仓|减仓)|(买入|卖出)评级|必涨|必跌|稳赚|保本收益|strong (buy|sell)|price target|buy rating|sell rating)",
    re.I,
)
# Only a complete, explicit non-advice declaration is exempt. Do not exempt a
# sentence merely because a negation occurs somewhere before a forbidden term.
_DISCLAIMER_ITEM = r"(?:买卖建议|投资建议|买入评级|卖出评级|投资评级|评级|目标价)"
_POSITIONING_DISCLAIMER = re.compile(
    r"(?:^|(?<=[。；;！!？?\n]))[ \t]*(?:[-*][ \t]+)?"
    r"(?:(?:本文|本报告|本分析|本次分析|本体检|我们|我)[ \t]*)?"
    r"(?:不提供|不给出|不给|不涉及|不作|不含)[ \t]*(?:任何[ \t]*)?"
    + _DISCLAIMER_ITEM
    + r"(?:[ \t]*(?:、|，|,|和|或|及)[ \t]*"
    + _DISCLAIMER_ITEM
    + r")*(?:[，,][ \t]*(?:结论留给用户|如何判断留给您))?[ \t]*(?=[。；;！!？?\n]|\Z)"
)


@dataclass
class Verdict:
    ok: bool
    parsed: Any = None
    failures: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)


def _json_error(candidate: str, error: json.JSONDecodeError) -> str:
    """Describe syntax errors without repairing evidence or echoing its content."""
    closing: list[str] = []
    quoted = escaped = False
    for char in candidate:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "{[":
            closing.append("}" if char == "{" else "]")
        elif char in "}]":
            if not closing or closing.pop() != char:
                break
    detail = f"invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}"
    if quoted:
        detail += "; unterminated string"
    elif closing:
        detail += f"; unclosed containers, expected closing delimiters: {''.join(reversed(closing))[:32]}"
    return (
        detail + ". Return one complete JSON object; preserve the data and citations."
    )


def _extract_json(text: str, *, exact_numbers: bool = False) -> Any:
    """容忍围栏与前后闲话；由 JSON 解码器处理字符串中的括号和转义。"""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t, parse_float=Decimal if exact_numbers else float)
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    if start < 0:
        raise ValueError("no JSON object found; return one complete JSON object")
    candidate = t[start:]
    try:
        parsed, _ = json.JSONDecoder(
            parse_float=Decimal if exact_numbers else float
        ).raw_decode(candidate)
        return parsed
    except json.JSONDecodeError as exc:
        raise ValueError(_json_error(candidate, exc)) from None


def value_at(obj: Any, path: str | None) -> Any:
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _preserve_numeric_evidence(
    parsed: dict[str, Any], text: str, rules: tuple[AcceptanceRule, ...]
) -> None:
    """Keep checked decimal operands/results lossless through JSON persistence.

    Numeric checks already accept decimal strings (the compute tool's format).
    Only replace checked numeric fields when float decoding changed their value;
    ordinary numbers, row identities and unrelated output keep their JSON types.
    Never infer a reported value from a compute record or increase tolerance.
    """
    numeric = [r for r in rules if r.kind in {"computed", "sourced"}]
    if not numeric:
        return
    exact = _extract_json(text, exact_numbers=True)
    for rule in numeric:
        rows, originals = value_at(parsed, rule.path), value_at(exact, rule.path)
        if isinstance(rows, dict):
            rows, originals = [rows], [originals]
        if not isinstance(rows, list):
            continue
        for row, original in zip(rows, originals, strict=True):
            if not isinstance(row, dict):
                continue
            for key in ("value", *rule.input_keys):
                value = original.get(key)
                if isinstance(value, Decimal) and value.is_finite():
                    decoded = row.get(key)
                    if isinstance(decoded, float) and Decimal(str(decoded)) != value:
                        row[key] = str(value)


def _text_of(value: Any) -> str:
    """要查措辞的文字：字符串原样；列表与对象取其中所有的字符串（键名不算）。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_text_of(v) for v in value.values())
    if isinstance(value, list):
        return "\n".join(_text_of(v) for v in value)
    return ""


def _positioning_hit(value: Any) -> str | None:
    """Check every text leaf; a disclaimer cannot exempt a different field."""
    if isinstance(value, str):
        hit = _POSITIONING.search(_POSITIONING_DISCLAIMER.sub("", value))
        return hit.group(0) if hit else None
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        for item in value:
            if (hit := _positioning_hit(item)) is not None:
                return hit
    return None


def _same(left: Any, right: Any) -> bool:
    # True == 1 在 Python 里成立；验收里布尔和数字不是一回事
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return float(left) == float(right)
    return type(left) is type(right) and left == right


def _all_equal(value: Any, name: str | None, expected: Any) -> tuple[bool, str]:
    """列表的每一项都有这个字段且等于期望值。空列表算通过：要求非空另用 list_min。"""
    if not isinstance(value, list):
        return False, "not a list"
    if not name:
        return False, "rule has no field"
    bad = [
        i
        for i, item in enumerate(value)
        if not isinstance(item, dict)
        or name not in item
        or not _same(item[name], expected)
    ]
    return not bad, f"items {bad[:10]} differ" if bad else f"len={len(value)}"


def timed_out(minutes: int, rules: tuple[AcceptanceRule, ...]) -> Verdict:
    """这一步到了时限没做完：没有交回物。不拿最后那句进度说明去验，各条照实记成「没有验」。"""
    message = f"这一步 {minutes} 分钟内没有做完，被停下，没有交回物"
    return Verdict(
        ok=False,
        failures=[f"timeout: {minutes} min"],
        checks=[
            {
                "rule": "in_time",
                "path": None,
                "pass": False,
                "detail": f"timed out after {minutes} min",
                "label": "在时限内交回",
                "message": message,
            },
            *(
                {
                    "rule": r.kind,
                    "path": r.path,
                    "pass": None,
                    "detail": "not checked",
                    "label": r.label,
                    "message": "",
                }
                for r in rules
            ),
        ],
    )


OUT_OF_SCOPE_CHARS = 500


def out_of_scope(text: str | None) -> dict[str, str] | None:
    """受理关口的回答（账 130）：专家说这个问题不归它，回 {reason, suggestion}；别的回答都回 None。"""
    try:
        reply = _extract_json(text or "")
    except ValueError:
        return None
    if not isinstance(reply, dict) or reply.get("out_of_scope") is not True:
        return None
    return {
        "reason": _text_of(reply.get("reason")).strip()[:OUT_OF_SCOPE_CHARS],
        "suggestion": _text_of(reply.get("suggestion")).strip()[:OUT_OF_SCOPE_CHARS],
    }


def _each_has(val: Any, rule: AcceptanceRule) -> tuple[bool, str]:
    """Check row fields, optional nonblank content and unique scalar identities."""
    if not isinstance(val, list):
        return False, "not a list"
    bad = []
    seen = set()
    for i, item in enumerate(val):
        if not isinstance(item, dict):
            bad.append(f"#{i} is not an object")
            continue
        missing = [k for k in rule.keys if k not in item]
        if missing:
            bad.append(f"#{i} misses {missing}")
        if rule.values and item.get(rule.field or "") not in rule.values:
            bad.append(f"#{i} {rule.field}={item.get(rule.field or '')!r}")
        for key in rule.non_empty_keys:
            value = item.get(key)
            if (
                value is None
                or value == []
                or value == {}
                or (isinstance(value, str) and not value.strip())
            ):
                bad.append(f"#{i} {key} is empty")
        if rule.unique_by:
            values = [item.get(key) for key in rule.unique_by]
            if any(
                not isinstance(v, str | int | float | bool)
                or (isinstance(v, str) and not v.strip())
                or (isinstance(v, float) and not math.isfinite(v))
                for v in values
            ):
                bad.append(f"#{i} has an invalid row identity")
            else:
                identity = tuple(
                    ("number", v)
                    if isinstance(v, int | float) and not isinstance(v, bool)
                    else (type(v).__name__, v)
                    for v in values
                )
                if identity in seen:
                    bad.append(f"#{i} duplicate row identity")
                seen.add(identity)
    return not bad, "; ".join(bad[:5])


def judge(text: str | None, rules: tuple[AcceptanceRule, ...]) -> Verdict:
    v = Verdict(ok=True)
    parsed: Any = None
    needs_json = any(r.kind != "max_chars" for r in rules)
    if needs_json:
        try:
            parsed = _extract_json(text or "")
            if not isinstance(parsed, dict):
                raise ValueError("top level is not an object")
            _preserve_numeric_evidence(parsed, text or "", rules)
        except ValueError as exc:
            v.ok = False
            v.failures.append(f"json_object: {exc}")
            first = next((r for r in rules if r.kind == "json_object"), None)
            v.checks.append(
                {
                    "rule": "json_object",
                    "path": None,
                    "pass": False,
                    "detail": str(exc),
                    "label": first.label if first else "",
                    "message": (first.message if first else "")
                    or "交回物不是 JSON 对象",
                }
            )
            # 格式不对，后面的各条无从验起：如实记成「没有验」，不记成通过或不通过
            v.checks += [
                {
                    "rule": r.kind,
                    "path": r.path,
                    "pass": None,
                    "detail": "not checked",
                    "label": r.label,
                    "message": "",
                }
                for r in rules
                if r is not first
            ]
            return v
    v.parsed = parsed
    for r in rules:
        if r.kind in NUMERIC_RULES:
            continue
        ok, detail = True, ""
        if r.kind == "json_object":
            ok = isinstance(parsed, dict)
        elif r.kind == "required_keys":
            missing = [k for k in r.keys if k not in parsed]
            ok, detail = not missing, f"missing {missing}" if missing else ""
        elif r.kind == "non_empty":
            val = value_at(parsed, r.path)
            ok = bool(val) and (not isinstance(val, str) or val.strip() != "")
        elif r.kind == "list_min":
            val = value_at(parsed, r.path)
            ok = isinstance(val, list) and len(val) >= r.min_items
            detail = f"len={len(val) if isinstance(val, list) else 'n/a'}"
        elif r.kind == "no_positioning_advice":
            val = value_at(parsed, r.path)
            hit = _positioning_hit(val)
            ok, detail = hit is None, hit or ""
        elif r.kind == "max_chars":
            ok = len(text or "") <= r.max_chars
        elif r.kind == "all_equal":
            ok, detail = _all_equal(value_at(parsed, r.path), r.field, r.equals)
        elif r.kind == "each_has":
            ok, detail = _each_has(value_at(parsed, r.path), r)
        elif r.kind == "list_empty":
            val = value_at(parsed, r.path)
            ok = isinstance(val, list) and not val
            detail = f"len={len(val)}" if isinstance(val, list) else "not a list"
        elif r.kind == "blank":
            val = value_at(parsed, r.path)
            ok = val is None or (isinstance(val, str) and val.strip() == "")
            detail = "" if ok else "has content"
        v.checks.append(
            {
                "rule": r.kind,
                "path": r.path,
                "pass": ok,
                "detail": detail,
                # 给用户看的两句：这条验收叫什么；不通过时怎么说
                "label": r.label,
                "message": "" if ok else r.message,
            }
        )
        if not ok:
            v.ok = False
            v.failures.append(
                f"{r.kind}{'(' + r.path + ')' if r.path else ''}: {r.message or detail}"
            )
    return v
