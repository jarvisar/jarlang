"""Works out the types of all variables, functions, and expressions for the native compiler."""

from __future__ import annotations

import re
from typing import Iterable

from .. import ast
from ..ast import GLOBAL
from ..builtins import BUILTINS, CONSTANTS
from ..errors import Diagnostic, MultipleErrors, arity_message
from ..source import Span
from .types import (BOOL, FLOAT, INT, NEVER, NIL, NUMERIC, STR, ListT, NumericConflict, Type, TypeConflict,
                    join, mangle_type, show_type)

NUMERIC_HELP = ("native code keeps int and float separate: write 0.0 instead of 0 "
                "(or use float(x)) so the value is a float from the start")

MAIN_SCOPE = 0

MATH1 = ("sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh", "exp", "ln",
         "log2", "log10", "cbrt", "deg", "rad")

INT64_MIN, INT64_MAX = -(2 ** 63), 2 ** 63 - 1

# Builtins the native compiler supports
NATIVE_BUILTINS = frozenset({
    "print", "write", "sqrt", *MATH1, "atan2", "hypot", "log", "abs", "floor", "ceil", "trunc", "sign",
    "round", "min", "max", "clamp", "gcd", "lcm", "isqrt", "is_prime", "factorial", "pow", "int", "float",
    "str", "len", "upper", "lower", "trim", "push", "pop", "sum", "product", "contains", "clock", "assert",
    "exit", "range", "split", "join", "replace", "starts_with", "ends_with", "ord", "chr", "chars", "input",
    "sort", "reverse", "index_of",
})

SUPPORTED_HINT = "the native compiler supports a subset of JarLang; run this program with the interpreter instead"

# Format specs supported by the C runtime
FORMAT_SPEC = re.compile(r"^(?:(?P<fill>.)?(?P<align>[<>^=]))?(?P<sign>[-+ ])?(?P<zero>0)?(?P<width>\d+)?"
                         r"(?P<grouping>[,_])?(?:\.(?P<precision>\d+))?(?P<type>[bcdeEfFgGoxX%s]?)$")


class FnInstance:
    """One version of a function for a set of argument types (or the main program)."""

    def __init__(self, func: ast.Function | None, name: str, argtypes: tuple, key: tuple) -> None:
        self.func = func
        self.name = name
        self.argtypes = argtypes
        self.key = key
        self.ret: Type = None
        self.vars: dict[tuple[int, int], Type] = {}
        self.types: dict[int, Type] = {}
        self.nodes: dict[int, ast.Node] = {}
        self.calls: dict[int, "FnInstance"] = {}
        self.defaults: dict[int, list[ast.Expr]] = {}
        self.globals_used: set[str] = set()
        if func is None:
            self.asm_name = "jl_main"
        else:
            sig = "".join(mangle_type(t) for t in argtypes) or "v"
            self.asm_name = f"jlf_{_mangle_name(name)}__{sig}"

    @property
    def signature(self) -> str:
        params = ", ".join(f"{p.name}: {show_type(t)}" for p, t in zip(self.func.params, self.argtypes)) \
            if self.func else ""
        return f"{self.name}({params}) -> {show_type(self.ret)}"

    def type_of(self, node: ast.Node) -> Type:
        return self.types.get(id(node))


def _mangle_name(name: str) -> str:
    out = []
    for ch in name:
        if ch.isascii() and (ch.isalnum() or ch == "_"):
            out.append(ch)
        else:
            out.append(f"u{ord(ch):x}_")
    return "".join(out)


class TypeChecker:
    MAX_PASSES = 64

    def __init__(self, program: ast.Program, user_globals: Iterable[str]) -> None:
        self.program = program
        self.user_globals = set(user_globals)
        self.fn_decls: dict[str, ast.FnDecl] = {}
        self.globals: dict[str, Type] = {}
        self.instances: dict[tuple, FnInstance] = {}
        self.main = FnInstance(None, "<main>", (), ("main",))
        self.errors: list[Diagnostic] = []
        self.changed = False
        self.reached: set[tuple] = set()
        self.cur: FnInstance = self.main
        self.scopes: list[int] = [MAIN_SCOPE]
        self.ret_acc: Type = None
        self.loop_depth = 0
        self.final_pass = False

    # Passes and errors
    # Repeat until no types change, since recursive functions start with an unknown return type
    def check(self) -> None:
        self._collect_functions()
        if self.errors:
            raise MultipleErrors(self.errors)
        for _ in range(self.MAX_PASSES):
            self._pass()
            if not self.changed:
                break
        else:
            raise MultipleErrors([Diagnostic("error", "native type inference did not converge",
                                             self.program.span, helps=[SUPPORTED_HINT])])
        # Run one more pass that reports errors, now that the types are stable
        self.final_pass = True
        self._pass()
        self._validate()
        if self.errors:
            raise MultipleErrors(_dedupe(self.errors))

    def _pass(self) -> None:
        self.changed = False
        self.errors = []
        self.reached = set()
        self._check_instance(self.main)
        done: set[tuple] = set()
        while True:
            pending = [inst for key, inst in list(self.instances.items())
                       if key in self.reached and key not in done]
            if not pending:
                break
            for inst in pending:
                done.add(inst.key)
                self._check_instance(inst)

    @property
    def live_instances(self) -> list[FnInstance]:
        return [inst for key, inst in self.instances.items() if key in self.reached]

    def _validate(self) -> None:
        for inst in [self.main, *self.live_instances]:
            if inst.func is not None and inst.ret is None:
                self.error(f"cannot infer what `{inst.name}` returns", inst.func.span,
                           helps=["does it recurse forever? every path needs a base case that returns a value"])
        for name, t in self.globals.items():
            if t is None:
                self.error(f"cannot infer the type of global `{name}`", None)
        if self.errors:
            return
        for inst in [self.main, *self.live_instances]:
            for key, t in inst.types.items():
                if t is None:
                    node = inst.nodes.get(key)
                    self.error("cannot infer the type of this expression", node.span if node else None,
                               helps=["give the variable a starting value of the right type"])
                    return

    def error(self, message: str, span: Span | None, label: str = "", helps: Iterable[str] = ()) -> Type:
        if self.final_pass:
            self.errors.append(Diagnostic("error", message, span, label, helps=list(helps)))
        return None

    def unsupported(self, what: str, span: Span) -> Type:
        # "dicts are", but "comparing lists is"
        plural = what.endswith("s") and not what.split()[0].endswith("ing")
        return self.error(f"{what} {'are' if plural else 'is'} not supported by the native compiler",
                          span, helps=[SUPPORTED_HINT])

    # Functions
    def _collect_functions(self) -> None:
        for stmt in self.program.stmts:
            if isinstance(stmt, ast.FnDecl):
                name = stmt.target.name
                if name in self.fn_decls:
                    self.errors.append(Diagnostic("error", f"function `{name}` is defined twice", stmt.target.span,
                                                  helps=["the native compiler needs each function defined once"]))
                self.fn_decls[name] = stmt
        for node in ast.walk(self.program):
            if isinstance(node, ast.Assign):
                targets = node.target.items if isinstance(node.target, ast.ListLit) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and t.depth == GLOBAL and t.name in self.fn_decls:
                        self.errors.append(Diagnostic("error", f"cannot reassign function `{t.name}` in native code",
                                                      t.span, helps=[SUPPORTED_HINT]))

    def _check_instance(self, inst: FnInstance) -> None:
        saved = (self.cur, self.scopes, self.ret_acc, self.loop_depth)
        self.cur = inst
        inst.types = {}
        inst.nodes = {}
        inst.calls = {}
        inst.defaults = {}
        self.ret_acc = None
        self.loop_depth = 0
        if inst.func is None:
            self.scopes = [MAIN_SCOPE]
            self.block(self.program.stmts, need_value=False, top=True)
        else:
            func = inst.func
            self.scopes = [id(func)]
            for p, t in zip(func.params, inst.argtypes):
                self._join_var((id(func), p.slot), t, p.span, p.name)
            body_type = self.block(func.body.stmts, need_value=True)
            try:
                ret = join(self.ret_acc, body_type)
            except TypeConflict as exc:
                helps = ["native functions must always return the same type"]
                helps.append(NUMERIC_HELP if isinstance(exc, NumericConflict) else "add an explicit `return` on every path")
                ret = self.error(f"`{inst.name}` returns {show_type(self.ret_acc)} on some paths but "
                                 f"{show_type(body_type)} on others", func.span, helps=helps)
            if ret == NEVER:
                ret = NIL
            if func.ret_type is not None:
                declared = self.annotation(func.ret_type)
                if declared is not None:
                    if ret is not None and not _assignable(ret, declared):
                        self.error(f"`{inst.name}` is declared to return {show_type(declared)} but returns "
                                   f"{show_type(ret)}", func.ret_type.span)
                    ret = declared
            self._update_ret(inst, ret)
        self.cur, self.scopes, self.ret_acc, self.loop_depth = saved

    def _update_ret(self, inst: FnInstance, ret: Type) -> None:
        try:
            new = join(inst.ret, ret)
        except TypeConflict:
            self.error(f"`{inst.name}` returns values of different types ({show_type(inst.ret)} and "
                       f"{show_type(ret)})", inst.func.span if inst.func else None)
            return
        if new != inst.ret:
            inst.ret = new
            self.changed = True

    def annotation(self, ref: ast.TypeRef) -> Type:
        simple = {"int": INT, "float": FLOAT, "bool": BOOL, "str": STR, "nil": NIL}
        if ref.name in simple and not ref.args:
            return simple[ref.name]
        if ref.name == "list":
            if len(ref.args) == 1:
                inner = self.annotation(ref.args[0])
                return ListT(inner) if inner is not None else None
            return ListT(None)
        return self.error(f"type `{ref}` is not supported by the native compiler", ref.span,
                          helps=["native types: int, float, bool, str, list[T]"])

    # Variables
    def local_key(self, node: ast.Name) -> tuple[int, int] | None:
        index = len(self.scopes) - 1 - node.depth
        if index < 0 or self.scopes[index] == MAIN_SCOPE:
            self.error(f"`{node.name}` is captured from an enclosing function", node.span,
                       helps=["closures are not supported by the native compiler", SUPPORTED_HINT])
            return None
        return (self.scopes[index], node.slot)

    def _join_var(self, key: tuple[int, int], t: Type, span: Span, name: str) -> None:
        old = self.cur.vars.get(key)
        try:
            new = join(old, t)
        except TypeConflict as exc:
            helps = [NUMERIC_HELP] if isinstance(exc, NumericConflict) else ["native variables must keep one type"]
            self.error(f"`{name}` holds {show_type(old)} but is assigned {show_type(t)}", span, "type changes here",
                       helps=helps)
            return
        if new != old:
            self.cur.vars[key] = new
            self.changed = True

    def assign_name(self, node: ast.Name, t: Type, span: Span) -> None:
        if t == NEVER:
            return
        if node.depth == GLOBAL:
            name = node.name
            if name in self.fn_decls:
                self.error(f"cannot assign to function `{name}`", node.span)
                return
            if self.cur is not self.main:
                self.cur.globals_used.add(name)
            old = self.globals.get(name)
            try:
                new = join(old, t)
            except TypeConflict as exc:
                helps = [NUMERIC_HELP] if isinstance(exc, NumericConflict) else ["native variables must keep one type"]
                self.error(f"global `{name}` holds {show_type(old)} but is assigned {show_type(t)}", span,
                           "type changes here", helps=helps)
                return
            if new != old:
                self.globals[name] = new
                self.changed = True
            return
        key = self.local_key(node)
        if key is not None:
            self._join_var(key, t, span, node.name)

    def var_type(self, node: ast.Name) -> Type:
        if node.depth == GLOBAL:
            name = node.name
            if name in self.fn_decls:
                return self.error(f"function `{name}` cannot be used as a value in native code", node.span,
                                  helps=["call it directly instead", SUPPORTED_HINT])
            if name == "argv" and name not in self.globals:
                return self.unsupported("`argv`", node.span)
            if name in self.user_globals:
                if self.cur is not self.main:
                    self.cur.globals_used.add(name)
                return self.globals.get(name)
            if name in CONSTANTS:
                return FLOAT
            if name in BUILTINS:
                return self.error(f"builtin `{name}` cannot be used as a value in native code", node.span,
                                  helps=["call it directly instead", SUPPORTED_HINT])
            return self.error(f"undefined variable `{name}`", node.span)
        key = self.local_key(node)
        if key is None:
            return None
        return self.cur.vars.get(key)

    # Statements
    def block(self, stmts: list[ast.Stmt], need_value: bool, top: bool = False) -> Type:
        result: Type = NIL
        for i, stmt in enumerate(stmts):
            is_last = i == len(stmts) - 1
            result = self.stmt(stmt, need_value and is_last, top)
        return result if stmts else NIL

    def stmt(self, node: ast.Stmt, need_value: bool, top: bool = False) -> Type:
        if isinstance(node, ast.ExprStmt):
            if isinstance(node.expr, ast.IfExpr):
                return self.if_expr(node.expr, need_value)
            return self.expr(node.expr)
        if isinstance(node, ast.Let):
            if node.value is None:
                t: Type = NIL
            else:
                t = self.expr(node.value)
            if node.type is not None:
                declared = self.annotation(node.type)
                if declared is not None:
                    if t is not None and not _assignable(t, declared):
                        self.error(f"`{node.target.name}` is declared as {show_type(declared)} but assigned "
                                   f"{show_type(t)}", node.span)
                    t = declared
            self.assign_name(node.target, t, node.span)
            return NIL
        if isinstance(node, ast.Assign):
            self.assign(node)
            return NIL
        if isinstance(node, ast.FnDecl):
            if not top:
                self.unsupported("nested functions", node.target.span)
            return NIL
        if isinstance(node, ast.While):
            self.condition(node.cond)
            self.loop_depth += 1
            self.block(node.body.stmts, need_value=False)
            self.loop_depth -= 1
            return NIL
        if isinstance(node, ast.For):
            if len(node.targets) != 1:
                self.unsupported("multiple loop variables", node.targets[1].span)
                return NIL
            elem = self.iter_elem(node.iterable)
            self.assign_name(node.targets[0], elem, node.targets[0].span)
            self.loop_depth += 1
            self.block(node.body.stmts, need_value=False)
            self.loop_depth -= 1
            return NIL
        if isinstance(node, ast.Return):
            t = self.expr(node.value) if node.value is not None else NIL
            try:
                self.ret_acc = join(self.ret_acc, t)
            except TypeConflict as exc:
                helps = ["native functions must always return the same type"]
                if isinstance(exc, NumericConflict):
                    helps.append(NUMERIC_HELP)
                self.error(f"this function returns both {show_type(self.ret_acc)} and {show_type(t)}",
                           node.span, helps=helps)
            return NEVER
        if isinstance(node, (ast.Break, ast.Continue)):
            return NEVER
        if isinstance(node, ast.Throw):
            self.unsupported("`throw`", node.span)
            return NEVER
        if isinstance(node, ast.Try):
            self.unsupported("`try`/`catch` blocks", node.span)
            return NIL
        if isinstance(node, ast.Import):
            self.unsupported("imports", node.span)
            return NIL
        self.error(f"unsupported statement {type(node).__name__}", node.span)
        return NIL

    def assign(self, node: ast.Assign) -> None:
        target = node.target
        if isinstance(target, ast.ListLit):
            if isinstance(node.value, ast.ListLit) and len(node.value.items) == len(target.items):
                types = [self.expr(v) for v in node.value.items]
                for t_node, t in zip(target.items, types):
                    if isinstance(t_node, ast.Name):
                        self.assign_name(t_node, t, node.span)
                    elif isinstance(t_node, ast.Index):
                        self.assign_index(t_node, t, node.span)
                    else:
                        self.unsupported("assigning to fields", t_node.span)
                return
            self.unsupported("unpacking a list into several variables", node.span)
            return
        value_t = self.expr(node.value)
        if node.op is not None:
            current = self.expr(target) if not isinstance(target, ast.Name) else self.var_type(target)
            if isinstance(target, ast.Name):
                self.cur.types[id(target)] = current
            value_t = self.binary_type(node.op, current, value_t, node.span, node.value)
            self.cur.types[id(node)] = value_t
        if isinstance(target, ast.Name):
            self.assign_name(target, value_t, node.span)
        elif isinstance(target, ast.Index):
            self.assign_index(target, value_t, node.span)
        else:
            self.unsupported("fields and dicts", target.span)

    def assign_index(self, target: ast.Index, value_t: Type, span: Span) -> None:
        obj_t = self.expr(target.obj)
        idx_t = self.expr(target.index)
        if obj_t is None:
            return
        if isinstance(obj_t, ListT):
            if idx_t is not None and idx_t != INT:
                self.error(f"list index must be int, found {show_type(idx_t)}", target.index.span)
            self.refine_list(target.obj, obj_t, value_t, span)
        elif obj_t == STR:
            self.error("strings are immutable", target.span)
        else:
            self.unsupported(f"indexing {show_type(obj_t)}", target.span)

    def refine_list(self, obj: ast.Expr, list_t: ListT, elem_t: Type, span: Span) -> None:
        """Update the list type when a value of type elem_t is stored in it."""
        if elem_t is None:
            return
        try:
            new = join(list_t, ListT(elem_t))
        except TypeConflict:
            self.error(f"cannot store {show_type(elem_t)} in a {show_type(list_t)}", span,
                       helps=["native lists hold a single element type"])
            return
        if new == list_t:
            if list_t.elem == INT and elem_t == FLOAT:
                self.error("cannot store float in a list[int]", span,
                           helps=["start the list with a float (e.g. [0.0]) so it is a list[float]"])
            return
        if isinstance(obj, ast.Name):
            self.assign_name(obj, new, span)
        elif list_t.elem is not None:
            self.error(f"cannot store {show_type(elem_t)} in a {show_type(list_t)}", span)

    def iter_elem(self, iterable: ast.Expr) -> Type:
        if isinstance(iterable, ast.RangeExpr):
            for part in (iterable.start, iterable.end):
                t = self.expr(part)
                if t is not None and t != INT:
                    self.error(f"range bounds must be int, found {show_type(t)}", part.span)
            self.cur.types[id(iterable)] = NIL
            return INT
        if isinstance(iterable, ast.Call) and isinstance(iterable.callee, ast.Name) and \
                iterable.callee.name == "range" and iterable.callee.depth == GLOBAL and \
                "range" not in self.user_globals and "range" not in self.fn_decls:
            if not 1 <= len(iterable.args) <= 3:
                self.error("range() takes 1 to 3 arguments", iterable.span)
            for a in iterable.args:
                t = self.expr(a)
                if t is not None and t != INT:
                    self.error(f"range() arguments must be int, found {show_type(t)}", a.span)
            self.cur.types[id(iterable)] = NIL
            return INT
        t = self.expr(iterable)
        if t is None:
            return None
        if isinstance(t, ListT):
            return t.elem
        if t == STR:
            return STR
        return self.error(f"cannot iterate over {show_type(t)}", iterable.span)

    def condition(self, node: ast.Expr) -> None:
        t = self.expr(node)
        if t is not None:
            self._truthy(t, node.span)

    # Expressions
    def expr(self, node: ast.Expr) -> Type:
        method = getattr(self, "e_" + type(node).__name__, None)
        if method is None:
            t = self.unsupported(_describe(node), node.span)
        else:
            t = method(node)
        self.cur.types[id(node)] = t
        self.cur.nodes[id(node)] = node
        return t

    def e_Literal(self, node: ast.Literal) -> Type:
        v = node.value
        if v is None:
            return NIL
        if isinstance(v, bool):
            return BOOL
        if isinstance(v, int):
            if not INT64_MIN <= v <= INT64_MAX:
                return self.error("integer literal is too large for native code (64-bit)", node.span,
                                  helps=["use a float (e.g. 1e20) or the interpreter's big integers"])
            return INT
        return FLOAT

    def e_StringLit(self, node: ast.StringLit) -> Type:
        if any(isinstance(part, str) and chr(0) in part for part in node.parts):
            # Native strings end at a zero byte
            self.unsupported("a zero character in a string", node.span)
        for part in node.parts:
            if isinstance(part, ast.InterpPart):
                t = self.expr(part.expr)
                if part.spec is not None:
                    m = FORMAT_SPEC.match(part.spec)
                    if m is None:
                        self.error(f"format spec {part.spec!r} is not supported by the native compiler", part.span,
                                   helps=["supported: [[fill]align][sign][0][width][,][.precision][type]"])
                    elif t is not None and t not in (INT, FLOAT, STR, BOOL):
                        self.unsupported(f"format specs on {show_type(t)}", part.span)
                    elif t is not None and not _spec_fits(t, m):
                        # Same message as the interpreter, which fails at run time
                        self.error(f"invalid format spec {part.spec!r} for {show_type(t)}", part.span)
                elif t is not None:
                    self._printable(t, part.expr.span)
        return STR

    def _printable(self, t: Type, span: Span) -> None:
        if t in (INT, FLOAT, BOOL, STR, NIL):
            return
        if isinstance(t, ListT):
            if t.elem is not None:
                self._printable(t.elem, span)
            return
        self.error(f"cannot print {show_type(t)}", span)

    def e_Name(self, node: ast.Name) -> Type:
        return self.var_type(node)

    def e_ListLit(self, node: ast.ListLit) -> Type:
        elem: Type = None
        for item in node.items:
            t = self.expr(item)
            try:
                elem = join(elem, t)
            except TypeConflict as exc:
                helps = ["native lists hold a single element type"]
                if isinstance(exc, NumericConflict):
                    helps.append("write every number as a float, e.g. [1.0, 2.5]")
                return self.error(f"list mixes {show_type(elem)} and {show_type(t)}", item.span, helps=helps)
        if elem == NIL:
            return self.error("lists of nil are not supported natively", node.span)
        return ListT(elem)

    def e_Unary(self, node: ast.Unary) -> Type:
        t = self.expr(node.operand)
        if t is None:
            return BOOL if node.op == "not" else (FLOAT if node.op == "√" else None)
        if node.op == "not":
            self._truthy(t, node.operand.span)
            return BOOL
        if t not in NUMERIC:
            return self.error(f"cannot apply `{node.op}` to {show_type(t)}", node.span)
        if node.op == "√":
            return FLOAT
        return t

    def _truthy(self, t: Type, span: Span) -> None:
        if t in (BOOL, INT, FLOAT, STR, NIL) or isinstance(t, ListT):
            return
        self.error(f"cannot test {show_type(t)} for truth", span)

    def e_Binary(self, node: ast.Binary) -> Type:
        lt = self.expr(node.left)
        rt = self.expr(node.right)
        return self.binary_type(node.op, lt, rt, node.span, node.right)

    def binary_type(self, op: str, lt: Type, rt: Type, span: Span, right: ast.Expr | None = None) -> Type:
        if op == "/":
            if lt is not None and lt not in NUMERIC or rt is not None and rt not in NUMERIC:
                return self._bad_operands(op, lt, rt, span)
            return FLOAT
        if lt is None or rt is None:
            return None
        if lt in NUMERIC and rt in NUMERIC:
            if op == "^" and lt == INT and rt == INT and right is not None and _negative_literal(right):
                return FLOAT
            return INT if lt == INT and rt == INT else FLOAT
        if op == "+" and lt == STR and rt == STR:
            return STR
        if op == "+" and isinstance(lt, ListT) and isinstance(rt, ListT):
            try:
                return join(lt, rt)
            except TypeConflict:
                return self._bad_operands(op, lt, rt, span)
        if op == "*" and ((lt == STR and rt == INT) or (lt == INT and rt == STR)):
            return STR
        if op == "*" and isinstance(lt, ListT) and rt == INT:
            return lt
        if op == "*" and lt == INT and isinstance(rt, ListT):
            return rt
        return self._bad_operands(op, lt, rt, span)

    def _bad_operands(self, op: str, lt: Type, rt: Type, span: Span) -> Type:
        helps = []
        if op == "+" and STR in (lt, rt):
            helps.append('use interpolation to build text, e.g. "total: {x}"')
        return self.error(f"cannot apply `{op}` to {show_type(lt)} and {show_type(rt)}", span, helps=helps)

    def e_Compare(self, node: ast.Compare) -> Type:
        types = [self.expr(o) for o in node.operands]
        for op, a, b in zip(node.ops, types, types[1:]):
            if a is None or b is None:
                continue
            if op in ("in", "not in"):
                self.membership("`in`", b, a, node.span)
                continue
            if a in NUMERIC and b in NUMERIC:
                continue
            if a == b and (a == STR or a in (BOOL, NIL) and op in ("==", "!=")):
                continue
            if isinstance(a, ListT) and isinstance(b, ListT) or \
                    op in ("==", "!=") and (isinstance(a, ListT) or isinstance(b, ListT)):
                self.unsupported("comparing lists", node.span)
                continue
            if op in ("==", "!="):
                continue  # different types are never equal
            self.error(f"cannot compare {show_type(a)} and {show_type(b)} with `{op}`", node.span)
        return BOOL

    def e_Logical(self, node: ast.Logical) -> Type:
        lt = self.expr(node.left)
        rt = self.expr(node.right)
        for t, sub in ((lt, node.left), (rt, node.right)):
            if t is not None and t != BOOL:
                self.error(f"`{node.op}` needs bool operands in native code, found {show_type(t)}", sub.span,
                           helps=["compare explicitly, e.g. `x != 0`", SUPPORTED_HINT])
        return BOOL

    def e_IfExpr(self, node: ast.IfExpr) -> Type:
        return self.if_expr(node, need_value=True)

    def if_expr(self, node: ast.IfExpr, need_value: bool) -> Type:
        result: Type = None
        types = []
        for cond, blk in node.branches:
            self.condition(cond)
            types.append(self.block(blk.stmts, need_value))
        if node.else_ is not None:
            types.append(self.block(node.else_.stmts, need_value))
        elif need_value:
            types.append(NIL)
        if not need_value:
            return NIL
        for t in types:
            try:
                result = join(result, t)
            except TypeConflict as exc:
                helps = ["native code needs every branch to produce the same type"]
                if isinstance(exc, NumericConflict):
                    helps.append(NUMERIC_HELP)
                return self.error(f"the branches of this `if` produce different types ({show_type(result)} "
                                  f"and {show_type(t)})", node.span, helps=helps)
        return NEVER if result is None and types and all(t == NEVER for t in types) else result

    def e_Index(self, node: ast.Index) -> Type:
        ot = self.expr(node.obj)
        it = self.expr(node.index)
        if it is not None and it != INT:
            self.error(f"index must be int, found {show_type(it)}", node.index.span)
        if ot is None:
            return None
        if isinstance(ot, ListT):
            if ot.elem is None:
                return self.error("cannot infer the element type of this list", node.obj.span,
                                  helps=["push at least one value, or give it a type: let xs: list[int] = []"])
            return ot.elem
        if ot == STR:
            return STR
        return self.error(f"cannot index into {show_type(ot)}", node.span)

    def e_Comprehension(self, node: ast.Comprehension) -> Type:
        if len(node.targets) != 1:
            return self.unsupported("multiple loop variables", node.targets[1].span)
        elem = self.iter_elem(node.iterable)
        self.scopes.append(id(node))
        self.assign_name(node.targets[0], elem, node.targets[0].span)
        if node.cond is not None:
            self.condition(node.cond)
        t = self.expr(node.expr)
        self.scopes.pop()
        if t is None:
            return None
        return ListT(t)

    def e_Call(self, node: ast.Call) -> Type:
        callee = node.callee
        if not isinstance(callee, ast.Name):
            for a in node.args:
                self.expr(a)
            return self.unsupported("calling computed functions", node.span)
        name = callee.name
        if callee.depth == GLOBAL and name in self.fn_decls:
            return self.user_call(node, self.fn_decls[name])
        if callee.depth == GLOBAL and name in BUILTINS and name not in self.user_globals:
            return self.builtin_call(node, name)
        for a in node.args:
            self.expr(a)
        return self.error(f"cannot call `{name}` in native code", callee.span,
                          helps=["only top-level functions and builtins can be called natively", SUPPORTED_HINT])

    def user_call(self, node: ast.Call, decl: ast.FnDecl) -> Type:
        func = decl.func
        params = func.params
        argtypes = [self.expr(a) for a in node.args]
        required = sum(1 for p in params if p.default is None)
        if not required <= len(argtypes) <= len(params):
            return self.error(arity_message(func.name, required, len(params), len(argtypes)), node.span)
        defaults = []
        for p in params[len(argtypes):]:
            if not _is_literal(p.default):
                return self.error(f"default value of `{p.name}` must be a literal in native code", p.span)
            defaults.append(p.default)
            argtypes.append(self.expr(p.default))
        final: list[Type] = []
        for p, t, arg_node in zip(params, argtypes, [*node.args, *defaults]):
            if p.type is not None:
                declared = self.annotation(p.type)
                if declared is not None and t is not None and not _assignable(t, declared):
                    self.error(f"`{func.name}` expects `{p.name}` to be {show_type(declared)}, "
                               f"but got {show_type(t)}", arg_node.span)
                t = declared if declared is not None else t
            final.append(t)
        if defaults:
            self.cur.defaults[id(node)] = defaults
        if any(t is None for t in final):
            return None
        if any(t == NIL for t in final):
            return self.error("cannot pass nil to a native function", node.span)
        key = (id(func), tuple(final))
        inst = self.instances.get(key)
        if inst is None:
            inst = FnInstance(func, decl.target.name, tuple(final), key)
            self.instances[key] = inst
            self.changed = True
        self.reached.add(key)
        self.cur.calls[id(node)] = inst
        return inst.ret

    # Builtins
    def builtin_call(self, node: ast.Call, name: str) -> Type:
        args = node.args
        ts = [self.expr(a) for a in args]
        n = len(ts)

        def arity(lo: int, hi: int | None) -> bool:
            if n < lo or (hi is not None and n > hi):
                self.error(arity_message(name, lo, hi, n), node.span, helps=["the native version of this builtin "
                                                                             "takes fewer arguments"])
                return False
            return True

        def numeric(i: int) -> bool:
            t = ts[i]
            if t is not None and t not in NUMERIC:
                self.error(f"`{name}` expects a number, found {show_type(t)}", args[i].span)
                return False
            return True

        def integer(i: int) -> bool:
            t = ts[i]
            if t is not None and t != INT:
                self.error(f"`{name}` expects an int, found {show_type(t)}", args[i].span)
                return False
            return True

        if name in ("print", "write"):
            for t, a in zip(ts, args):
                if t is not None:
                    self._printable(t, a.span)
            return NIL
        if name in MATH1 or name == "sqrt":
            if arity(1, 1):
                numeric(0)
            return FLOAT
        if name in ("atan2", "hypot"):
            if arity(2, 2):
                numeric(0), numeric(1)
            return FLOAT
        if name == "log":
            if arity(1, 2):
                for i in range(n):
                    numeric(i)
            return FLOAT
        if name in ("abs",):
            if arity(1, 1) and numeric(0):
                return ts[0]
            return None
        if name in ("floor", "ceil", "trunc", "sign"):
            if arity(1, 1):
                numeric(0)
            return INT
        if name == "round":
            if arity(1, 2):
                numeric(0)
                if n == 2:
                    integer(1)
                    return FLOAT if ts[0] == FLOAT else (INT if ts[0] == INT else None)
            return INT
        if name in ("min", "max"):
            if n == 1:
                t = ts[0]
                if t is None:
                    return None
                if isinstance(t, ListT) and t.elem in NUMERIC:
                    return t.elem
                return self.error(f"`{name}` of a single argument needs a list of numbers", args[0].span)
            if n < 2:
                return self.error(f"`{name}` needs at least 1 argument", node.span)
            result: Type = None
            for i in range(n):
                if not numeric(i):
                    return None
                try:
                    result = join(result, ts[i])
                except TypeConflict:
                    return self.error(f"`{name}` mixes int and float arguments", node.span,
                                      helps=["the interpreter returns whichever argument wins, keeping its type; "
                                             "convert explicitly with float(x) for native code"])
            return result
        if name == "clamp":
            if arity(3, 3) and numeric(0) and numeric(1) and numeric(2):
                result = None
                for t in ts:
                    try:
                        result = join(result, t)
                    except TypeConflict:
                        return self.error("`clamp` mixes int and float arguments", node.span,
                                          helps=["convert explicitly with float(x) for native code"])
                return result
            return None
        if name in ("gcd", "lcm"):
            if arity(2, 2):
                integer(0), integer(1)
            return INT
        if name == "isqrt":
            if arity(1, 1):
                integer(0)
            return INT
        if name == "is_prime":
            if arity(1, 1):
                integer(0)
            return BOOL
        if name == "factorial":
            if arity(1, 1) and numeric(0):
                return ts[0]
            return None
        if name == "pow":
            if arity(2, 2) and numeric(0) and numeric(1):
                return self.binary_type("^", ts[0], ts[1], node.span, args[1])
            return None
        if name == "int":
            if arity(1, 1) and ts[0] is not None and ts[0] not in (INT, FLOAT, BOOL, STR):
                self.error(f"cannot convert {show_type(ts[0])} to int", args[0].span)
            return INT
        if name == "float":
            if arity(1, 1) and ts[0] is not None and ts[0] not in (INT, FLOAT, BOOL, STR):
                self.error(f"cannot convert {show_type(ts[0])} to float", args[0].span)
            return FLOAT
        if name == "str":
            if arity(1, 1) and ts[0] is not None:
                self._printable(ts[0], args[0].span)
            return STR
        if name == "len":
            if arity(1, 1) and ts[0] is not None and ts[0] != STR and not isinstance(ts[0], ListT):
                self.error(f"len() of {show_type(ts[0])} is undefined", args[0].span)
            return INT
        if name in ("upper", "lower", "trim"):
            if arity(1, 1) and ts[0] is not None and ts[0] != STR:
                self.error(f"`{name}` expects a string, found {show_type(ts[0])}", args[0].span)
            return STR
        def string(i: int) -> bool:
            if ts[i] is not None and ts[i] != STR:
                self.error(f"`{name}` expects a string, found {show_type(ts[i])}", args[i].span)
                return False
            return True

        if name in ("split", "chars"):
            if arity(1, 2 if name == "split" else 1):
                for i in range(n):
                    string(i)
            return ListT(STR)
        if name == "join":
            if arity(1, 2):
                lst, sep = (ts[0], ts[1] if n == 2 else STR)
                if lst == STR and isinstance(sep, ListT):  # join(", ", xs) also works
                    lst, sep = sep, lst
                if lst is not None and not isinstance(lst, ListT):
                    self.error(f"`join` needs a list, found {show_type(lst)}", args[0].span)
                elif isinstance(lst, ListT) and lst.elem is not None:
                    self._printable(lst.elem, args[0].span)
                if sep is not None and sep != STR:
                    self.error("`join` separator must be a string", node.span)
            return STR
        if name == "replace":
            if arity(3, 3):
                for i in range(3):
                    string(i)
            return STR
        if name in ("starts_with", "ends_with"):
            if arity(2, 2):
                string(0), string(1)
            return BOOL
        if name == "ord":
            if arity(1, 1):
                string(0)
            return INT
        if name == "chr":
            if arity(1, 1):
                integer(0)
            return STR
        if name == "input":
            if arity(0, 1) and n == 1:
                string(0)
            return STR
        if name in ("sort", "reverse"):
            if not arity(1, 1):
                if n == 2 and name == "sort":
                    return self.unsupported("sort() with a key function", node.span)
                return None
            t = ts[0]
            if t is None:
                return None
            if name == "reverse" and t == STR:
                return STR
            if not isinstance(t, ListT):
                return self.error(f"`{name}` needs a list, found {show_type(t)}", args[0].span)
            if name == "sort" and t.elem not in (None, INT, FLOAT, STR, BOOL):
                return self.unsupported(f"sorting a {show_type(t)}", node.span)
            return t
        if name == "index_of":
            if arity(2, 2) and ts[0] is not None and ts[1] is not None:
                coll, item = ts
                if coll == STR:
                    string(1)
                elif isinstance(coll, ListT):
                    if coll.elem is not None and not _comparable(item, coll.elem):
                        self.error(f"cannot look for {show_type(item)} in {show_type(coll)}", node.span)
                    elif coll.elem not in (None, INT, FLOAT, STR, BOOL):
                        self.unsupported(f"index_of on a {show_type(coll)}", node.span)
                else:
                    self.error(f"`index_of` needs a list or string, found {show_type(coll)}", args[0].span)
            return INT
        if name == "push":
            if n < 1:
                return self.error("`push` needs a list", node.span)
            lt = ts[0]
            if lt is None:
                return None
            if not isinstance(lt, ListT):
                return self.error(f"`push` needs a list, found {show_type(lt)}", args[0].span)
            for t, a in zip(ts[1:], args[1:]):
                if t is None:
                    continue
                self.refine_list(args[0], lt, t, a.span)
                if lt.elem is None:
                    lt = ListT(t)
            return lt if lt.elem is not None else ListT(None)
        if name == "pop":
            if arity(1, 1):
                lt = ts[0]
                if lt is None:
                    return None
                if not isinstance(lt, ListT):
                    return self.error(f"`pop` needs a list, found {show_type(lt)}", args[0].span)
                if lt.elem is None:
                    return self.error("cannot infer the element type of this list", args[0].span)
                return lt.elem
            return None
        if name in ("sum", "product"):
            if arity(1, 1):
                lt = ts[0]
                if lt is None:
                    return None
                if isinstance(lt, ListT) and lt.elem in NUMERIC:
                    return lt.elem
                if isinstance(lt, ListT) and lt.elem is None:
                    return INT
                return self.error(f"`{name}` needs a list of numbers, found {show_type(lt)}", args[0].span)
            return None
        if name == "contains":
            if arity(2, 2) and ts[0] is not None and ts[1] is not None:
                self.membership("`contains`", ts[0], ts[1], node.span)
            return BOOL
        if name == "clock":
            arity(0, 0)
            return FLOAT
        if name == "assert":
            if arity(1, 2):
                self._truthy(ts[0], args[0].span) if ts[0] is not None else None
                if n == 2 and ts[1] is not None and ts[1] != STR:
                    self.error("assert message must be a string", args[1].span)
            return NIL
        if name == "exit":
            if arity(0, 1) and n == 1:
                integer(0)
            return NIL
        return self.error(f"builtin `{name}` is not supported by the native compiler", node.callee.span,
                          helps=[SUPPORTED_HINT])

    def membership(self, what: str, coll: Type, item: Type, span: Span) -> None:
        """Check `item in coll` and contains(coll, item)."""
        if isinstance(coll, ListT):
            if coll.elem is not None and not _comparable(item, coll.elem):
                self.error(f"cannot look for {show_type(item)} in {show_type(coll)}", span)
            elif isinstance(coll.elem, ListT):
                self.unsupported("comparing lists", span)
        elif coll == STR:
            if item != STR:
                self.error(f"{what} on a string needs a string, found {show_type(item)}", span)
        else:
            self.error(f"{what} needs a list or string, found {show_type(coll)}", span)

    # Unsupported expressions
    def e_DictLit(self, node: ast.DictLit) -> Type:
        return self.unsupported("dicts", node.span)

    def e_Lambda(self, node: ast.Lambda) -> Type:
        return self.unsupported("anonymous functions", node.span)

    def e_RangeExpr(self, node: ast.RangeExpr) -> Type:
        return self.error("ranges can only be used in `for` loops and comprehensions in native code",
                          node.span, helps=["build a list with [i for i in a..b]"])

    def e_Field(self, node: ast.Field) -> Type:
        return self.unsupported("fields and dicts", node.span)

    def e_Slice(self, node: ast.Slice) -> Type:
        return self.unsupported("slices", node.span)


def _spec_fits(t: Type, m: re.Match) -> bool:
    """Whether Python's format() accepts the spec for this type, like the interpreter needs."""
    kind, grouping = m.group("type"), m.group("grouping")
    if t in (STR, BOOL):  # bools are formatted as the text "true" or "false"
        return kind in ("", "s") and not m.group("sign") and not grouping and m.group("align") != "="
    if t == FLOAT or kind in ("e", "E", "f", "F", "g", "G", "%"):
        return kind in ("", "e", "E", "f", "F", "g", "G", "%")
    # Ints with an int type
    if kind == "s" or m.group("precision") is not None:
        return False
    if kind == "c":
        return not m.group("sign") and not grouping
    return not (grouping == "," and kind in ("b", "o", "x", "X"))


def _is_literal(node: ast.Expr | None) -> bool:
    if isinstance(node, (ast.Literal, ast.StringLit)):
        return True
    # -1 is a Unary node when constant folding is off (jarlang check --native, --no-opt)
    return isinstance(node, ast.Unary) and node.op == "-" and isinstance(node.operand, ast.Literal)         and type(node.operand.value) in (int, float)


def _negative_literal(node: ast.Expr) -> bool:
    if isinstance(node, ast.Literal) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value < 0
    return isinstance(node, ast.Unary) and node.op == "-" and isinstance(node.operand, ast.Literal) \
        and isinstance(node.operand.value, int) and not isinstance(node.operand.value, bool)


def _assignable(t: Type, declared: Type) -> bool:
    if t == declared or t == NEVER:
        return True
    if t == INT and declared == FLOAT:
        return True
    if isinstance(t, ListT) and isinstance(declared, ListT):
        return t.elem is None or t.elem == declared.elem
    return False


def _comparable(a: Type, b: Type) -> bool:
    if a in NUMERIC and b in NUMERIC:
        return True
    return a == b


def _describe(node: ast.Node) -> str:
    return {
        "MethodCall": "method calls",
        "Pipe": "pipelines",
    }.get(type(node).__name__, type(node).__name__)


def _dedupe(diags: list[Diagnostic]) -> list[Diagnostic]:
    seen = set()
    out = []
    for d in diags:
        key = (d.message, d.span.start if d.span else None)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def typecheck(program: ast.Program, user_globals: Iterable[str] | None = None) -> TypeChecker:
    """Infer the types of a resolved program (raises on errors)."""
    from .lower import lower
    lower(program)
    if user_globals is None:
        from ..resolver import Resolver
        r = Resolver()
        r._collect(program.stmts, r.global_scope)
        user_globals = [n for n, s in r.global_scope.symbols.items() if s.kind != "function"]
    checker = TypeChecker(program, user_globals)
    checker.check()
    return checker
