"""Prints the tree of nodes as text, Graphviz DOT, Mermaid, or JSON (jarlang ast)."""

from __future__ import annotations

import json
from dataclasses import fields
from typing import Any, Iterator

from . import ast
from .term import style


def label(node: ast.Node) -> str:
    kind = type(node).__name__
    if isinstance(node, ast.Literal):
        v = node.value
        text = "nil" if v is None else ("true" if v is True else "false" if v is False else repr(v))
        return f"{text}"
    if isinstance(node, ast.Name):
        return node.name
    if isinstance(node, ast.StringLit):
        if node.is_plain:
            return json.dumps(node.plain_value, ensure_ascii=False)
        return "interpolated string"
    if isinstance(node, (ast.Binary, ast.Logical)):
        return node.op + (" (implicit)" if isinstance(node, ast.Binary) and node.implicit else "")
    if isinstance(node, ast.Unary):
        return "!" + " (factorial)" if node.op == "!" else node.op
    if isinstance(node, ast.Compare):
        return " ".join(node.ops)
    if isinstance(node, ast.RangeExpr):
        return ".." if node.inclusive else "..<"
    if isinstance(node, ast.Assign):
        return f"{node.op}=" if node.op else "="
    if isinstance(node, (ast.FnDecl,)):
        return f"fn {node.target.name}"
    if isinstance(node, ast.Function):
        params = ", ".join(p.name for p in node.params)
        return f"Function({params})"
    if isinstance(node, ast.Param):
        return f"param {node.name}" + (f": {node.type}" if node.type else "")
    if isinstance(node, ast.Let):
        return f"{'const' if node.const else 'let'} {node.target.name}"
    if isinstance(node, ast.Field):
        return f".{node.name}"
    if isinstance(node, ast.MethodCall):
        return f".{node.method.name}()"
    if isinstance(node, ast.Import):
        return f"import {node.path!r}"
    if isinstance(node, ast.InterpPart):
        return "{…}" + (f":{node.spec}" if node.spec else "")
    if isinstance(node, ast.TypeRef):
        return str(node)
    return kind


_SKIP_FIELDS = {"span", "style", "bare_keys", "name_span", "depth", "slot", "nlocals",
                "local_names", "implicit", "inclusive", "const", "op", "ops", "raw"}


def children(node: ast.Node) -> Iterator[tuple[str, ast.Node]]:
    """Yield (edge name, child node) pairs."""
    if isinstance(node, ast.FnDecl):
        yield from children(node.func)
        return
    if isinstance(node, ast.Let):
        if node.type is not None:
            yield "type", node.type
        if node.value is not None:
            yield "value", node.value
        return
    if isinstance(node, ast.MethodCall):
        yield "receiver", node.receiver
        for i, a in enumerate(node.args):
            yield f"arg{i}", a
        return
    if isinstance(node, ast.Block):
        for s in node.stmts:
            yield "", s
        return
    if isinstance(node, ast.ExprStmt):
        yield from children(node.expr)
        return
    if isinstance(node, ast.StringLit):
        for p in node.parts:
            if isinstance(p, ast.InterpPart):
                yield "", p.expr
        return
    for f in fields(node):
        if f.name in _SKIP_FIELDS or f.name in ("name", "path", "method") and not isinstance(getattr(node, f.name), ast.Node):
            continue
        value = getattr(node, f.name)
        if isinstance(value, ast.Node):
            yield f.name, value
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, ast.Node):
                    yield f"{f.name}[{i}]" if len(value) > 1 else f.name, item
                elif isinstance(item, tuple):
                    for j, sub in enumerate(item):
                        if isinstance(sub, ast.Node):
                            yield f"{f.name}[{i}].{j}", sub


def _display(node: ast.Node) -> ast.Node:
    return node.expr if isinstance(node, ast.ExprStmt) else node


def _kind(node: ast.Node) -> str:
    node = _display(node)
    return type(node).__name__


def render_tree(node: ast.Node, color: bool = False) -> str:
    lines: list[str] = []

    def s(text: str, spec: str) -> str:
        return style(text, spec, color)

    def head(n: ast.Node) -> str:
        n = _display(n)
        kind = type(n).__name__
        lab = label(n)
        if lab == kind:
            return s(kind, "bold bright_blue")
        return f"{s(kind, 'bold bright_blue')} {s(lab, 'bright_yellow')}"

    def walk(n: ast.Node, prefix: str, edge: str, last: bool, root: bool) -> None:
        connector = "" if root else ("└─ " if last else "├─ ")
        edge_text = s(edge + ": ", "gray") if edge and not edge.startswith(("stmts", "items")) else ""
        lines.append(prefix + connector + edge_text + head(n))
        kids = list(children(_display(n)))
        child_prefix = prefix + ("" if root else ("   " if last else "│  "))
        for i, (e, child) in enumerate(kids):
            walk(child, child_prefix, e, i == len(kids) - 1, False)

    walk(node, "", "", True, True)
    return "\n".join(lines)


def to_dot(node: ast.Node) -> str:
    out = ["digraph AST {", '  node [shape=box, style="rounded,filled", fillcolor="#f3eefe", fontname="Helvetica"];']
    counter = [0]

    def visit(n: ast.Node) -> str:
        n = _display(n)
        counter[0] += 1
        nid = f"n{counter[0]}"
        kind = type(n).__name__
        lab = label(n)
        text = kind if lab == kind else f"{kind}\\n{lab}"
        out.append(f'  {nid} [label="{_dot_escape(text)}"];')
        for edge, child in children(n):
            cid = visit(child)
            attr = f' [label="{_dot_escape(edge)}"]' if edge else ""
            out.append(f"  {nid} -> {cid}{attr};")
        return nid

    visit(node)
    out.append("}")
    return "\n".join(out)


def _dot_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\\\\n", "\\n")


def to_mermaid(node: ast.Node) -> str:
    out = ["graph TD"]
    counter = [0]

    def visit(n: ast.Node) -> str:
        n = _display(n)
        counter[0] += 1
        nid = f"n{counter[0]}"
        kind = type(n).__name__
        lab = label(n)
        text = kind if lab == kind else f"{kind}<br/>{lab}"
        out.append(f'  {nid}["{_mermaid_escape(text)}"]')
        for edge, child in children(n):
            cid = visit(child)
            out.append(f"  {nid} -->|{_mermaid_escape(edge)}| {cid}" if edge else f"  {nid} --> {cid}")
        return nid

    visit(node)
    return "\n".join(out)


def _mermaid_escape(text: str) -> str:
    return text.replace('"', "#quot;").replace("|", "#124;")


def to_json(node: Any) -> Any:
    if isinstance(node, list):
        return [to_json(n) for n in node]
    if isinstance(node, tuple):
        return [to_json(n) for n in node]
    if not isinstance(node, ast.Node):
        return node
    out: dict[str, Any] = {"type": type(node).__name__}
    span = node.span
    out["line"], out["column"] = span.source.line_col(span.start)
    for f in fields(node):
        if f.name == "span" or not f.compare:
            continue
        value = getattr(node, f.name)
        if f.name == "name_span":
            continue
        out[f.name] = to_json(value)
    return out
