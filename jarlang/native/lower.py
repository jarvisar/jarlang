"""Turns method calls and pipelines into plain function calls."""

from __future__ import annotations

from dataclasses import fields
from typing import Any

from .. import ast


def lower(node: Any) -> Any:
    if isinstance(node, list):
        return [lower(n) for n in node]
    if isinstance(node, tuple):
        return tuple(lower(n) for n in node)
    if not isinstance(node, ast.Node):
        return node
    for f in fields(node):
        if f.name == "span":
            continue
        value = getattr(node, f.name)
        if isinstance(value, (ast.Node, list, tuple)):
            setattr(node, f.name, lower(value))
    if isinstance(node, ast.MethodCall):
        return ast.Call(node.method, [node.receiver, *node.args], span=node.span)
    if isinstance(node, ast.Pipe):
        func = node.func
        if isinstance(func, ast.Call):
            return ast.Call(func.callee, [node.value, *func.args], span=node.span)
        return ast.Call(func, [node.value], span=node.span)
    return node
