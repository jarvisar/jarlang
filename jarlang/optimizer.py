"""Works out constant expressions like 2 * 3 ahead of time (used by the native compiler)."""

from __future__ import annotations

import math
from dataclasses import fields
from typing import Any

from . import ast

INT64_MIN, INT64_MAX = -(2 ** 63), 2 ** 63 - 1


def _num(node: ast.Node) -> bool:
    return isinstance(node, ast.Literal) and type(node.value) in (int, float)


def _fits(v: Any) -> bool:
    if type(v) is int:
        return INT64_MIN <= v <= INT64_MAX
    if type(v) is float:
        return math.isfinite(v)
    return type(v) is bool


def _fold_binary(node: ast.Binary) -> ast.Expr:
    a, b = node.left, node.right
    if not (_num(a) and _num(b)):
        if node.op == "+" and isinstance(a, ast.StringLit) and isinstance(b, ast.StringLit) \
                and a.is_plain and b.is_plain:
            return ast.StringLit([a.plain_value + b.plain_value], span=node.span)
        return node
    x, y = a.value, b.value  # type: ignore[union-attr]
    op = node.op
    try:
        if op == "+":
            v = x + y
        elif op == "-":
            v = x - y
        elif op == "*":
            v = x * y
        elif op == "/":
            if y == 0:
                return node
            v = x / y
        elif op == "//":
            if y == 0:
                return node
            v = x // y
        elif op == "%":
            if y == 0:
                return node
            v = x % y
        elif op == "^":
            if type(x) is int and type(y) is int:
                if not 0 <= y <= 64:
                    return node
                v = x ** y
            else:
                if x == 0 and y < 0 or x < 0 and float(y) != int(y):
                    return node
                v = float(x) ** y
        else:
            return node
    except (OverflowError, ValueError, ZeroDivisionError):
        return node
    if not _fits(v):
        return node
    return ast.Literal(v, span=node.span)


def _fold_unary(node: ast.Unary) -> ast.Expr:
    operand = node.operand
    if node.op == "-" and _num(operand):
        v = -operand.value  # type: ignore[union-attr]
        return ast.Literal(v, span=node.span) if _fits(v) else node
    if node.op == "+" and _num(operand):
        return ast.Literal(operand.value, span=node.span)  # type: ignore[union-attr]
    if node.op == "not" and isinstance(operand, ast.Literal) and type(operand.value) is bool:
        return ast.Literal(not operand.value, span=node.span)
    if node.op == "√" and _num(operand) and operand.value >= 0:  # type: ignore[union-attr]
        return ast.Literal(math.sqrt(operand.value), span=node.span)  # type: ignore[union-attr]
    if node.op == "!" and isinstance(operand, ast.Literal) and type(operand.value) is int \
            and 0 <= operand.value <= 20:
        return ast.Literal(math.factorial(operand.value), span=node.span)
    return node


def _fold(node: Any) -> Any:
    if isinstance(node, list):
        return [_fold(n) for n in node]
    if isinstance(node, tuple):
        return tuple(_fold(n) for n in node)
    if not isinstance(node, ast.Node):
        return node
    for f in fields(node):
        if f.name == "span":
            continue
        value = getattr(node, f.name)
        if isinstance(value, (ast.Node, list, tuple)):
            setattr(node, f.name, _fold(value))
    if isinstance(node, ast.Binary):
        return _fold_binary(node)
    if isinstance(node, ast.Unary):
        return _fold_unary(node)
    return node


def fold_constants(program: ast.Program) -> ast.Program:
    _fold(program)
    return program
