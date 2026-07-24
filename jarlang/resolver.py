"""Works out which scope each variable belongs to, and finds errors before the code runs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import ast
from .ast import GLOBAL
from .errors import Diagnostic, JarLangError, arity_message, suggest
from .source import Span

# Names that can be used in type annotations. The interpreter has a check for each one.
TYPE_NAMES = ("any", "bool", "dict", "float", "fn", "int", "list", "nil", "num", "range", "str")


@dataclass
class Symbol:
    name: str
    kind: str  # "variable" | "constant" | "function" | "parameter" | "builtin"
    span: Span | None
    scope: "Scope"
    slot: int = -1
    reads: int = 0
    writes: int = 0
    func: ast.Function | None = None  # when bound only by a `fn` declaration
    implicit: bool = False  # declared by a plain assignment (no let/fn/for)
    type: ast.TypeRef | None = None
    doc: str | None = None


# The program, each function and each comprehension have a scope (blocks don't)
# x = ... updates the nearest x if there is one, otherwise it declares a new local
@dataclass
class Scope:
    kind: str  # "global" | "function" | "comprehension"
    parent: "Scope | None"
    node: ast.Node | None = None
    symbols: dict[str, Symbol] = field(default_factory=dict)
    next_slot: int = 1  # slot 0 of every frame holds the parent frame

    def declare(self, name: str, kind: str, span: Span | None) -> Symbol:
        sym = self.symbols.get(name)
        if sym is None:
            slot = -1
            if self.kind != "global":
                slot = self.next_slot
                self.next_slot += 1
            sym = Symbol(name, kind, span, self, slot)
            self.symbols[name] = sym
        return sym


@dataclass
class Reference:
    span: Span
    symbol: Symbol


class Resolver:
    def __init__(self, builtins: dict[str, object] | None = None,
                 known_globals: Iterable[str] = (), known_consts: Iterable[str] = (),
                 filename: str | None = None,
                 module_names: Callable[[str, Span], dict[str, str]] | None = None) -> None:
        if builtins is None:
            from .builtins import BUILTINS
            builtins = BUILTINS
        self.builtins = builtins
        self.global_scope = Scope("global", None)
        for name in known_globals:
            self.global_scope.declare(name, "variable", None)
        for name in known_consts:
            self.global_scope.declare(name, "constant", None)
        self.filename = filename
        self.module_names = module_names
        self.diagnostics: list[Diagnostic] = []
        self.references: list[Reference] = []
        self.scope = self.global_scope
        self.loop_depth = 0
        self.function_depth = 0

    # Reporting
    def error(self, message: str, span: Span | None, label: str = "", helps: Iterable[str] = ()) -> None:
        self.diagnostics.append(Diagnostic("error", message, span, label, helps=list(helps)))

    def warn(self, message: str, span: Span | None, label: str = "", helps: Iterable[str] = ()) -> None:
        self.diagnostics.append(Diagnostic("warning", message, span, label, helps=list(helps)))

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "warning"]

    # Entry point
    def resolve_program(self, program: ast.Program) -> None:
        self._collect(program.stmts, self.global_scope)
        self._statements(program.stmts)

    # Declare names first, so functions can be used before they are defined
    def _collect(self, stmts: list[ast.Stmt], scope: Scope) -> None:
        """Declare every name in stmts (but not inside nested functions)."""
        for stmt in stmts:
            self._collect_node(stmt, scope)

    def _collect_node(self, node: ast.Node, scope: Scope) -> None:
        if isinstance(node, (ast.Lambda, ast.Function, ast.Comprehension)):
            return
        if isinstance(node, ast.Let):
            sym = scope.declare(node.target.name, "constant" if node.const else "variable", node.target.span)
            if node.const:
                sym.kind = "constant"
            if node.type is not None:
                sym.type = node.type
        elif isinstance(node, ast.FnDecl):
            existing = scope.symbols.get(node.target.name)
            sym = scope.declare(node.target.name, "function", node.target.span)
            if existing is None:
                sym.func = node.func
            else:
                sym.func = None  # declared more than once, so not known ahead of time
            return
        elif isinstance(node, ast.For):
            for t in node.targets:
                scope.declare(t.name, "variable", t.span)
        elif isinstance(node, ast.Try) and node.catch_name is not None:
            scope.declare(node.catch_name.name, "variable", node.catch_name.span)
        elif isinstance(node, ast.Import):
            if node.alias is not None:
                scope.declare(node.alias.name, "variable", node.alias.span)
            elif self.module_names is not None:
                self._import_names(node)
        elif isinstance(node, ast.Assign):
            for target in _assign_names(node.target):
                if self._lookup(target.name, scope) is None:
                    scope.declare(target.name, "variable", target.span).implicit = True
                else:
                    sym = self._lookup(target.name, scope)
                    if sym is not None and sym.kind == "function" and sym.scope is scope:
                        sym.func = None
        for child in node.children():
            self._collect_node(child, scope)

    def _import_names(self, node: ast.Import) -> None:
        assert self.module_names is not None
        try:
            names = self.module_names(node.path, node.span)
        except JarLangError as exc:
            self.diagnostics.append(exc.diagnostic)
            return
        except Exception as exc:  # pragma: no cover - reported properly when the import runs
            self.error(f"cannot import {node.path!r}: {exc}", node.span)
            return
        # A plain import adds the names to the globals, even when it runs inside a function
        for name, kind in names.items():
            sym = self.global_scope.declare(name, "variable", node.span)
            if kind == "constant":
                sym.kind = "constant"

    def _lookup(self, name: str, scope: Scope | None) -> Symbol | None:
        while scope is not None:
            sym = scope.symbols.get(name)
            if sym is not None:
                return sym
            scope = scope.parent
        return None

    def _find(self, name: str) -> tuple[Symbol | None, int]:
        """Return (symbol, depth) for a name, looking out from the current scope."""
        scope: Scope | None = self.scope
        depth = 0
        while scope is not None:
            sym = scope.symbols.get(name)
            if sym is not None:
                return sym, (GLOBAL if scope.kind == "global" else depth)
            scope = scope.parent
            depth += 1
        return None, GLOBAL

    # Statements
    def _statements(self, stmts: list[ast.Stmt]) -> None:
        warned = False
        for i, stmt in enumerate(stmts):
            self._stmt(stmt)
            if not warned and i + 1 < len(stmts) and \
                    isinstance(stmt, (ast.Return, ast.Break, ast.Continue, ast.Throw)):
                keyword = type(stmt).__name__.lower()
                self.warn("unreachable code", stmts[i + 1].span, "this will never run",
                          helps=[f"it comes after `{keyword}`"])
                warned = True

    def _stmt(self, node: ast.Stmt) -> None:
        method = getattr(self, "_stmt_" + type(node).__name__, None)
        if method is None:
            raise AssertionError(f"resolver: unhandled statement {type(node).__name__}")
        method(node)

    def _stmt_ExprStmt(self, node: ast.ExprStmt) -> None:
        self._expr(node.expr)

    def _stmt_Let(self, node: ast.Let) -> None:
        self._type(node.type)
        if node.value is not None:
            # When this let is what declares the name here, `let x = x + 1` reads the outer x
            # (like Rust). Otherwise the hoisted local would be read before it has a value.
            name = node.target.name
            sym = self.scope.symbols.get(name)
            shadowing = sym is not None and sym.span == node.target.span
            if shadowing:
                del self.scope.symbols[name]
            try:
                self._expr(node.value)
            finally:
                if shadowing:
                    self.scope.symbols[name] = sym
        self._bind(node.target, declaring=True)

    def _stmt_Assign(self, node: ast.Assign) -> None:
        self._expr(node.value)
        targets = node.target.items if isinstance(node.target, ast.ListLit) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                if node.op is not None:
                    self._read(target)
                self._bind(target, declaring=False)
            elif isinstance(target, ast.Index):
                self._expr(target.obj)
                self._expr(target.index)
            elif isinstance(target, ast.Field):
                self._expr(target.obj)
            else:
                self._expr(target)

    def _stmt_FnDecl(self, node: ast.FnDecl) -> None:
        self._bind(node.target, declaring=True)
        self._function(node.func)

    def _stmt_While(self, node: ast.While) -> None:
        self._expr(node.cond)
        self.loop_depth += 1
        self._block(node.body)
        self.loop_depth -= 1

    def _stmt_For(self, node: ast.For) -> None:
        self._expr(node.iterable)
        for t in node.targets:
            self._bind(t, declaring=True)
        self.loop_depth += 1
        self._block(node.body)
        self.loop_depth -= 1

    def _stmt_Return(self, node: ast.Return) -> None:
        if self.function_depth == 0:
            self.error("`return` outside of a function", node.span,
                       helps=["use `exit()` to stop the program early"])
        if node.value is not None:
            self._expr(node.value)

    def _stmt_Break(self, node: ast.Break) -> None:
        if self.loop_depth == 0:
            self.error("`break` outside of a loop", node.span)

    def _stmt_Continue(self, node: ast.Continue) -> None:
        if self.loop_depth == 0:
            self.error("`continue` outside of a loop", node.span)

    def _stmt_Throw(self, node: ast.Throw) -> None:
        self._expr(node.value)

    def _stmt_Try(self, node: ast.Try) -> None:
        self._block(node.body)
        if node.catch_name is not None:
            self._bind(node.catch_name, declaring=True)
            sym, _ = self._find(node.catch_name.name)
            if sym is not None:
                sym.reads += 1  # an unused catch variable is fine
        self._block(node.handler)

    def _stmt_Import(self, node: ast.Import) -> None:
        if node.alias is not None:
            self._bind(node.alias, declaring=True)

    def _block(self, block: ast.Block) -> None:
        self._statements(block.stmts)

    # Names
    def _bind(self, name: ast.Name, declaring: bool) -> None:
        sym, depth = self._find(name.name)
        if sym is None:  # pragma: no cover - the collection pass declares everything
            sym = self.scope.declare(name.name, "variable", name.span)
            depth = GLOBAL if self.scope.kind == "global" else 0
        if not declaring and sym.kind == "constant":
            where = f" at {sym.span.location()}" if sym.span else ""
            self.error(f"cannot assign to constant `{name.name}`", name.span, "constant",
                       helps=[f"`{name.name}` was declared with `const`{where}",
                              f"use `let {name.name}` to create a new variable instead"])
        if not declaring and sym.implicit and name.name in self.builtins \
                and _is_constant_value(self.builtins[name.name]):
            self.error(f"cannot assign to built-in constant `{name.name}`", name.span,
                       helps=[f"use `let {name.name} = ...` to shadow it"])
        sym.writes += 1
        name.depth = depth
        name.slot = sym.slot
        self.references.append(Reference(name.span, sym))

    def _read(self, name: ast.Name) -> Symbol | None:
        sym, depth = self._find(name.name)
        if sym is None:
            builtin = self.builtins.get(name.name)
            if builtin is not None:
                name.depth = GLOBAL
                name.slot = -1
                return None
            candidates = set(self.builtins)
            scope: Scope | None = self.scope
            while scope is not None:
                candidates.update(scope.symbols)
                scope = scope.parent
            guess = suggest(name.name, candidates)
            helps = [f"did you mean `{guess}`?"] if guess else []
            self.error(f"undefined variable `{name.name}`", name.span, "not found in this scope", helps)
            return None
        sym.reads += 1
        name.depth = depth
        name.slot = sym.slot
        self.references.append(Reference(name.span, sym))
        return sym

    # Functions
    def _function(self, func: ast.Function) -> None:
        for p in func.params:
            self._type(p.type)
            if p.default is not None:
                self._expr(p.default)
        self._type(func.ret_type)
        outer = self.scope
        scope = Scope("function", outer, func)
        for p in func.params:
            sym = scope.declare(p.name, "parameter", p.span)
            sym.reads += 1  # don't warn about unused parameters
            p.slot = sym.slot
            sym.type = p.type
        self._collect(func.body.stmts, scope)
        saved_loop, self.loop_depth = self.loop_depth, 0
        self.scope = scope
        self.function_depth += 1
        self._block(func.body)
        self.function_depth -= 1
        self.scope = outer
        self.loop_depth = saved_loop
        func.nlocals = scope.next_slot
        func.local_names = ["<parent>"] + [s.name for s in sorted(scope.symbols.values(), key=lambda s: s.slot)]
        for sym in scope.symbols.values():
            if sym.reads == 0 and sym.kind == "variable" and not sym.name.startswith("_"):
                self.warn(f"unused variable `{sym.name}`", sym.span, "assigned but never used",
                          helps=[f"prefix it with an underscore (`_{sym.name}`) if this is intentional"])

    def _type(self, ref: ast.TypeRef | None) -> None:
        if ref is None:
            return
        if ref.name not in TYPE_NAMES:
            guess = suggest(ref.name, TYPE_NAMES)
            helps = [f"did you mean `{guess}`?"] if guess else [f"known types: {', '.join(TYPE_NAMES)}"]
            self.error(f"unknown type `{ref.name}`", ref.span, helps=helps)
        for arg in ref.args:
            self._type(arg)

    # Expressions
    def _expr(self, node: ast.Expr | None) -> None:
        if node is None:
            return
        method = getattr(self, "_expr_" + type(node).__name__, None)
        if method is None:
            for child in node.children():
                if isinstance(child, ast.Expr):
                    self._expr(child)
                elif isinstance(child, ast.Block):
                    self._block(child)
                elif isinstance(child, ast.InterpPart):
                    self._expr(child.expr)
            return
        method(node)

    def _expr_Name(self, node: ast.Name) -> None:
        self._read(node)

    def _expr_Lambda(self, node: ast.Lambda) -> None:
        self._function(node.func)

    def _expr_IfExpr(self, node: ast.IfExpr) -> None:
        for cond, block in node.branches:
            self._expr(cond)
            self._block(block)
        if node.else_ is not None:
            self._block(node.else_)

    def _expr_StringLit(self, node: ast.StringLit) -> None:
        for part in node.parts:
            if isinstance(part, ast.InterpPart):
                self._expr(part.expr)

    def _expr_DictLit(self, node: ast.DictLit) -> None:
        for key, value in node.entries:
            self._expr(key)
            self._expr(value)

    def _expr_Comprehension(self, node: ast.Comprehension) -> None:
        self._expr(node.iterable)
        outer = self.scope
        scope = Scope("comprehension", outer, node)
        for t in node.targets:
            sym = scope.declare(t.name, "variable", t.span)
            sym.reads += 1
        self.scope = scope
        for t in node.targets:
            self._bind(t, declaring=True)
        if node.cond is not None:
            self._expr(node.cond)
        self._expr(node.expr)
        self.scope = outer
        node.nlocals = scope.next_slot

    def _expr_MethodCall(self, node: ast.MethodCall) -> None:
        self._expr(node.receiver)
        for arg in node.args:
            self._expr(arg)
        # obj.name(...) may be a dict field, so an unknown name is not an error
        sym, depth = self._find(node.method.name)
        if sym is not None:
            sym.reads += 1
            node.method.depth = depth
            node.method.slot = sym.slot
            self.references.append(Reference(node.method.span, sym))
        else:
            node.method.depth = GLOBAL
            node.method.slot = -1
            builtin = self.builtins.get(node.method.name)
            if builtin is not None:
                self._check_arity(node.method.name, builtin, len(node.args) + 1, node.span, method=True)

    def _expr_Call(self, node: ast.Call) -> None:
        self._expr(node.callee)
        for arg in node.args:
            self._expr(arg)
        if isinstance(node.callee, ast.Name):
            self._check_call(node.callee, len(node.args), node.span)

    def _expr_Pipe(self, node: ast.Pipe) -> None:
        self._expr(node.value)
        func = node.func
        if isinstance(func, ast.Call):
            self._expr(func.callee)
            for arg in func.args:
                self._expr(arg)
            if isinstance(func.callee, ast.Name):
                self._check_call(func.callee, len(func.args) + 1, func.span)
        else:
            self._expr(func)
            if isinstance(func, ast.Name):
                self._check_call(func, 1, func.span)

    def _check_call(self, callee: ast.Name, nargs: int, span: Span) -> None:
        sym, _ = self._find(callee.name)
        if sym is not None:
            if sym.func is not None and sym.kind == "function":
                self._check_user_arity(sym.func, nargs, span)
            return
        builtin = self.builtins.get(callee.name)
        if builtin is not None:
            self._check_arity(callee.name, builtin, nargs, span)

    def _check_user_arity(self, func: ast.Function, nargs: int, span: Span) -> None:
        required = sum(1 for p in func.params if p.default is None)
        total = len(func.params)
        if required <= nargs <= total:
            return
        sig = ", ".join(p.name for p in func.params)
        self.error(arity_message(func.name or "<lambda>", required, total, nargs), span,
                   helps=[f"signature: {func.name}({sig})"])

    def _check_arity(self, name: str, builtin: object, nargs: int, span: Span, method: bool = False) -> None:
        lo = getattr(builtin, "min_args", None)
        hi = getattr(builtin, "max_args", None)
        if lo is None or (lo <= nargs and (hi is None or nargs <= hi)):
            return
        helps = []
        sig = getattr(builtin, "signature", None)
        if sig:
            helps.append(f"signature: {sig}")
        # Only a warning for x.name(...), since x might be a dict with its own `name` function
        report = self.error
        if method:
            helps.append(f"`x.{name}(...)` passes `x` as the first argument")
            report = self.warn
        report(arity_message(name, lo, hi, nargs), span, helps=helps)


def _assign_names(target: ast.Expr) -> list[ast.Name]:
    if isinstance(target, ast.Name):
        return [target]
    if isinstance(target, ast.ListLit):
        return [t for t in target.items if isinstance(t, ast.Name)]
    return []


def _is_constant_value(value: object) -> bool:
    return isinstance(value, (int, float))


def resolve(program: ast.Program, **kwargs) -> Resolver:
    resolver = Resolver(**kwargs)
    resolver.resolve_program(program)
    return resolver


def module_path(path: str, importer: str | None) -> str:
    """Resolve an import path relative to the importing file."""
    if not path.endswith(".jlang"):
        path += ".jlang"
    if importer and not os.path.isabs(path):
        base = os.path.dirname(os.path.abspath(importer)) if os.path.exists(importer) else os.getcwd()
        return os.path.normpath(os.path.join(base, path))
    return os.path.abspath(path)
