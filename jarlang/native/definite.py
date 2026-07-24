"""Finds variables that might be read before they are assigned."""

from __future__ import annotations

from .. import ast
from ..ast import GLOBAL
from .typecheck import MAIN_SCOPE, TypeChecker

Key = tuple
Assigned = frozenset


# Native variables start as 0, so only the reads found here get an "assigned" check
class DefiniteAssignment:
    def __init__(self, checker: TypeChecker) -> None:
        self.checker = checker
        self.user_globals = set(checker.globals)
        self.functions = set(checker.fn_decls)
        self.checked: set[int] = set()  # ids of Name nodes whose read must be checked
        self.flagged: set[Key] = set()  # variables that need an "assigned" flag
        self.scopes: list[int] = []

    def run(self) -> tuple[set[int], set[Key]]:
        safe_globals = self._main(self.checker.program)
        seen: set[int] = set()
        for inst in self.checker.live_instances:
            func = inst.func
            if func is None or id(func) in seen:
                continue
            seen.add(id(func))
            self.scopes = [id(func)]
            start = {(id(func), p.slot) for p in func.params} | {("global", g) for g in safe_globals}
            self._block(func.body.stmts, frozenset(start))
        return self.checked, self.flagged

    # Helpers
    def key(self, name: ast.Name) -> Key | None:
        if name.depth == GLOBAL:
            return ("global", name.name) if name.name in self.user_globals else None
        index = len(self.scopes) - 1 - name.depth
        if index < 0:
            return None
        return (self.scopes[index], name.slot)

    def _read(self, name: ast.Name, assigned: Assigned) -> None:
        k = self.key(name)
        if k is not None and k not in assigned:
            self.checked.add(id(name))
            self.flagged.add(k)

    def _with(self, assigned: Assigned, *names: ast.Name) -> Assigned:
        keys = {self.key(n) for n in names}
        keys.discard(None)
        return assigned | keys

    def _calls_function(self, node: ast.Node) -> bool:
        for n in ast.walk(node):
            if isinstance(n, ast.Call) and isinstance(n.callee, ast.Name) and n.callee.name in self.functions:
                return True
        return False

    # Main program
    # Globals assigned before the first function call are safe to read inside functions
    def _main(self, program: ast.Program) -> set[str]:
        self.scopes = [MAIN_SCOPE]
        assigned: Assigned | None = frozenset()
        safe: set[str] | None = None
        for stmt in program.stmts:
            if isinstance(stmt, ast.FnDecl):
                continue
            if safe is None and self._calls_function(stmt):
                safe = {k[1] for k in assigned if k[0] == "global"}  # type: ignore[union-attr]
            assigned = self._stmt(stmt, assigned)  # type: ignore[arg-type]
            if assigned is None:
                break
        return safe if safe is not None else set(self.user_globals)

    # Statements
    def _block(self, stmts: list[ast.Stmt], assigned: Assigned) -> Assigned | None:
        for stmt in stmts:
            result = self._stmt(stmt, assigned)
            if result is None:
                return None
            assigned = result
        return assigned

    def _stmt(self, node: ast.Stmt, a: Assigned) -> Assigned | None:
        if isinstance(node, ast.ExprStmt):
            if isinstance(node.expr, ast.IfExpr):
                return self._if(node.expr, a)
            self._expr(node.expr, a)
            return a
        if isinstance(node, ast.Let):
            if node.value is not None:
                self._expr(node.value, a)
            return self._with(a, node.target)
        if isinstance(node, ast.Assign):
            return self._assign(node, a)
        if isinstance(node, ast.While):
            self._expr(node.cond, a)
            self._block(node.body.stmts, a)
            return a
        if isinstance(node, ast.For):
            self._expr(node.iterable, a)
            self._block(node.body.stmts, self._with(a, *node.targets))
            return a
        if isinstance(node, ast.Return):
            if node.value is not None:
                self._expr(node.value, a)
            return None
        if isinstance(node, (ast.Break, ast.Continue)):
            return None
        return a

    def _assign(self, node: ast.Assign, a: Assigned) -> Assigned:
        target = node.target
        if isinstance(target, ast.ListLit):
            values = node.value.items if isinstance(node.value, ast.ListLit) else [node.value]
            for v in values:
                self._expr(v, a)
            names = []
            for t in target.items:
                if isinstance(t, ast.Name):
                    names.append(t)
                else:
                    self._expr(t, a)
            return self._with(a, *names)
        if isinstance(target, ast.Name):
            if node.op is not None:
                self._read(target, a)
            self._expr(node.value, a)
            return self._with(a, target)
        # Index assignment, e.g. xs[i] = v
        self._expr(node.value, a)
        self._expr(target, a)
        return a

    def _if(self, node: ast.IfExpr, a: Assigned) -> Assigned | None:
        results: list[Assigned | None] = []
        for cond, block in node.branches:
            self._expr(cond, a)
            results.append(self._block(block.stmts, a))
        results.append(self._block(node.else_.stmts, a) if node.else_ is not None else a)
        live = [r for r in results if r is not None]
        if not live:
            return None
        out = live[0]
        for r in live[1:]:
            out = out & r
        return out

    # Expressions
    def _expr(self, node: ast.Node | None, a: Assigned) -> None:
        if node is None:
            return
        if isinstance(node, ast.Name):
            self._read(node, a)
            return
        if isinstance(node, ast.IfExpr):
            self._if(node, a)
            return
        if isinstance(node, ast.Comprehension):
            self._expr(node.iterable, a)
            self.scopes.append(id(node))
            inner = self._with(a, *node.targets)
            self._expr(node.cond, inner)
            self._expr(node.expr, inner)
            self.scopes.pop()
            return
        if isinstance(node, ast.StringLit):
            for part in node.parts:
                if isinstance(part, ast.InterpPart):
                    self._expr(part.expr, a)
            return
        for child in node.children():
            if isinstance(child, ast.Block):
                self._block(child.stmts, a)
            elif isinstance(child, ast.InterpPart):
                self._expr(child.expr, a)
            else:
                self._expr(child, a)


def analyze(checker: TypeChecker) -> tuple[set[int], set[Key]]:
    return DefiniteAssignment(checker).run()
