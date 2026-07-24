"""Node types for the abstract syntax tree (AST)."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Iterator, Union

from .source import Span

GLOBAL = -1  # depth of global variables


@dataclass(eq=False, slots=True)
class Node:
    span: Span = field(kw_only=True, repr=False)

    def children(self) -> Iterator["Node"]:
        for f in fields(self):
            if f.name == "span":
                continue
            value = getattr(self, f.name)
            yield from _iter_nodes(value)


def _iter_nodes(value: Any) -> Iterator[Node]:
    if isinstance(value, Node):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_nodes(item)


# Type annotations

@dataclass(eq=False, slots=True)
class TypeRef(Node):
    name: str
    args: list["TypeRef"] = field(default_factory=list)

    def __str__(self) -> str:
        if self.args:
            return f"{self.name}[{', '.join(map(str, self.args))}]"
        return self.name


# Expressions

@dataclass(eq=False, slots=True)
class Expr(Node):
    pass


@dataclass(eq=False, slots=True)
class Literal(Expr):
    value: Any  # int | float | bool | None


@dataclass(eq=False, slots=True)
class InterpPart(Node):
    expr: Expr
    spec: str | None = None


@dataclass(eq=False, slots=True)
class StringLit(Expr):
    parts: list[Union[str, InterpPart]]
    raw: bool = False  # single-quoted: no interpolation

    @property
    def is_plain(self) -> bool:
        return all(isinstance(p, str) for p in self.parts)

    @property
    def plain_value(self) -> str:
        return "".join(p for p in self.parts if isinstance(p, str))


@dataclass(eq=False, slots=True)
class Name(Expr):
    name: str
    depth: int = field(default=GLOBAL, compare=False)
    slot: int = field(default=-1, compare=False)


@dataclass(eq=False, slots=True)
class ListLit(Expr):
    items: list[Expr]


@dataclass(eq=False, slots=True)
class DictLit(Expr):
    entries: list[tuple[Expr, Expr]]
    # Which keys were written as bare names ({name: value}), for the formatter
    bare_keys: list[bool] = field(default_factory=list)


@dataclass(eq=False, slots=True)
class RangeExpr(Expr):
    start: Expr
    end: Expr
    inclusive: bool = True


@dataclass(eq=False, slots=True)
class Unary(Expr):
    op: str  # "-", "+", "not", "√", "!" (postfix factorial)
    operand: Expr


@dataclass(eq=False, slots=True)
class Binary(Expr):
    op: str  # "+", "-", "*", "/", "//", "%", "^"
    left: Expr
    right: Expr
    implicit: bool = False  # implicit multiplication like 2x


@dataclass(eq=False, slots=True)
class Compare(Expr):
    """A comparison, which can be chained (a < b <= c)."""

    operands: list[Expr]
    ops: list[str]  # "==", "!=", "<", "<=", ">", ">=", "in", "not in"


@dataclass(eq=False, slots=True)
class Logical(Expr):
    op: str  # "and" | "or"
    left: Expr
    right: Expr


@dataclass(eq=False, slots=True)
class Call(Expr):
    callee: Expr
    args: list[Expr]


@dataclass(eq=False, slots=True)
class MethodCall(Expr):
    """obj.name(args): calls a dict field, or name(obj, args) otherwise."""

    receiver: Expr
    method: Name
    args: list[Expr]


@dataclass(eq=False, slots=True)
class Field(Expr):
    obj: Expr
    name: str
    name_span: Span | None = field(default=None, repr=False)


@dataclass(eq=False, slots=True)
class Index(Expr):
    obj: Expr
    index: Expr


@dataclass(eq=False, slots=True)
class Slice(Expr):
    obj: Expr
    start: Expr | None
    stop: Expr | None
    step: Expr | None = None


@dataclass(eq=False, slots=True)
class Pipe(Expr):
    """x |> f runs f(x), and x |> f(a) runs f(x, a)."""

    value: Expr
    func: Expr


@dataclass(eq=False, slots=True)
class Param(Node):
    name: str
    type: TypeRef | None = None
    default: Expr | None = None
    slot: int = field(default=-1, compare=False)


@dataclass(eq=False, slots=True)
class Function(Node):
    """Used by fn declarations and anonymous functions."""

    name: str | None
    params: list[Param]
    body: "Block"
    ret_type: TypeRef | None = None
    # "block", "arrow" (fn f() => e) or "math" (f(x) = e), used by the formatter
    style: str = "block"
    # Filled in by the resolver
    nlocals: int = field(default=0, compare=False)
    local_names: list[str] = field(default_factory=list, compare=False)


@dataclass(eq=False, slots=True)
class Lambda(Expr):
    func: Function


@dataclass(eq=False, slots=True)
class IfExpr(Expr):
    branches: list[tuple[Expr, "Block"]]
    else_: "Block | None" = None


@dataclass(eq=False, slots=True)
class Comprehension(Expr):
    """[expr for x in iterable if cond]"""

    expr: Expr
    targets: list[Name]
    iterable: Expr
    cond: Expr | None = None
    nlocals: int = field(default=0, compare=False)


# Statements

@dataclass(eq=False, slots=True)
class Stmt(Node):
    pass


@dataclass(eq=False, slots=True)
class Block(Node):
    stmts: list[Stmt]


@dataclass(eq=False, slots=True)
class ExprStmt(Stmt):
    expr: Expr


@dataclass(eq=False, slots=True)
class Let(Stmt):
    target: Name
    value: Expr | None
    type: TypeRef | None = None
    const: bool = False


@dataclass(eq=False, slots=True)
class Assign(Stmt):
    target: Expr  # Name | Index | Field
    value: Expr
    op: str | None = None  # for compound assignment: "+", "-", ...


@dataclass(eq=False, slots=True)
class FnDecl(Stmt):
    target: Name
    func: Function


@dataclass(eq=False, slots=True)
class While(Stmt):
    cond: Expr
    body: Block


@dataclass(eq=False, slots=True)
class For(Stmt):
    targets: list[Name]
    iterable: Expr
    body: Block


@dataclass(eq=False, slots=True)
class Return(Stmt):
    value: Expr | None


@dataclass(eq=False, slots=True)
class Break(Stmt):
    pass


@dataclass(eq=False, slots=True)
class Continue(Stmt):
    pass


@dataclass(eq=False, slots=True)
class Throw(Stmt):
    value: Expr


@dataclass(eq=False, slots=True)
class Try(Stmt):
    body: Block
    catch_name: Name | None
    handler: Block


@dataclass(eq=False, slots=True)
class Import(Stmt):
    path: str
    alias: Name | None = None


@dataclass(eq=False, slots=True)
class Program(Node):
    stmts: list[Stmt]


# Utilities

def walk(node: Node) -> Iterator[Node]:
    """Yield the node and every node below it."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(list(current.children())))


def dump(node: Any, indent: int = 0) -> str:
    """Return the tree as a short S-expression (used by tests and jarlang ast)."""
    if isinstance(node, list):
        return "[" + " ".join(dump(n) for n in node) + "]"
    if isinstance(node, tuple):
        return "(" + " ".join(dump(n) for n in node) + ")"
    if not isinstance(node, Node):
        return repr(node)
    name = type(node).__name__
    if isinstance(node, Literal):
        v = node.value
        return "nil" if v is None else ("true" if v is True else "false" if v is False else repr(v))
    if isinstance(node, Name):
        return node.name
    if isinstance(node, StringLit):
        parts = []
        for p in node.parts:
            parts.append(repr(p) if isinstance(p, str) else "{" + dump(p.expr) + (":" + p.spec if p.spec else "") + "}")
        return "(str " + " ".join(parts) + ")"
    if isinstance(node, (Binary, Logical)):
        return f"({node.op} {dump(node.left)} {dump(node.right)})"
    if isinstance(node, Unary):
        return f"({node.op} {dump(node.operand)})"
    if isinstance(node, Compare):
        out = dump(node.operands[0])
        for op, rhs in zip(node.ops, node.operands[1:]):
            out += f" {op} {dump(rhs)}"
        return f"(cmp {out})"
    if isinstance(node, Block):
        return "{" + "; ".join(dump(s) for s in node.stmts) + "}"
    if isinstance(node, ExprStmt):
        return dump(node.expr)
    parts = []
    for f in fields(node):
        if f.name == "span" or not f.init or not f.compare or f.name in ("style", "bare_keys", "name_span"):
            continue
        value = getattr(node, f.name)
        if value is None or value is False or value == []:
            continue
        parts.append(dump(value))
    return f"({name.lower()} {' '.join(parts)})" if parts else f"({name.lower()})"
