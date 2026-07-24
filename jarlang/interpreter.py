"""Runs the tree of nodes by first converting each node into a small Python function."""

from __future__ import annotations

import operator
import os
import random
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from . import ast, ops
from .ast import GLOBAL
from .builtins import BUILTINS, ProgramExit, ThrownValue, format_value
from .errors import Diagnostic, JarLangError, JLRuntimeError, MultipleErrors, arity_message, suggest
from .parser import parse
from .resolver import Resolver, module_path
from .source import Source, Span
from .values import Builtin, BuiltinError, Function, is_callable, is_number, show, type_name

if hasattr(sys, "set_int_max_str_digits"):
    sys.set_int_max_str_digits(0)

Frame = list  # [parent_frame, slot1, slot2, ...]


class _Unset:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<unset>"


UNSET = _Unset()
MISSING = object()


# Return, break and continue are passed back as values instead of exceptions
class Ret:
    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


class _LoopSignal:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


BREAK = _LoopSignal("break")
CONTINUE = _LoopSignal("continue")
_SIGNAL_TYPES = (Ret, _LoopSignal)


class SignalException(Exception):
    """Carries a signal out of an expression, such as return inside an if expression."""

    def __init__(self, signal: Any) -> None:
        super().__init__()
        self.signal = signal


class JLThrow(JLRuntimeError):
    """A value thrown with throw or error()."""

    def __init__(self, value: Any, span: Span | None) -> None:
        super().__init__(f"uncaught error: {show(value)}", span)
        self.value = value


def _rt(exc: BuiltinError, span: Span | None) -> JLRuntimeError:
    return JLRuntimeError(str(exc), span, helps=exc.helps)


def _unset(name: str, span: Span) -> JLRuntimeError:
    return JLRuntimeError(f"variable `{name}` is used before it is assigned", span,
                          helps=[f"give `{name}` a value first"])


# Used for +, - and * when both sides are numbers
_ARITH = {"+": operator.add, "-": operator.sub, "*": operator.mul}


MAX_TRACE = 8

_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "int": lambda v: type(v) is int,
    "float": lambda v: type(v) is float or type(v) is int,
    "num": is_number,
    "bool": lambda v: type(v) is bool,
    "str": lambda v: type(v) is str,
    "list": lambda v: type(v) is list,
    "dict": lambda v: type(v) is dict,
    "range": lambda v: type(v) is range,
    "fn": is_callable,
    "nil": lambda v: v is None,
    "any": lambda v: True,
}


@dataclass
class CompiledProgram:
    run: Callable[[], Any]
    ends_with_expression: bool
    warnings: list[Diagnostic]
    program: ast.Program


# How deep JarLang function calls can go (the native compiler uses the same limit)
MAX_DEPTH = 2500


class Interpreter:
    """Runs JarLang programs. Variables are kept between runs, like in the REPL."""

    def __init__(self, stdout=None, stdin=None, *, max_depth: int = MAX_DEPTH, seed: int | None = None,
                 trace=None) -> None:
        self.stdout = stdout if stdout is not None else sys.stdout
        # If set to a stream, each statement is printed there before it runs
        self.trace = trace
        self._last_trace: tuple | None = None
        self.stdin = stdin
        self.globals: dict[str, Any] = {}
        self.consts: set[str] = set()
        self.modules: dict[str, dict[str, Any]] = {}
        self.random = random.Random(seed)
        self.depth = 0
        self.max_depth = max_depth
        self._importing: list[str] = []
        needed = 40 * max_depth + 10_000
        if sys.getrecursionlimit() < needed:
            sys.setrecursionlimit(needed)

    # Input and output used by builtins
    def write(self, text: str) -> None:
        self.stdout.write(text)

    def read_line(self, prompt: str) -> str:
        self.write(prompt)
        flush = getattr(self.stdout, "flush", None)
        if flush:
            flush()
        stream = self.stdin if self.stdin is not None else sys.stdin
        line = stream.readline()
        if not line:
            return ""
        return line.rstrip("\n").rstrip("\r")

    # Public API
    def compile(self, source: Source | str, name: str = "<input>") -> CompiledProgram:
        if isinstance(source, str):
            source = Source(source, name)
        program = parse(source)
        return self.compile_program(program, source.name)

    def compile_program(self, program: ast.Program, filename: str = "<input>",
                        globals_: dict[str, Any] | None = None) -> CompiledProgram:
        g = self.globals if globals_ is None else globals_
        consts = self.consts if globals_ is None else set()
        resolver = Resolver(known_globals=[k for k in g if k not in consts], known_consts=consts,
                            filename=filename, module_names=self._module_names(filename))
        resolver.resolve_program(program)
        if resolver.errors:
            raise MultipleErrors(resolver.errors)
        for stmt in program.stmts:
            if isinstance(stmt, ast.Let) and stmt.const:
                consts.add(stmt.target.name)
        compiler = Compiler(self, g, filename)
        body = compiler.block(program.stmts, want_value=True, hoist=True)
        root: Frame = [None]

        def run() -> Any:
            r = body(root)
            if r.__class__ in _SIGNAL_TYPES:  # pragma: no cover - resolver prevents this
                return None
            return r

        ends = bool(program.stmts) and isinstance(program.stmts[-1], ast.ExprStmt)
        return CompiledProgram(run, ends, resolver.warnings, program)

    def run(self, source: Source | str, name: str = "<input>") -> Any:
        """Run a program and return the value of its last expression (if any)."""
        return self.compile(source, name).run()

    def eval(self, text: str) -> Any:
        return self.run(text)

    # Calling functions
    def call(self, f: Any, args: list, span: Span | None = None) -> Any:
        if f.__class__ is Function:
            return self.call_function(f, args, span)
        return self.call_other(f, args, span)

    def call_function(self, f: Function, args: list, span: Span | None) -> Any:
        n = len(args)
        if n != f.nparams:
            args = self._fill_defaults(f, args, span)
        if f.param_checks:
            self._check_params(f, args, span)
        frame = [f.closure, *args, *f.tail]
        self.depth += 1
        try:
            if self.depth > self.max_depth:
                raise JLRuntimeError("maximum recursion depth exceeded", span,
                                     helps=[f"`{f.name}` recursed more than {self.max_depth} levels deep",
                                            "check the base case, or use a loop instead"])
            r = f.body(frame)
        except SignalException as s:
            r = s.signal
        except JLRuntimeError as e:
            _add_trace(e, f.name, span)
            raise
        except RecursionError:
            raise JLRuntimeError("maximum recursion depth exceeded", span) from None
        finally:
            self.depth -= 1
        if r.__class__ is Ret:
            r = r.value
        if f.ret_check is not None:
            r = self._check_return(f, r, span)
        return r

    def call_other(self, f: Any, args: list, span: Span | None) -> Any:
        if f.__class__ is Builtin:
            n = len(args)
            if n < f.min_args or (f.max_args is not None and n > f.max_args):
                raise JLRuntimeError(arity_message(f.name, f.min_args, f.max_args, n), span,
                                     helps=[f"signature: {f.signature}"])
            try:
                return f.impl(self, *args)
            except BuiltinError as e:
                raise _rt(e, span) from None
            except ThrownValue as t:
                raise JLThrow(t.value, span) from None
            except RecursionError:
                raise JLRuntimeError("maximum recursion depth exceeded", span) from None
            except (ArithmeticError, ValueError) as e:
                raise JLRuntimeError(f"{f.name}(): {e}", span) from None
        helps = []
        if is_number(f):
            helps.append("to multiply, write an explicit `*`, e.g. x*(y + 1)")
        raise JLRuntimeError(f"{type_name(f)} value is not callable", span, helps=helps)

    def _fill_defaults(self, f: Function, args: list, span: Span | None) -> list:
        n = len(args)
        if n < f.nrequired or n > f.nparams:
            raise JLRuntimeError(arity_message(f.name, f.nrequired, f.nparams, n), span,
                                 helps=[f"signature: {f.signature}"])
        args = list(args)
        for default in f.defaults[n:]:
            args.append(default(f.closure) if default is not None else None)
        return args

    def _check_params(self, f: Function, args: list, span: Span | None) -> None:
        for i, (check, tname) in enumerate(f.param_checks):
            if check is None:
                continue
            v = args[i]
            if not check(v):
                p = f.node.params[i]
                raise JLRuntimeError(f"`{f.name}` expects `{p.name}` to be {tname}, but got {type_name(v)}",
                                     span, helps=[f"signature: {f.signature}"])
            if tname == "float" and type(v) is int:
                args[i] = float(v)

    def _check_return(self, f: Function, value: Any, span: Span | None) -> Any:
        check, tname = f.ret_check
        if not check(value):
            raise JLRuntimeError(f"`{f.name}` should return {tname}, but returned {type_name(value)}",
                                 span, helps=[f"signature: {f.signature}"])
        if tname == "float" and type(value) is int:
            return float(value)
        return value

    # Modules
    def _module_names(self, importer: str, visiting: set[str] | None = None
                      ) -> Callable[[str, Span], dict[str, str]]:
        """Return a function that maps the names `import "path"` adds to their kind, for the resolver."""
        visiting = set() if visiting is None else visiting

        def names(path: str, span: Span) -> dict[str, str]:
            full = module_path(path, importer)
            if full in visiting:  # circular import, reported when it runs
                return {}
            text = _read_module(full, path, span)
            try:
                program = parse(Source(text, full))
            except JarLangError as err:
                helps = [f"the error is at {err.span.location()}"] if err.span else []
                raise JLRuntimeError(f"cannot import {path!r}: {err.message}", span, helps=helps) from None
            # The module's own imports add names to it too, so follow them
            visiting.add(full)
            try:
                r = Resolver(module_names=self._module_names(full, visiting))
                r._collect(program.stmts, r.global_scope)
            finally:
                visiting.discard(full)
            return {n: sym.kind for n, sym in r.global_scope.symbols.items() if not n.startswith("_")}
        return names

    def load_module(self, path: str, importer: str, span: Span) -> dict[str, Any]:
        full = module_path(path, importer)
        if full in self.modules:
            return self.modules[full]
        if full in self._importing:
            raise JLRuntimeError(f"circular import of {path!r}", span)
        text = _read_module(full, path, span)
        self._importing.append(full)
        try:
            module_globals: dict[str, Any] = {}
            compiled = self.compile_program(parse(Source(text, full)), full, module_globals)
            compiled.run()
        finally:
            self._importing.pop()
        exports = {k: v for k, v in module_globals.items() if not k.startswith("_")}
        self.modules[full] = exports
        return exports


def _read_module(full: str, path: str, span: Span) -> str:
    try:
        with open(full, encoding="utf-8-sig") as fh:
            return fh.read()
    except OSError:
        raise JLRuntimeError(f"cannot find module {path!r}", span, helps=[f"looked for {full}"]) from None
    except UnicodeDecodeError:
        raise JLRuntimeError(f"cannot read module {path!r}", span, helps=[f"{full} is not a UTF-8 text file"]) from None


def _add_trace(e: JLRuntimeError, fname: str, span: Span | None) -> None:
    trace = e.diagnostic.trace
    counts: list[int] = e.__dict__.setdefault("_trace_counts", [])
    where = f" (called at {span.location()})" if span is not None else ""
    entry = f"in `{fname}`{where}"
    base = e.__dict__.setdefault("_trace_base", [])
    if base and base[-1] == entry:
        counts[-1] += 1
        trace[len(base) - 1] = f"{entry} ×{counts[-1]}"
        return
    if len(base) < MAX_TRACE:
        base.append(entry)
        counts.append(1)
        trace.append(entry)
    elif len(base) == MAX_TRACE:
        base.append("...")
        counts.append(1)
        trace.append("... (more frames omitted)")


# Compiler: converts each node into a Python function

Code = Callable[[Frame], Any]


class Compiler:
    def __init__(self, interp: Interpreter, globals_: dict[str, Any], module: str) -> None:
        self.interp = interp
        self.g = globals_
        self.module = module

    # Blocks
    def block(self, stmts: list[ast.Stmt], want_value: bool, hoist: bool = False) -> Code:
        hoisted: list[Code] = []
        execs: list[Code] = []
        last: Code | None = None
        for i, stmt in enumerate(stmts):
            is_last = i == len(stmts) - 1
            if hoist and isinstance(stmt, ast.FnDecl):
                hoisted.append(self.stmt(stmt))
                if is_last and want_value:
                    last = lambda fr: None  # noqa: E731
                continue
            if is_last and want_value and isinstance(stmt, ast.ExprStmt):
                if isinstance(stmt.expr, ast.IfExpr):
                    last = self.if_expr(stmt.expr, mode="tail")
                else:
                    last = self.expr(stmt.expr)
            else:
                execs.append(self.stmt(stmt))
        all_execs = hoisted + execs
        if last is not None and self.interp.trace is not None and stmts and \
                isinstance(stmts[-1], ast.ExprStmt):
            last = self._traced(stmts[-1], last)

        if last is None:
            if not all_execs:
                return lambda fr: None
            if len(all_execs) == 1:
                return all_execs[0]
            if len(all_execs) == 2:
                a, b = all_execs

                def run2(fr: Frame) -> Any:
                    r = a(fr)
                    if r is not None:
                        return r
                    return b(fr)
                return run2

            def run_n(fr: Frame) -> Any:
                for e in all_execs:
                    r = e(fr)
                    if r is not None:
                        return r
                return None
            return run_n

        if not all_execs:
            return last
        if len(all_execs) == 1:
            first = all_execs[0]

            def run1v(fr: Frame) -> Any:
                r = first(fr)
                if r is not None:
                    return r
                return last(fr)
            return run1v

        def run_nv(fr: Frame) -> Any:
            for e in all_execs:
                r = e(fr)
                if r is not None:
                    return r
            return last(fr)
        return run_nv

    # Statements
    def stmt(self, node: ast.Stmt) -> Code:
        method = getattr(self, "s_" + type(node).__name__)
        code = method(node)
        if self.interp.trace is not None and not isinstance(node, ast.FnDecl):
            return self._traced(node, code)
        return code

    def _traced(self, node: ast.Stmt, code: Code) -> Code:
        interp = self.interp
        line = node.span.line
        text = node.span.source.line_text(line).strip()
        name = os.path.basename(node.span.source.name)

        def traced(fr: Frame) -> Any:
            key = (name, line, interp.depth)
            if interp._last_trace != key:  # one entry per line, even for if c { return x }
                interp._last_trace = key
                interp.trace.write(f"{name}:{line:<4} {'  ' * interp.depth}{text}\n")
            return code(fr)
        return traced

    def s_ExprStmt(self, node: ast.ExprStmt) -> Code:
        if isinstance(node.expr, ast.IfExpr):
            return self.if_expr(node.expr, mode="stmt")
        e = self.expr(node.expr)

        def run(fr: Frame) -> None:
            e(fr)
        return run

    def s_Let(self, node: ast.Let) -> Code:
        setter = self.setter(node.target)
        if node.value is None:
            return lambda fr: setter(fr, None)
        value = self.expr(node.value)
        if node.type is not None:
            check, tname = self.type_check(node.type)
            span, name = node.value.span, node.target.name

            def run_typed(fr: Frame) -> None:
                v = value(fr)
                if not check(v):
                    raise JLRuntimeError(f"`{name}` is declared as {tname}, but the value is {type_name(v)}", span)
                if tname == "float" and type(v) is int:
                    v = float(v)
                setter(fr, v)
            return run_typed

        def run(fr: Frame) -> None:
            setter(fr, value(fr))
        return run

    def s_Assign(self, node: ast.Assign) -> Code:
        target = node.target
        value = self.expr(node.value)
        span = node.span

        if isinstance(target, ast.ListLit):
            return self._multi_assign(target, node.value, span)

        if node.op is not None:
            opfn = ops.BINARY[node.op]
            pyop = _ARITH.get(node.op)
            if isinstance(target, ast.Name):
                getter = self.name_getter(target)
                setter = self.setter(target)
                if pyop is not None and target.depth == 0:
                    # count += 1 on a local variable, the most common case in loops
                    slot, tname, tspan = target.slot, target.name, target.span

                    def run_local(fr: Frame) -> None:
                        a = fr[slot]
                        if a is UNSET:
                            raise _unset(tname, tspan)
                        b = value(fr)
                        ta, tb = a.__class__, b.__class__
                        if (ta is int or ta is float) and (tb is int or tb is float):
                            fr[slot] = pyop(a, b)
                            return
                        try:
                            fr[slot] = opfn(a, b)
                        except BuiltinError as e:
                            raise _rt(e, span) from None
                    return run_local
                if pyop is not None:
                    def run_fast(fr: Frame) -> None:
                        a = getter(fr)
                        b = value(fr)
                        ta, tb = a.__class__, b.__class__
                        if (ta is int or ta is float) and (tb is int or tb is float):
                            setter(fr, pyop(a, b))
                            return
                        try:
                            setter(fr, opfn(a, b))
                        except BuiltinError as e:
                            raise _rt(e, span) from None
                    return run_fast

                def run_op(fr: Frame) -> None:
                    try:
                        setter(fr, opfn(getter(fr), value(fr)))
                    except BuiltinError as e:
                        raise _rt(e, span) from None
                return run_op
            if isinstance(target, ast.Index):
                obj, idx = self.expr(target.obj), self.expr(target.index)

                def run_index_op(fr: Frame) -> None:
                    o, k = obj(fr), idx(fr)
                    try:
                        ops.set_index(o, k, opfn(ops.index(o, k), value(fr)))
                    except BuiltinError as e:
                        raise _rt(e, span) from None
                return run_index_op
            if isinstance(target, ast.Field):
                obj = self.expr(target.obj)
                fname = target.name

                def run_field_op(fr: Frame) -> None:
                    o = obj(fr)
                    cur = self._field(o, fname, target)
                    try:
                        o[fname] = opfn(cur, value(fr))
                    except BuiltinError as e:
                        raise _rt(e, span) from None
                return run_field_op

        if isinstance(target, ast.Name):
            setter = self.setter(target)

            def run_name(fr: Frame) -> None:
                setter(fr, value(fr))
            return run_name
        if isinstance(target, ast.Index):
            obj, idx = self.expr(target.obj), self.expr(target.index)

            def run_index(fr: Frame) -> None:
                v = value(fr)
                o = obj(fr)
                if o.__class__ is list:
                    k = idx(fr)
                    if k.__class__ is int and -len(o) <= k < len(o):
                        o[k] = v
                        return
                    try:
                        ops.set_index(o, k, v)
                    except BuiltinError as e:
                        raise _rt(e, span) from None
                    return
                try:
                    ops.set_index(o, idx(fr), v)
                except BuiltinError as e:
                    raise _rt(e, span) from None
            return run_index
        if isinstance(target, ast.Field):
            obj = self.expr(target.obj)
            fname = target.name

            def run_field(fr: Frame) -> None:
                v = value(fr)
                o = obj(fr)
                if o.__class__ is not dict:
                    raise JLRuntimeError(f"cannot set field `{fname}` on {type_name(o)}", target.span,
                                         helps=["only dicts have fields"])
                o[fname] = v
            return run_field
        raise JLRuntimeError("invalid assignment target", target.span)  # pragma: no cover

    def _multi_assign(self, target: ast.ListLit, value_node: ast.Expr, span: Span) -> Code:
        setters = [self._target_setter(t) for t in target.items]
        count = len(setters)
        if isinstance(value_node, ast.ListLit) and len(value_node.items) == count:
            values = [self.expr(v) for v in value_node.items]

            def run_parallel(fr: Frame) -> None:
                vals = [v(fr) for v in values]
                for s, v in zip(setters, vals):
                    s(fr, v)
            return run_parallel
        value = self.expr(value_node)

        def run_destructure(fr: Frame) -> None:
            v = value(fr)
            items = self._destructure(v, count, span)
            for s, item in zip(setters, items):
                s(fr, item)
        return run_destructure

    def _destructure(self, v: Any, count: int, span: Span) -> list:
        if type(v) not in (list, range, str):
            raise JLRuntimeError(f"cannot unpack {type_name(v)} into {count} variables", span)
        if len(v) != count:
            raise JLRuntimeError(f"cannot unpack {len(v)} values into {count} variables", span)
        return list(v)

    def _target_setter(self, target: ast.Expr) -> Callable[[Frame, Any], None]:
        if isinstance(target, ast.Name):
            return self.setter(target)
        if isinstance(target, ast.Index):
            obj, idx = self.expr(target.obj), self.expr(target.index)

            def set_index(fr: Frame, v: Any) -> None:
                try:
                    ops.set_index(obj(fr), idx(fr), v)
                except BuiltinError as e:
                    raise _rt(e, target.span) from None
            return set_index
        if isinstance(target, ast.Field):
            obj = self.expr(target.obj)

            def set_field(fr: Frame, v: Any) -> None:
                o = obj(fr)
                if o.__class__ is not dict:
                    raise JLRuntimeError(f"cannot set field `{target.name}` on {type_name(o)}", target.span)
                o[target.name] = v
            return set_field
        raise JLRuntimeError("invalid assignment target", target.span)

    def s_FnDecl(self, node: ast.FnDecl) -> Code:
        make = self.function_maker(node.func, node.target.name)
        setter = self.setter(node.target)

        def run(fr: Frame) -> None:
            setter(fr, make(fr))
        return run

    def s_While(self, node: ast.While) -> Code:
        cond = self.expr(node.cond)
        body = self.block(node.body.stmts, want_value=False)

        def run(fr: Frame) -> Any:
            while cond(fr):
                try:
                    r = body(fr)
                except SignalException as s:
                    r = s.signal
                if r is not None:
                    if r is BREAK:
                        break
                    if r is CONTINUE:
                        continue
                    return r
            return None
        return run

    def s_For(self, node: ast.For) -> Code:
        iterable = self.expr(node.iterable)
        body = self.block(node.body.stmts, want_value=False)
        span = node.iterable.span
        assign = self._loop_assigner(node.targets, span)

        def run(fr: Frame) -> Any:
            seq = _iterate(iterable(fr), span)
            for item in seq:
                assign(fr, item)
                try:
                    r = body(fr)
                except SignalException as s:
                    r = s.signal
                if r is not None:
                    if r is BREAK:
                        break
                    if r is CONTINUE:
                        continue
                    return r
            return None
        return run

    def _loop_assigner(self, targets: list[ast.Name], span: Span) -> Callable[[Frame, Any], None]:
        if len(targets) == 1:
            return self.setter(targets[0])
        setters = [self.setter(t) for t in targets]
        count = len(setters)

        def assign(fr: Frame, item: Any) -> None:
            if type(item) is not list or len(item) != count:
                what = f"{len(item)} values" if type(item) in (list, str) else type_name(item)
                raise JLRuntimeError(f"cannot unpack {what} into {count} loop variables", span,
                                     helps=["iterate over enumerate(xs), zip(a, b) or items(d) to get pairs"])
            for s, v in zip(setters, item):
                s(fr, v)
        return assign

    def s_Return(self, node: ast.Return) -> Code:
        if node.value is None:
            nil_ret = Ret(None)
            return lambda fr: nil_ret
        value = self.expr(node.value)
        return lambda fr: Ret(value(fr))

    def s_Break(self, node: ast.Break) -> Code:
        return lambda fr: BREAK

    def s_Continue(self, node: ast.Continue) -> Code:
        return lambda fr: CONTINUE

    def s_Throw(self, node: ast.Throw) -> Code:
        value = self.expr(node.value)
        span = node.span

        def run(fr: Frame) -> None:
            raise JLThrow(value(fr), span)
        return run

    def s_Try(self, node: ast.Try) -> Code:
        body = self.block(node.body.stmts, want_value=False)
        handler = self.block(node.handler.stmts, want_value=False)
        setter = self.setter(node.catch_name) if node.catch_name is not None else None
        interp = self.interp

        def run(fr: Frame) -> Any:
            depth = interp.depth
            try:
                return body(fr)
            except JLThrow as t:
                caught = t.value
            except JLRuntimeError as e:
                caught = e.message
            except RecursionError:
                caught = "maximum recursion depth exceeded"
            interp.depth = depth
            if setter is not None:
                setter(fr, caught)
            return handler(fr)
        return run

    def s_Import(self, node: ast.Import) -> Code:
        interp, g, module = self.interp, self.g, self.module
        setter = self.setter(node.alias) if node.alias is not None else None

        def run(fr: Frame) -> None:
            exports = interp.load_module(node.path, module, node.span)
            if setter is not None:
                setter(fr, dict(exports))
            else:
                g.update(exports)
        return run

    # Variables
    def name_getter(self, node: ast.Name) -> Code:
        name, depth, slot = node.name, node.depth, node.slot
        span = node.span
        if depth == GLOBAL:
            g = self.g
            builtin = BUILTINS.get(name, MISSING)

            def get_global(fr: Frame) -> Any:
                v = g.get(name, MISSING)
                if v is MISSING:
                    if builtin is not MISSING:
                        return builtin
                    raise JLRuntimeError(f"`{name}` is used before it is defined", span,
                                         helps=[f"assign `{name}` before this line runs"])
                return v
            return get_global

        if depth == 0:
            def get_local(fr: Frame) -> Any:
                v = fr[slot]
                if v is UNSET:
                    raise _unset(name, span)
                return v
            return get_local
        if depth == 1:
            def get_outer(fr: Frame) -> Any:
                v = fr[0][slot]
                if v is UNSET:
                    raise _unset(name, span)
                return v
            return get_outer

        def get_deep(fr: Frame) -> Any:
            for _ in range(depth):
                fr = fr[0]
            v = fr[slot]
            if v is UNSET:
                raise _unset(name, span)
            return v
        return get_deep

    def setter(self, node: ast.Name) -> Callable[[Frame, Any], None]:
        name, depth, slot = node.name, node.depth, node.slot
        if depth == GLOBAL:
            g = self.g

            def set_global(fr: Frame, v: Any) -> None:
                g[name] = v
            return set_global
        if depth == 0:
            def set_local(fr: Frame, v: Any) -> None:
                fr[slot] = v
            return set_local

        def set_deep(fr: Frame, v: Any) -> None:
            for _ in range(depth):
                fr = fr[0]
            fr[slot] = v
        return set_deep

    # Functions
    def type_check(self, ref: ast.TypeRef) -> tuple[Callable[[Any], bool], str]:
        return _TYPE_CHECKS[ref.name], ref.name  # the resolver reports unknown types

    def function_maker(self, func: ast.Function, name: str | None) -> Callable[[Frame], Function]:
        body = self.block(func.body.stmts, want_value=True, hoist=True)
        defaults = [self.expr(p.default) if p.default is not None else None for p in func.params]
        param_checks = []
        for p in func.params:
            param_checks.append(self.type_check(p.type) if p.type is not None else (None, "any"))
        if all(c is None for c, _ in param_checks):
            param_checks = []
        ret_check = self.type_check(func.ret_type) if func.ret_type is not None else None
        fname = name or func.name or "<lambda>"
        g, module = self.g, self.module
        nrequired = sum(1 for p in func.params if p.default is None)
        tail = [UNSET] * (func.nlocals - 1 - len(func.params))

        def make(fr: Frame) -> Function:
            return Function(fname, func, fr, body, defaults, param_checks, ret_check, g, module, nrequired, tail)
        return make

    # Expressions
    def expr(self, node: ast.Expr) -> Code:
        method = getattr(self, "e_" + type(node).__name__)
        return method(node)

    def e_Literal(self, node: ast.Literal) -> Code:
        v = node.value
        return lambda fr: v

    def e_StringLit(self, node: ast.StringLit) -> Code:
        if node.is_plain:
            text = node.plain_value
            return lambda fr: text
        pieces: list[Any] = []
        for part in node.parts:
            if isinstance(part, str):
                pieces.append(part)
            else:
                pieces.append((self.expr(part.expr), part.spec, part.span))

        def run(fr: Frame) -> str:
            out = []
            for piece in pieces:
                if piece.__class__ is str:
                    out.append(piece)
                    continue
                code, spec, span = piece
                v = code(fr)
                if spec is None:
                    out.append(v if v.__class__ is str else show(v))
                else:
                    try:
                        out.append(format_value(v, spec))
                    except BuiltinError as e:
                        raise _rt(e, span) from None
            return "".join(out)
        return run

    def e_Name(self, node: ast.Name) -> Code:
        return self.name_getter(node)

    def e_ListLit(self, node: ast.ListLit) -> Code:
        items = [self.expr(i) for i in node.items]
        if not items:
            return lambda fr: []
        return lambda fr: [i(fr) for i in items]

    def e_DictLit(self, node: ast.DictLit) -> Code:
        entries = [(self.expr(k), self.expr(v), k.span) for k, v in node.entries]

        def run(fr: Frame) -> dict:
            out = {}
            for k, v, span in entries:
                key = k(fr)
                try:
                    out[ops.hashable_key(key)] = v(fr)
                except BuiltinError as e:
                    raise _rt(e, span) from None
            return out
        return run

    def e_RangeExpr(self, node: ast.RangeExpr) -> Code:
        start, end = self.expr(node.start), self.expr(node.end)
        inclusive = node.inclusive
        span = node.span

        def run(fr: Frame) -> range:
            a, b = start(fr), end(fr)
            if a.__class__ is int and b.__class__ is int:
                return range(a, b + 1 if inclusive else b)
            try:
                return ops.make_range(a, b, inclusive)
            except BuiltinError as e:
                raise _rt(e, span) from None
        return run

    def e_Unary(self, node: ast.Unary) -> Code:
        operand = self.expr(node.operand)
        span = node.span
        op = node.op
        if op == "not":
            return lambda fr: not operand(fr)
        if op == "-":
            def neg(fr: Frame) -> Any:
                v = operand(fr)
                if v.__class__ is int or v.__class__ is float:
                    return -v
                try:
                    return ops.neg(v)
                except BuiltinError as e:
                    raise _rt(e, span) from None
            return neg
        fn = {"+": ops.pos, "√": ops.sqrt, "!": ops.factorial}[op]

        def run(fr: Frame) -> Any:
            try:
                return fn(operand(fr))
            except BuiltinError as e:
                raise _rt(e, span) from None
        return run

    def _local_slot(self, node: ast.Expr) -> int | None:
        """Return the slot of a variable in the current frame, or None."""
        if isinstance(node, ast.Name) and node.depth == 0:
            return node.slot
        return None

    @staticmethod
    def _number_const(node: ast.Expr) -> Any:
        if isinstance(node, ast.Literal) and node.value.__class__ in (int, float):
            return node.value
        return None

    def _specialized_binary(self, node: ast.Binary) -> Code | None:
        """Faster code for x + 1, n - 2 or a * b when one side is a local variable or a number."""
        op = node.op
        pyop = _ARITH.get(op)
        if pyop is None:
            return None
        slow = ops.BINARY[op]
        span = node.span
        k = self._number_const(node.right)
        slot = self._local_slot(node.left)
        rslot = self._local_slot(node.right)
        name = node.left.name if isinstance(node.left, ast.Name) else ""

        def fail(a: Any, b: Any) -> Any:
            if a is UNSET:
                raise _unset(name, node.left.span)
            try:
                return slow(a, b)
            except BuiltinError as e:
                raise _rt(e, span) from None

        if k is not None and slot is not None:
            def local_const(fr: Frame) -> Any:
                a = fr[slot]
                if a.__class__ is int or a.__class__ is float:
                    return pyop(a, k)
                return fail(a, k)
            return local_const
        if k is not None:
            left = self.expr(node.left)

            def any_const(fr: Frame) -> Any:
                a = left(fr)
                if a.__class__ is int or a.__class__ is float:
                    return pyop(a, k)
                return fail(a, k)
            return any_const
        if slot is not None and rslot is not None:
            rname = node.right.name  # type: ignore[union-attr]

            def local_local(fr: Frame) -> Any:
                a, b = fr[slot], fr[rslot]
                if (a.__class__ is int or a.__class__ is float) and (b.__class__ is int or b.__class__ is float):
                    return pyop(a, b)
                if b is UNSET:
                    raise _unset(rname, node.right.span)
                return fail(a, b)
            return local_local
        return None

    def e_Binary(self, node: ast.Binary) -> Code:
        special = self._specialized_binary(node)
        if special is not None:
            return special
        left, right = self.expr(node.left), self.expr(node.right)
        span = node.span
        op = node.op
        slow = ops.BINARY[op]
        if op == "+":
            def add(fr: Frame) -> Any:
                a, b = left(fr), right(fr)
                if a.__class__ is int and b.__class__ is int:
                    return a + b
                if a.__class__ is float and b.__class__ is float:
                    return a + b
                try:
                    return slow(a, b)
                except BuiltinError as e:
                    raise _rt(e, span) from None
            return add
        if op == "-":
            def sub(fr: Frame) -> Any:
                a, b = left(fr), right(fr)
                if a.__class__ is int and b.__class__ is int:
                    return a - b
                if a.__class__ is float and b.__class__ is float:
                    return a - b
                try:
                    return slow(a, b)
                except BuiltinError as e:
                    raise _rt(e, span) from None
            return sub
        if op == "*":
            def mul(fr: Frame) -> Any:
                a, b = left(fr), right(fr)
                if a.__class__ is float and b.__class__ is float:
                    return a * b
                if a.__class__ is int and b.__class__ is int:
                    return a * b
                try:
                    return slow(a, b)
                except BuiltinError as e:
                    raise _rt(e, span) from None
            return mul
        if op == "/":
            def div(fr: Frame) -> Any:
                a, b = left(fr), right(fr)
                if a.__class__ is float and b.__class__ is float and b:
                    return a / b
                try:
                    return slow(a, b)
                except BuiltinError as e:
                    raise _rt(e, span) from None
            return div
        if op in ("%", "//"):
            is_mod = op == "%"

            def intdiv(fr: Frame) -> Any:
                a, b = left(fr), right(fr)
                if a.__class__ is int and b.__class__ is int and b:
                    return a % b if is_mod else a // b
                try:
                    return slow(a, b)
                except BuiltinError as e:
                    raise _rt(e, span) from None
            return intdiv

        def run(fr: Frame) -> Any:
            try:
                return slow(left(fr), right(fr))
            except BuiltinError as e:
                raise _rt(e, span) from None
        return run

    def e_Compare(self, node: ast.Compare) -> Code:
        operands = [self.expr(o) for o in node.operands]
        fns = [ops.COMPARE[op] for op in node.ops]
        span = node.span
        if len(fns) == 1:
            left, right = operands
            fn = fns[0]
            op = node.ops[0]
            if op in ("<", "<=", ">", ">="):
                pyop = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}[op]
                k = self._number_const(node.operands[1])
                if k is not None:
                    slot = self._local_slot(node.operands[0])

                    def cmp_fail(a: Any) -> bool:
                        if a is UNSET:
                            raise _unset(node.operands[0].name, node.operands[0].span)
                        try:
                            return fn(a, k)
                        except BuiltinError as e:
                            raise _rt(e, span) from None

                    if slot is not None:
                        # One function per operator so the comparison is inline
                        if op == "<":
                            def lt_local(fr: Frame) -> bool:
                                a = fr[slot]
                                if a.__class__ is int or a.__class__ is float:
                                    return a < k
                                return cmp_fail(a)
                            return lt_local
                        if op == "<=":
                            def le_local(fr: Frame) -> bool:
                                a = fr[slot]
                                if a.__class__ is int or a.__class__ is float:
                                    return a <= k
                                return cmp_fail(a)
                            return le_local
                        if op == ">":
                            def gt_local(fr: Frame) -> bool:
                                a = fr[slot]
                                if a.__class__ is int or a.__class__ is float:
                                    return a > k
                                return cmp_fail(a)
                            return gt_local

                        def ge_local(fr: Frame) -> bool:
                            a = fr[slot]
                            if a.__class__ is int or a.__class__ is float:
                                return a >= k
                            return cmp_fail(a)
                        return ge_local

                    def cmp_const(fr: Frame) -> bool:
                        a = left(fr)
                        if a.__class__ is int or a.__class__ is float:
                            return pyop(a, k)
                        return cmp_fail(a)
                    return cmp_const

                def cmp_order(fr: Frame) -> bool:
                    a, b = left(fr), right(fr)
                    if (a.__class__ is int or a.__class__ is float) and (b.__class__ is int or b.__class__ is float):
                        return pyop(a, b)
                    try:
                        return fn(a, b)
                    except BuiltinError as e:
                        raise _rt(e, span) from None
                return cmp_order
            if op in ("==", "!="):
                is_eq = op == "=="
                k = self._number_const(node.operands[1])
                if k is not None:
                    def eq_const(fr: Frame) -> bool:
                        a = left(fr)
                        if a.__class__ is int or a.__class__ is float:
                            return (a == k) is is_eq
                        return ops.eq(a, k) is is_eq
                    return eq_const

                def cmp_eq(fr: Frame) -> bool:
                    a, b = left(fr), right(fr)
                    t = a.__class__
                    # Lists and dicts need ops.eq, which doesn't treat true as 1 inside them
                    if t is b.__class__ and t is not list and t is not dict:
                        return (a == b) is is_eq
                    return ops.eq(a, b) is is_eq
                return cmp_eq

            def cmp1(fr: Frame) -> bool:
                try:
                    return fn(left(fr), right(fr))
                except BuiltinError as e:
                    raise _rt(e, span) from None
            return cmp1

        def chain(fr: Frame) -> bool:
            a = operands[0](fr)
            for fn, rhs in zip(fns, operands[1:]):
                b = rhs(fr)
                try:
                    if not fn(a, b):
                        return False
                except BuiltinError as e:
                    raise _rt(e, span) from None
                a = b
            return True
        return chain

    def e_Logical(self, node: ast.Logical) -> Code:
        left, right = self.expr(node.left), self.expr(node.right)
        if node.op == "and":
            def and_(fr: Frame) -> Any:
                a = left(fr)
                return right(fr) if a else a
            return and_

        def or_(fr: Frame) -> Any:
            a = left(fr)
            return a if a else right(fr)
        return or_

    def _callee(self, node: ast.Expr) -> Code:
        """Like expr, but with a faster lookup for calls to global functions."""
        if isinstance(node, ast.Name) and node.depth == GLOBAL:
            g, name = self.g, node.name
            builtin = BUILTINS.get(name, MISSING)
            if builtin is not MISSING:
                return self.name_getter(node)
            slow = self.name_getter(node)

            def global_callee(fr: Frame) -> Any:
                f = g.get(name)
                return f if f is not None else slow(fr)
            return global_callee
        return self.expr(node)

    def e_Call(self, node: ast.Call) -> Code:
        callee = self._callee(node.callee)
        args = [self.expr(a) for a in node.args]
        span = node.span
        interp = self.interp
        call_function, call_other = interp.call_function, interp.call_other
        n = len(args)

        # Fast path: inline call_function for user functions without type annotations
        def enter(f: Function, frame: Frame) -> Any:
            interp.depth += 1
            try:
                if interp.depth > interp.max_depth:
                    raise JLRuntimeError("maximum recursion depth exceeded", span,
                                         helps=[f"`{f.name}` recursed more than {interp.max_depth} levels deep",
                                                "check the base case, or use a loop instead"])
                r = f.body(frame)
            except SignalException as s:
                r = s.signal
            except JLRuntimeError as e:
                _add_trace(e, f.name, span)
                raise
            except RecursionError:
                raise JLRuntimeError("maximum recursion depth exceeded", span) from None
            finally:
                interp.depth -= 1
            return r.value if r.__class__ is Ret else r

        if n == 0:
            def call0(fr: Frame) -> Any:
                f = callee(fr)
                if f.__class__ is Function:
                    if f.simple and f.nparams == 0:
                        return enter(f, [f.closure, *f.tail])
                    return call_function(f, [], span)
                return call_other(f, [], span)
            return call0
        if n == 1:
            a0 = args[0]

            def call1(fr: Frame) -> Any:
                f = callee(fr)
                v0 = a0(fr)
                if f.__class__ is Function:
                    if f.simple and f.nparams == 1:
                        interp.depth += 1
                        try:
                            if interp.depth > interp.max_depth:
                                raise JLRuntimeError("maximum recursion depth exceeded", span,
                                                     helps=[f"`{f.name}` recursed more than {interp.max_depth} "
                                                            "levels deep", "check the base case, or use a loop instead"])
                            r = f.body([f.closure, v0, *f.tail])
                        except SignalException as s:
                            r = s.signal
                        except JLRuntimeError as e:
                            _add_trace(e, f.name, span)
                            raise
                        except RecursionError:
                            raise JLRuntimeError("maximum recursion depth exceeded", span) from None
                        finally:
                            interp.depth -= 1
                        return r.value if r.__class__ is Ret else r
                    return call_function(f, [v0], span)
                return call_other(f, [v0], span)
            return call1
        if n == 2:
            a0, a1 = args

            def call2(fr: Frame) -> Any:
                f = callee(fr)
                v0 = a0(fr)
                v1 = a1(fr)
                if f.__class__ is Function:
                    if f.simple and f.nparams == 2:
                        return enter(f, [f.closure, v0, v1, *f.tail])
                    return call_function(f, [v0, v1], span)
                return call_other(f, [v0, v1], span)
            return call2

        def calln(fr: Frame) -> Any:
            f = callee(fr)
            vals = [a(fr) for a in args]
            if f.__class__ is Function:
                return call_function(f, vals, span)
            return call_other(f, vals, span)
        return calln

    def e_MethodCall(self, node: ast.MethodCall) -> Code:
        receiver = self.expr(node.receiver)
        args = [self.expr(a) for a in node.args]
        mname = node.method.name
        span = node.span
        call = self.interp.call
        lookup = self._method_lookup(node.method)

        def run(fr: Frame) -> Any:
            obj = receiver(fr)
            vals = [a(fr) for a in args]
            if obj.__class__ is dict:
                # Only a function field is a method, so {count: 3}.count() still calls count()
                f = obj.get(mname, MISSING)
                if f is not MISSING and is_callable(f):
                    return call(f, vals, span)
            f = lookup(fr)
            if f is MISSING:
                raise _no_method(obj, mname, node.method.span)
            return call(f, [obj, *vals], span)
        return run

    def _method_lookup(self, name: ast.Name) -> Code:
        if name.depth == GLOBAL:
            g = self.g
            key = name.name
            builtin = BUILTINS.get(key, MISSING)

            def lookup_global(fr: Frame) -> Any:
                v = g.get(key, MISSING)
                return builtin if v is MISSING else v
            return lookup_global
        getter = self.name_getter(name)
        return getter

    def _field(self, obj: Any, fname: str, node: ast.Field) -> Any:
        if obj.__class__ is dict:
            v = obj.get(fname, MISSING)
            if v is MISSING:
                keys = [k for k in obj if type(k) is str]
                guess = suggest(fname, keys)
                helps = [f"did you mean `{guess}`?"] if guess else []
                helps.append("use get(d, key, default) for optional keys")
                raise JLRuntimeError(f"dict has no key `{fname}`", node.name_span or node.span, helps=helps)
            return v
        helps = []
        if fname in BUILTINS and is_callable(BUILTINS[fname]):
            helps.append(f"to call `{fname}` on this value write `.{fname}()` with parentheses")
        raise JLRuntimeError(f"{type_name(obj)} has no field `{fname}`", node.name_span or node.span, helps=helps)

    def e_Field(self, node: ast.Field) -> Code:
        obj = self.expr(node.obj)
        fname = node.name

        def run(fr: Frame) -> Any:
            o = obj(fr)
            if o.__class__ is dict:
                v = o.get(fname, MISSING)
                if v is not MISSING:
                    return v
            return self._field(o, fname, node)
        return run

    def e_Index(self, node: ast.Index) -> Code:
        obj, idx = self.expr(node.obj), self.expr(node.index)
        span = node.span

        def run(fr: Frame) -> Any:
            o, k = obj(fr), idx(fr)
            if o.__class__ is list and k.__class__ is int and -len(o) <= k < len(o):
                return o[k]
            try:
                return ops.index(o, k)
            except BuiltinError as e:
                raise _rt(e, span) from None
        return run

    def e_Slice(self, node: ast.Slice) -> Code:
        obj = self.expr(node.obj)
        parts = [self.expr(p) if p is not None else None for p in (node.start, node.stop, node.step)]
        span = node.span

        def run(fr: Frame) -> Any:
            vals = [p(fr) if p is not None else None for p in parts]
            try:
                return ops.slice_(obj(fr), *vals)
            except BuiltinError as e:
                raise _rt(e, span) from None
        return run

    def e_Pipe(self, node: ast.Pipe) -> Code:
        value = self.expr(node.value)
        call = self.interp.call
        span = node.span
        if isinstance(node.func, ast.Call):
            callee = self.expr(node.func.callee)
            args = [self.expr(a) for a in node.func.args]

            def run_call(fr: Frame) -> Any:
                v = value(fr)
                return call(callee(fr), [v, *[a(fr) for a in args]], span)
            return run_call
        func = self.expr(node.func)

        def run(fr: Frame) -> Any:
            v = value(fr)
            return call(func(fr), [v], span)
        return run

    def e_Lambda(self, node: ast.Lambda) -> Code:
        return self.function_maker(node.func, None)

    def e_IfExpr(self, node: ast.IfExpr) -> Code:
        return self.if_expr(node, mode="expr")

    def if_expr(self, node: ast.IfExpr, mode: str) -> Code:
        """mode is "stmt" (no value), "tail" (value or signal) or "expr" (value, signals raise)."""
        want_value = mode != "stmt"
        branches = [(self.expr(c), self.block(b.stmts, want_value)) for c, b in node.branches]
        else_ = self.block(node.else_.stmts, want_value) if node.else_ is not None else None

        if len(branches) == 1:
            (cond, then), = branches
            if mode != "expr":
                if else_ is None:
                    def if1(fr: Frame) -> Any:
                        if cond(fr):
                            return then(fr)
                        return None
                    return if1

                def if2(fr: Frame) -> Any:
                    if cond(fr):
                        return then(fr)
                    return else_(fr)
                return if2

        def run(fr: Frame) -> Any:
            for cond, blk in branches:
                if cond(fr):
                    r = blk(fr)
                    break
            else:
                r = else_(fr) if else_ is not None else None
            if mode == "expr" and r.__class__ in _SIGNAL_TYPES:
                raise SignalException(r)
            return r
        return run

    def e_Comprehension(self, node: ast.Comprehension) -> Code:
        iterable = self.expr(node.iterable)
        expr = self.expr(node.expr)
        cond = self.expr(node.cond) if node.cond is not None else None
        assign = self._loop_assigner(node.targets, node.iterable.span)
        nlocals = node.nlocals
        span = node.iterable.span

        def run(fr: Frame) -> list:
            cf = [fr] + [UNSET] * (nlocals - 1)
            out = []
            for item in _iterate(iterable(fr), span):
                assign(cf, item)
                if cond is None or cond(cf):
                    out.append(expr(cf))
            return out
        return run


def _iterate(v: Any, span: Span) -> Iterable:
    t = v.__class__
    if t is range or t is list or t is str:
        return v
    if t is dict:
        return list(v)
    helps = []
    if is_number(v):
        helps.append(f"to loop {show(v)} times write `for i in 1..{show(v)}`")
    raise JLRuntimeError(f"cannot iterate over {type_name(v)}", span, helps=helps)


def _no_method(obj: Any, name: str, span: Span) -> JLRuntimeError:
    candidates = [k for k, v in BUILTINS.items() if is_callable(v)]
    if type(obj) is dict:
        candidates += [k for k in obj if type(k) is str]
    guess = suggest(name, candidates)
    helps = [f"did you mean `{guess}`?"] if guess else []
    return JLRuntimeError(f"no function or field named `{name}` for {type_name(obj)}", span, helps=helps)


def run_with_big_stack(fn: Callable[[], Any], stack_mb: int = 512) -> Any:
    """Run fn in a thread with a large stack so deep recursion doesn't crash Python."""
    result: dict[str, Any] = {}

    def target() -> None:
        try:
            result["value"] = fn()
        except BaseException as exc:  # re-raised in the calling thread
            result["error"] = exc

    old = threading.stack_size()
    for size_mb in (stack_mb, 255):  # Windows only allows stacks under 256 MB
        try:
            threading.stack_size(size_mb * 1024 * 1024)
            break
        except (ValueError, RuntimeError):
            pass
    else:
        return fn()
    try:
        # daemon, so a thread stuck in a blocking call can't stop Python from exiting
        thread = threading.Thread(target=target, name="jarlang-main", daemon=True)
        thread.start()
        try:
            while thread.is_alive():
                thread.join(0.1)
        except KeyboardInterrupt:
            # Ctrl+C only reaches the main thread, so pass it on or the program keeps running
            _interrupt(thread)
            thread.join(2)  # a thread waiting on input() won't notice until it returns
            raise
    finally:
        threading.stack_size(old)
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _interrupt(thread: threading.Thread) -> None:
    """Raise KeyboardInterrupt inside thread the next time it runs Python code."""
    try:
        import ctypes

        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread.ident),
                                                   ctypes.py_object(KeyboardInterrupt))
    except (ImportError, AttributeError):  # not CPython
        pass


__all__ = ["Interpreter", "CompiledProgram", "JLThrow", "ProgramExit", "run_with_big_stack", "UNSET"]
