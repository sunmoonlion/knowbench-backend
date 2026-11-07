"""调用方模型与提供方 schema 逐字段比对（SDD 0052 第一节）。

锁测试原来只拿一份样例过提供方 schema，测不出长度、必填、正则这些约束走没走偏。这里把
两边化成同一种形状逐项比：字段集合、必填、类型（含可空）、长度、正则、枚举、条数、数值范围、
多余字段。不比标题、说明、默认值。
"""

from __future__ import annotations

from typing import Any

_KEPT = (
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
)


def _resolve(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if ref is None:
        return node
    return defs[ref.rsplit("/", 1)[-1]]


def shape(node: dict[str, Any], defs: dict[str, Any], depth: int = 0) -> Any:
    assert depth < 20, "schema nesting too deep"
    node = _resolve(node, defs)
    if "anyOf" in node:
        options = [_resolve(o, defs) for o in node["anyOf"]]
        nullable = any(o.get("type") == "null" for o in options)
        rest = [o for o in options if o.get("type") != "null"]
        inner = (
            shape(rest[0], defs, depth + 1)
            if len(rest) == 1
            else sorted(repr(shape(o, defs, depth + 1)) for o in rest)
        )
        return {"nullable": nullable, "of": inner}
    out: dict[str, Any] = {"type": node.get("type")}
    for key in _KEPT:
        if key in node:
            out[key] = node[key]
    if "enum" in node:
        out["enum"] = sorted(node["enum"])
    if "const" in node:
        out["enum"] = [node["const"]]
    if node.get("type") == "array" and "items" in node:
        out["items"] = shape(node["items"], defs, depth + 1)
    if node.get("type") == "object" or "properties" in node:
        props = node.get("properties", {})
        out["properties"] = {
            k: shape(v, defs, depth + 1) for k, v in sorted(props.items())
        }
        out["required"] = sorted(node.get("required", []))
        extra = node.get("additionalProperties", True)
        out["additionalProperties"] = (
            shape(extra, defs, depth + 1) if isinstance(extra, dict) else extra
        )
    return out


def differences(a: Any, b: Any, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            where = f"{path}.{key}" if path else key
            if key not in a:
                out.append(f"{where}: only provider has {b[key]!r}")
            elif key not in b:
                out.append(f"{where}: only consumer has {a[key]!r}")
            else:
                out += differences(a[key], b[key], where)
        return out
    return [] if a == b else [f"{path}: consumer {a!r} vs provider {b!r}"]


def model_vs_provider(model: Any, provider: dict[str, Any], name: str) -> list[str]:
    """调用方 pydantic 模型 vs 提供方 schema 里 `$defs[name]`（或整份 schema）。"""
    mine = model.model_json_schema()
    theirs_defs = provider.get("$defs", {})
    theirs = theirs_defs[name] if name in theirs_defs else provider
    return differences(shape(mine, mine.get("$defs", {})), shape(theirs, theirs_defs))
