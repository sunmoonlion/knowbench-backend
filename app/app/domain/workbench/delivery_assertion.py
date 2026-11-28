"""Evaluate the validated JSON reads and comparisons; missing evidence fails closed."""

import ast
from decimal import Decimal, DecimalException

from app.domain.workbench.delivery_assertion_contract import (
    _ARITH,
    _COMPARE,
    parse_assertion,
)


def _number(value):
    if type(value) not in (int, float, str, Decimal) or len(str(value)) > 96:
        raise ValueError("Expected a finite number")
    n = Decimal(str(value))
    if not n.is_finite() or abs(n.adjusted()) > 150:
        raise ValueError("Number outside assertion range")
    return n


def matches_assertion(expression, document):
    def read(name, path, filters):
        key, *fields = path.split(".")
        many = key.endswith("[]")
        key = key.removesuffix("[]")
        element = document.get(key)
        if name == "pairs":
            if not isinstance(element, list) or not all(
                isinstance(v, dict) for v in element
            ):
                raise ValueError("Expected a table of coordinates")
            return Decimal(
                len(
                    {
                        (
                            _number(v.get(filters["first"])),
                            _number(v.get(filters["second"])),
                        )
                        for v in element
                    }
                )
            )
        if many:
            if not isinstance(element, list):
                raise ValueError("Expected rows")
            values = [
                r
                for r in element
                if isinstance(r, dict)
                and all(r.get(k) == v for k, v in filters.items())
            ]
            if not filters and len(values) != len(element):
                raise ValueError("Expected object rows")
        else:
            if filters:
                raise ValueError("Filters require a table")
            values = [element]
        for field in fields:
            values = [v.get(field) if isinstance(v, dict) else None for v in values]
        if name == "largest":
            return max(_number(v) for v in values)
        if name == "distinct":
            return Decimal(len({_number(v) for v in values}))
        if name == "size" and many:
            return Decimal(len(values))
        if len(values) != 1:
            raise ValueError("Expected one value")
        value = values[0]
        if name == "exists":
            return value is not None
        if name == "nonblank":
            return isinstance(value, str) and bool(value.strip())
        if name == "whole" and type(value) is int:
            return _number(value)
        if name == "nonblank_items":
            return (
                isinstance(value, list)
                and bool(value)
                and all(isinstance(v, str) and bool(v.strip()) for v in value)
            )
        if name == "size" and isinstance(value, list):
            return Decimal(len(value))
        if name == "word" and isinstance(value, str):
            return value
        if name == "num":
            return _number(value.get("value") if isinstance(value, dict) else value)
        raise ValueError("Invalid assertion operand")

    def visit(node):
        if isinstance(node, ast.Call):
            return read(
                node.func.id,
                node.args[0].value,
                {k.arg: k.value.value for k in node.keywords},
            )
        if isinstance(node, ast.Constant):
            return (
                _number(node.value) if type(node.value) in (int, float) else node.value
            )
        if isinstance(node, ast.BoolOp):
            tests = (visit(child) is True for child in node.values)
            return all(tests) if isinstance(node.op, ast.And) else any(tests)
        if isinstance(node, ast.UnaryOp):
            return visit(node.operand) is False
        if isinstance(node, ast.BinOp):
            return _number(
                _ARITH[type(node.op)](
                    _number(visit(node.left)), _number(visit(node.right))
                )
            )
        left = visit(node.left)
        for op, child in zip(node.ops, node.comparators, strict=True):
            right = visit(child)
            if type(left) is not type(right) or not _COMPARE[type(op)](left, right):
                return False
            left = right
        return True

    try:
        return visit(parse_assertion(expression)) is True
    except (ValueError, TypeError, DecimalException, OverflowError):
        return False
