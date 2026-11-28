"""Shared syntax contract for deterministic, read-only delivery comparisons."""

import ast
import operator
import re
from functools import lru_cache

_PATH = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\[\])?(?:\.[A-Za-z_][A-Za-z_0-9]*)*")
_READS = {
    "num",
    "word",
    "size",
    "exists",
    "nonblank",
    "whole",
    "nonblank_items",
    "largest",
    "distinct",
    "pairs",
}
_COMPARE = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_ARITH = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def parse_assertion(expression: str) -> ast.expr:
    if not isinstance(expression, str) or not 1 <= len(expression) <= 2048:
        raise ValueError("Invalid assertion length")
    return _parse(expression)


@lru_cache(maxsize=256)
def _parse(expression: str) -> ast.expr:
    try:
        root = ast.parse(expression, mode="eval").body
    except (SyntaxError, RecursionError, OverflowError) as exc:
        raise ValueError("Invalid assertion syntax") from exc
    if len(list(ast.walk(root))) > 192:
        raise ValueError("Assertion too complex")

    def operand_type(node):
        if isinstance(node, ast.Constant):
            if type(node.value) in (int, float):
                return "number"
            if type(node.value) in (bool, str):
                return type(node.value).__name__
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name not in _READS or len(node.args) != 1:
                raise ValueError("Unsupported delivery selector")
            arg = node.args[0]
            if (
                not isinstance(arg, ast.Constant)
                or not isinstance(arg.value, str)
                or not _PATH.fullmatch(arg.value)
            ):
                raise ValueError("Invalid delivery field path")
            if (
                len(node.keywords) > 4
                or any(
                    not k.arg
                    or not isinstance(k.value, ast.Constant)
                    or not isinstance(k.value.value, str)
                    for k in node.keywords
                )
                or len({k.arg for k in node.keywords}) != len(node.keywords)
            ):
                raise ValueError("Invalid delivery row selectors")
            if name == "pairs":
                if not arg.value.isidentifier() or {k.arg for k in node.keywords} != {
                    "first",
                    "second",
                }:
                    raise ValueError("pairs needs a table and two coordinate fields")
            elif (
                node.keywords or name in {"largest", "distinct"}
            ) and "[]" not in arg.value:
                raise ValueError("Row selectors require a table path")
            if name in {"exists", "nonblank", "nonblank_items"}:
                return "bool"
            return "str" if name == "word" else "number"
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            if all(operand_type(child) == "bool" for child in node.values):
                return "bool"
        elif isinstance(node, ast.Compare):
            types = {operand_type(child) for child in (node.left, *node.comparators)}
            if len(types) == 1 and all(
                type(op) in _COMPARE
                and ("number" in types or isinstance(op, (ast.Eq, ast.NotEq)))
                for op in node.ops
            ):
                return "bool"
        elif isinstance(node, ast.BinOp) and type(node.op) in _ARITH:
            if operand_type(node.left) == operand_type(node.right) == "number":
                return "number"
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            if operand_type(node.operand) == "bool":
                return "bool"
        raise ValueError("Unsupported delivery assertion operand")

    if operand_type(root) != "bool":
        raise ValueError("A delivery assertion must return a boolean")
    return root
