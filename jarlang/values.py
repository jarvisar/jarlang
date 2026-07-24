"""JarLang values at runtime, and helpers used by the interpreter and builtins."""

from __future__ import annotations

import math
from typing import Any, Callable

from . import ast


class Function:
    """A function defined in JarLang code."""

    __slots__ = ("name", "node", "closure", "body", "nparams", "nrequired", "nlocals",
                 "defaults", "param_checks", "ret_check", "globals", "module", "tail", "simple")

    def __init__(self, name: str, node: ast.Function, closure: list | None, body: Callable,
                 defaults: list[Callable | None], param_checks: list, ret_check, globals_: dict,
                 module: str, nrequired: int = -1, tail: list | None = None) -> None:
        self.name = name
        self.node = node
        self.closure = closure
        self.body = body
        self.nparams = len(node.params)
        self.nrequired = nrequired if nrequired >= 0 else sum(1 for p in node.params if p.default is None)
        self.nlocals = node.nlocals
        # Empty local slots added to each call frame, and whether type checks can be skipped
        self.tail = tail or []
        self.simple = not param_checks and ret_check is None
        self.defaults = defaults
        self.param_checks = param_checks
        self.ret_check = ret_check
        self.globals = globals_
        self.module = module

    @property
    def signature(self) -> str:
        parts = []
        for p in self.node.params:
            text = p.name
            if p.type is not None:
                text += f": {p.type}"
            if p.default is not None:
                text += " = …"
            parts.append(text)
        ret = f" -> {self.node.ret_type}" if self.node.ret_type is not None else ""
        return f"{self.name}({', '.join(parts)}){ret}"

    def __repr__(self) -> str:
        return f"<fn {self.signature}>"


class Builtin:
    """A function written in Python, called as impl(ctx, *args)."""

    __slots__ = ("name", "impl", "min_args", "max_args", "doc", "signature", "category")

    def __init__(self, name: str, impl: Callable, min_args: int, max_args: int | None,
                 doc: str = "", signature: str = "", category: str = "misc") -> None:
        self.name = name
        self.impl = impl
        self.min_args = min_args
        self.max_args = max_args
        self.doc = doc
        self.signature = signature or name + "(...)"
        self.category = category

    def __repr__(self) -> str:
        return f"<builtin {self.name}>"


class BuiltinError(Exception):
    """Raised by builtins. The interpreter adds the span of the call."""

    def __init__(self, message: str, *helps: str) -> None:
        super().__init__(message)
        self.helps = list(helps)


def type_name(v: Any) -> str:
    t = type(v)
    if t is int:
        return "int"
    if t is float:
        return "float"
    if t is bool:
        return "bool"
    if t is str:
        return "str"
    if v is None:
        return "nil"
    if t is list:
        return "list"
    if t is dict:
        return "dict"
    if t is range:
        return "range"
    if t is Function or t is Builtin:
        return "fn"
    return t.__name__


def format_float(x: float) -> str:
    if x != x:
        return "nan"
    if x == math.inf:
        return "inf"
    if x == -math.inf:
        return "-inf"
    return repr(x)


def quote(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "{":
            out.append("\\{")
        elif ch == "}":
            out.append("\\}")
        elif ord(ch) < 32:
            out.append(f"\\u{{{ord(ch):x}}}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _is_ident(s: str) -> bool:
    from .tokens import KEYWORDS
    return s.isidentifier() and s not in KEYWORDS


def show(v: Any, nested: bool = False, _seen: set[int] | None = None) -> str:
    """Convert a value to the text print shows."""
    t = type(v)
    if t is str:
        return quote(v) if nested else v
    if t is int:
        return str(v)
    if t is float:
        return format_float(v)
    if t is bool:
        return "true" if v else "false"
    if v is None:
        return "nil"
    if t is list or t is dict:
        seen = _seen or set()
        if id(v) in seen:
            return "[...]" if t is list else "{...}"
        seen.add(id(v))
        try:
            if t is list:
                return "[" + ", ".join(show(x, True, seen) for x in v) + "]"
            items = []
            for k, val in v.items():
                key = k if (type(k) is str and _is_ident(k)) else show(k, True, seen)
                items.append(f"{key}: {show(val, True, seen)}")
            return "{" + ", ".join(items) + "}"
        finally:
            seen.discard(id(v))
    if t is range:
        if v.step == 1:
            return f"{v.start}..<{v.stop}"
        return f"range({v.start}, {v.stop}, {v.step})"
    if t is Function:
        return f"<fn {v.name}>"
    if t is Builtin:
        return f"<builtin {v.name}>"
    return str(v)


def repr_value(v: Any) -> str:
    """Like show, but strings are quoted (used by the REPL)."""
    return show(v, nested=True)


def is_number(v: Any) -> bool:
    t = type(v)
    return t is int or t is float


def is_callable(v: Any) -> bool:
    t = type(v)
    return t is Function or t is Builtin
