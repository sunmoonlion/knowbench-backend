"""分身的确定性计算（SDD 0030 §3.2）：安全的算式求值、十进制。

算式只许四则运算、整数次乘方（`**`，折现系数这类要用，10-28）、括号、正负号、数字常数，
和 sum、avg、median、min、max、abs 六个函数；名字只能是这次计算的输入（一个数，或一串数）。用 Python 的语法树按白名单走一遍，不 eval，
不取属性、不下标、不调别的函数。全程十进制，不经浮点。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Any

from app.domain.workbench.errors import ComputeRefused as ComputeError

MAX_EXPRESSION = 500
MAX_NODES = 200
MAX_DEPTH = 20
MAX_INPUTS = 500  # 一次计算最多这么多个输入
MAX_ROUND = 10
MAX_POWER = 100  # 乘方的指数只许整数，绝对值不超过它
PRECISION = 34
NAME = re.compile(r"^[a-z_][a-z0-9_]{0,31}$")
COMPUTE_ID = re.compile(r"^c[1-9][0-9]{0,9}$")
ZERO_DIVISOR = "denominator is zero"


def decimal_of(value: Any) -> Decimal | None:
    """交回物或记录里的数：数字或十进制字符串。布尔、非有限数、别的都不算。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = (
            Decimal(str(value).strip())
            if isinstance(value, int | float | str)
            else None
        )
    except InvalidOperation:
        return None
    return number if number is not None and number.is_finite() else None


def text_of(number: Decimal) -> str:
    """十进制字符串：不用科学记数，去掉末尾的 0。"""
    text = format(number.normalize(), "f")
    return "0" if text in {"0", "-0"} else text


Value = Decimal | list[Decimal]


def _flat(args: Sequence[Value]) -> list[Decimal]:
    out: list[Decimal] = []
    for a in args:
        out.extend(a if isinstance(a, list) else [a])
    if not out:
        raise ComputeError("a function got no numbers")
    return out


def _median(xs: list[Decimal]) -> Decimal:
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


FUNCTIONS: dict[str, Callable[[list[Decimal]], Decimal]] = {
    "sum": lambda xs: sum(xs, Decimal(0)),
    "avg": lambda xs: sum(xs, Decimal(0)) / len(xs),
    "median": _median,
    "min": min,
    "max": max,
}


def _check_shape(tree: ast.AST) -> None:
    nodes = 0

    def walk(node: ast.AST, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise ComputeError("the expression is too long or too deep")
        for child in ast.iter_child_nodes(node):
            walk(child, depth + 1)

    walk(tree, 0)


def evaluate(expression: str, values: dict[str, Value]) -> Decimal:
    """按白名单求值；结果必须是一个数。"""
    if not isinstance(expression, str) or not expression.strip():
        raise ComputeError("the expression is empty")
    if len(expression) > MAX_EXPRESSION:
        raise ComputeError("the expression is too long")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        raise ComputeError("the expression is not valid") from None
    _check_shape(tree)

    def ev(node: ast.AST) -> Value:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, int | float):
                raise ComputeError("only numbers can be written in the expression")
            return (
                Decimal(repr(node.value))
                if isinstance(node.value, float)
                else Decimal(node.value)
            )
        if isinstance(node, ast.Name):
            if node.id not in values:
                raise ComputeError(
                    f"unknown name {node.id!r}: every name must be an input"
                )
            return values[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
            operand = _scalar(ev(node.operand))
            return -operand if isinstance(node.op, ast.USub) else operand
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            return _power(_scalar(ev(node.left)), _scalar(ev(node.right)))
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, ast.Add | ast.Sub | ast.Mult | ast.Div
        ):
            left, right = _scalar(ev(node.left)), _scalar(ev(node.right))
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if right == 0:
                raise ComputeError(ZERO_DIVISOR)
            return left / right
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and not node.keywords
        ):
            name = node.func.id
            if name != "abs" and name not in FUNCTIONS:
                raise ComputeError(f"function {name!r} is not allowed")
            args = [ev(a) for a in node.args]
            if name == "abs":
                if len(args) != 1:
                    raise ComputeError("abs takes one number")
                return abs(_scalar(args[0]))
            return FUNCTIONS[name](_flat(args))
        raise ComputeError(
            "only + - * / ** ( ), numbers, inputs and sum avg median min max abs are allowed"
        )

    with localcontext() as ctx:
        ctx.prec = PRECISION
        try:
            return _scalar(ev(tree))
        except (InvalidOperation, ArithmeticError) as exc:
            raise ComputeError("the expression cannot be computed") from exc


def _power(base: Decimal, exponent: Decimal) -> Decimal:
    """整数次乘方：开方这类非整数指数不收（结果要能逐位确定）；0 的负数次方同除以零。"""
    if exponent != exponent.to_integral_value():
        raise ComputeError("a power takes a whole-number exponent")
    if abs(exponent) > MAX_POWER:
        raise ComputeError(f"a power's exponent is at most {MAX_POWER}")
    if base == 0 and exponent < 0:
        raise ComputeError(ZERO_DIVISOR)
    return base ** int(exponent)


def _scalar(value: Value) -> Decimal:
    if isinstance(value, list):
        raise ComputeError("a list of numbers needs sum, avg, median, min or max")
    return value


def rounded(value: Decimal, places: int | None) -> Decimal:
    if places is None:
        return value
    with localcontext() as ctx:
        ctx.prec = PRECISION
        try:
            return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        except InvalidOperation as exc:
            raise ComputeError("the result is too large to round") from exc


def same_expression(left: str, right: str) -> bool:
    """两个算式写法相同（空格、多余括号不算）：比语法树。"""
    try:
        return ast.dump(ast.parse(left.strip(), mode="eval")) == ast.dump(
            ast.parse(right.strip(), mode="eval")
        )
    except (SyntaxError, AttributeError):
        return False
