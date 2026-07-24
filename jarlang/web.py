"""Runs JarLang for the browser playground and returns JSON-friendly results."""

from __future__ import annotations

import io
import json
import math
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from . import __version__
from .builtins import ProgramExit
from .errors import Diagnostic, JarLangError, MultipleErrors
from .interpreter import Interpreter, run_with_big_stack
from .source import Source
from .values import repr_value, type_name

# File name shown in error messages
FILENAME = "main.jlang"

# True in the browser (Pyodide has no threads and a small stack)
IN_BROWSER = sys.platform == "emscripten"

# Stdout is cut off after this many characters
MAX_OUTPUT = 1_000_000
# A program that keeps printing is stopped after this many
HARD_OUTPUT_LIMIT = 20 * MAX_OUTPUT
# The final value is cut off after this many characters
MAX_VALUE = 100_000

# Stack limits, measured in Pyodide so deep programs don't overflow the browser's stack
MAX_DEPTH = 1000  # how deep JarLang function calls can go
RECURSION_LIMIT = 6_500  # while a program runs (3 to 6 Python frames per JarLang call)
ANALYSIS_RECURSION_LIMIT = 4_000  # while lexing, parsing, checking and compiling

TARGETS = ("linux", "windows", "macos")

# Gets chunks of stdout while a program runs (see set_listener)
_listener: Callable[[str], Any] | None = None
# Seconds between streamed stdout chunks
STREAM_INTERVAL = 0.05

_T = TypeVar("_T")


# Helpers

class OutputLimitExceeded(Exception):
    """Raised when a program prints far too much (JarLang code can't catch it)."""


class _Capture(io.TextIOBase):
    """Stdout replacement that keeps at most limit characters and streams them in batches."""

    def __init__(self, limit: int = MAX_OUTPUT, hard_limit: int = HARD_OUTPUT_LIMIT,
                 listener: Callable[[str], Any] | None = None,
                 interval: float = STREAM_INTERVAL) -> None:
        super().__init__()
        self.limit = limit
        self.hard_limit = max(hard_limit, limit)
        self.parts: list[str] = []
        self.kept = 0
        self.total = 0
        self.listener = listener
        self.interval = interval
        self._pending: list[str] = []
        self._last_flush = time.monotonic()

    @property
    def truncated(self) -> bool:
        return self.total > self.kept

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        n = len(text)
        self.total += n
        room = self.limit - self.kept
        if room > 0:
            piece = text if n <= room else text[:room]
            self.parts.append(piece)
            self.kept += len(piece)
            if self.listener is not None:
                self._pending.append(piece)
                if time.monotonic() - self._last_flush >= self.interval:
                    self.flush()
        if self.total > self.hard_limit:
            raise OutputLimitExceeded()
        return n

    def flush(self) -> None:
        if self.listener is not None and self._pending:
            chunk = "".join(self._pending)
            self._pending.clear()
            self._last_flush = time.monotonic()
            try:
                self.listener(chunk)
            except Exception:  # noqa: BLE001 - a broken listener shouldn't stop the program
                self.listener = None

    def getvalue(self) -> str:
        return "".join(self.parts)


def diagnostic_dict(d: Diagnostic) -> dict[str, Any]:
    """Diagnostic as a dict (lines and columns start at 1, the end is exclusive)."""
    out: dict[str, Any] = {
        "severity": d.severity,
        "message": d.message,
        "label": d.label,
        "helps": list(d.helps),
        "notes": list(d.notes),
        "line": None,
        "column": None,
        "end_line": None,
        "end_column": None,
        "file": None,
        "rendered": d.render(color=False),
    }
    if d.span is not None:
        src = d.span.source
        line, col = src.line_col(d.span.start)
        end_line, end_col = src.line_col(max(d.span.end, d.span.start))
        if (end_line, end_col) == (line, col):  # zero-width span: mark one character
            end_col = col + 1
        out.update(line=line, column=col, end_line=end_line, end_column=end_col, file=src.name)
    return out


def _plain_diagnostic(message: str, rendered: str) -> dict[str, Any]:
    return {"severity": "error", "message": message, "label": "", "helps": [], "notes": [],
            "line": None, "column": None, "end_line": None, "end_column": None, "file": None,
            "rendered": rendered}


def _error_diagnostics(err: JarLangError) -> list[Diagnostic]:
    if isinstance(err, MultipleErrors):
        return list(err.diagnostics)
    return [err.diagnostic]


_TOO_DEEP = "error: the program is nested too deeply to process"


def _failure(err: BaseException, **extra: Any) -> dict[str, Any]:
    """Result for a failed call, with the error text and diagnostics."""
    if isinstance(err, JarLangError):
        return {"ok": False, "stderr": err.render(color=False),
                "diagnostics": [diagnostic_dict(d) for d in _error_diagnostics(err)],
                "error_kind": err.kind, **extra}
    if isinstance(err, RecursionError):
        return {"ok": False, "stderr": _TOO_DEEP, "error_kind": "recursion",
                "diagnostics": [_plain_diagnostic(_TOO_DEEP[7:], _TOO_DEEP)], **extra}
    if isinstance(err, MemoryError):
        return {"ok": False, "stderr": "error: out of memory", "diagnostics": [],
                "error_kind": "memory", **extra}
    text = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    return {"ok": False, "stderr": "internal error (this is a bug in JarLang, please report it):\n" + text,
            "diagnostics": [], "error_kind": "internal", **extra}


@contextmanager
def _recursion_limit(limit: int) -> Iterator[None]:
    """Set Python's recursion limit, then restore it afterwards."""
    old = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(limit)
        yield
    finally:
        sys.setrecursionlimit(old)


def _on_big_stack(fn: Callable[[], _T]) -> _T:
    """Run fn on a thread with a big stack, except in the browser (no threads)."""
    return fn() if IN_BROWSER else run_with_big_stack(fn)


def _analyze(fn: Callable[[], _T]) -> _T:
    """Run lexing, parsing or checking with the analysis recursion limit."""
    with _recursion_limit(ANALYSIS_RECURSION_LIMIT):
        return _on_big_stack(fn)


def _source(code: str) -> Source:
    if not isinstance(code, str):
        raise TypeError("code must be a string")
    return Source(code, FILENAME)


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


# Running programs

class _PlaygroundInterpreter(Interpreter):
    """Reads input() lines from a list and echoes them like a terminal."""

    def __init__(self, stdin_lines: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.stdin_lines = stdin_lines
        self.stdin_exhausted = False

    def read_line(self, prompt: str) -> str:
        self.write(prompt)
        if self.stdin_lines:
            line = self.stdin_lines.pop(0)
        else:
            line = ""
            self.stdin_exhausted = True
        self.write(line + "\n")
        return line


def set_listener(listener: Callable[[str], Any] | None) -> None:
    """Set a function that gets stdout chunks while a program runs (with stream=True)."""
    global _listener
    _listener = listener


# Each input() call takes the next line of stdin_lines, and gets "" once they run out
def run(code: str, seed: int | None = None, stdin_lines: list[str] | None = None, *,
        max_output: int = MAX_OUTPUT, hard_output_limit: int | None = None,
        max_depth: int = MAX_DEPTH, stream: bool = False) -> dict[str, Any]:
    """Run a program and return its output, errors, final value and timing."""
    lines = [str(line) for line in (stdin_lines or [])]
    hard = hard_output_limit if hard_output_limit is not None else max(HARD_OUTPUT_LIMIT, max_output)
    capture = _Capture(max_output, hard, _listener if stream else None)
    result: dict[str, Any] = {
        "ok": True, "stdout": "", "stderr": "", "value": None, "value_type": None,
        "warnings": [], "diagnostics": [], "exit_code": 0, "error_kind": None,
        "truncated": False, "stdin_exhausted": False, "time_ms": 0.0,
    }
    interp: _PlaygroundInterpreter | None = None
    running = False  # True once the program starts running
    start = time.perf_counter()
    with _recursion_limit(ANALYSIS_RECURSION_LIMIT):  # restores the old limit afterwards
        try:
            source = _source(code)
            interp = _PlaygroundInterpreter(lines, stdout=capture, seed=seed, max_depth=max_depth)
            interp.globals["argv"] = []
            sys.setrecursionlimit(ANALYSIS_RECURSION_LIMIT)  # the interpreter raised it, set it back
            compiled = _on_big_stack(lambda: interp.compile(source))
            result["warnings"] = [diagnostic_dict(w) for w in compiled.warnings]
            sys.setrecursionlimit(RECURSION_LIMIT)
            running = True
            value = _on_big_stack(compiled.run)
            if compiled.ends_with_expression and value is not None:
                text = repr_value(value)
                if len(text) > MAX_VALUE:
                    text = text[:MAX_VALUE] + f"… ({len(text) - MAX_VALUE:,} more characters)"
                result["value"] = text
                result["value_type"] = type_name(value)
        except ProgramExit as e:
            result["exit_code"] = e.code
        except OutputLimitExceeded:
            result.update(ok=False, error_kind="output",
                          stderr=f"error: the program was stopped after printing more than "
                                 f"{hard:,} characters")
        except JarLangError as e:
            result.update(_failure(e))
        except RecursionError as e:
            if running:
                result.update(ok=False, error_kind="recursion",
                              stderr="error: maximum recursion depth exceeded\n"
                                     "  = help: the playground allows about "
                                     f"{max_depth} nested calls; use a loop, or a smaller input")
            else:  # the code is nested too deeply to parse or compile
                result.update(_failure(e))
        except Exception as e:  # noqa: BLE001 - report bugs instead of crashing the page
            result.update(_failure(e))
        finally:
            capture.flush()
            result["time_ms"] = _elapsed_ms(start)
            result["stdout"] = capture.getvalue()
            if capture.truncated:
                result["truncated"] = True
                result["stdout"] += (f"\n… output truncated: showing the first {capture.kept:,} of "
                                     f"{capture.total:,} characters\n")
            result["stdin_exhausted"] = interp is not None and interp.stdin_exhausted
    return result


# Tokens, syntax trees and native code

def tokens(code: str) -> dict[str, Any]:
    """The tokens (text is what jarlang tokens prints), or the tokens so far plus the error."""
    from .lexer import Lexer

    start = time.perf_counter()
    source = _source(code)
    lexer = Lexer(source)
    out: dict[str, Any] = {"ok": True, "stderr": "", "diagnostics": []}
    try:
        toks = _analyze(lexer.tokenize)
    except Exception as e:  # noqa: BLE001
        toks = lexer.tokens
        out.update(_failure(e))
    rows = []
    lines = []
    for tok in toks:
        line, col = source.line_col(tok.span.start)
        end_line, end_col = source.line_col(tok.span.end)
        rows.append({"kind": tok.kind.name, "text": tok.text, "line": line, "column": col,
                     "end_line": end_line, "end_column": end_col})
        lines.append(f"{line:>4}:{col:<4} {tok.kind.name:<14} {tok.text!r}")
    comments = []
    for c in lexer.comments:
        line, col = source.line_col(c.span.start)
        comments.append({"text": c.text, "line": line, "column": col})
    out.update(tokens=rows, comments=comments, text="\n".join(lines), time_ms=_elapsed_ms(start))
    return out


def _parse(code: str):
    from .parser import parse

    return parse(_source(code))


def ast_tree(code: str) -> dict[str, Any]:
    """The syntax tree as text (what jarlang ast prints)."""
    from .tree import render_tree

    start = time.perf_counter()
    try:
        tree = _analyze(lambda: render_tree(_parse(code), color=False))
    except Exception as e:  # noqa: BLE001
        return _failure(e, tree="", time_ms=_elapsed_ms(start))
    return {"ok": True, "tree": tree, "stderr": "", "diagnostics": [], "time_ms": _elapsed_ms(start)}


def ast_json(code: str) -> dict[str, Any]:
    """The syntax tree as nested JSON objects."""
    from .tree import to_json

    start = time.perf_counter()
    try:
        tree = _analyze(lambda: _jsonable(to_json(_parse(code))))
    except Exception as e:  # noqa: BLE001
        return _failure(e, ast=None, time_ms=_elapsed_ms(start))
    return {"ok": True, "ast": tree, "stderr": "", "diagnostics": [], "time_ms": _elapsed_ms(start)}


NATIVE_NOTE = "This program uses features the native compiler doesn't support. It still runs with the interpreter."


def _native_failure(err: BaseException, start: float, **extra: Any) -> dict[str, Any]:
    out = _failure(err, time_ms=_elapsed_ms(start), **extra)
    # Syntax and name errors are the program's fault, not the native compiler's
    if isinstance(err, JarLangError) and err.kind not in ("syntax error", "name error"):
        out["note"] = NATIVE_NOTE
    return out


def asm(code: str, target: str = "linux", opt: bool = True) -> dict[str, Any]:
    """The x86-64 assembly code (AT&T syntax) that jarlang build would assemble."""
    from .native.driver import compile_to_asm

    start = time.perf_counter()
    if target not in TARGETS:
        message = f"unknown target {target!r} (choose one of: {', '.join(TARGETS)})"
        return {"ok": False, "asm": "", "target": target, "error_kind": "usage", "stderr": "error: " + message,
                "diagnostics": [_plain_diagnostic(message, "error: " + message)], "time_ms": 0.0}
    try:
        text = _analyze(lambda: compile_to_asm(_source(code), target=target, opt=bool(opt)))
    except Exception as e:  # noqa: BLE001
        return _native_failure(e, start, asm="", target=target)
    return {"ok": True, "asm": text, "target": target, "stderr": "", "diagnostics": [],
            "lines": text.count("\n") + (0 if text.endswith("\n") else 1), "time_ms": _elapsed_ms(start)}


def explain(code: str) -> dict[str, Any]:
    """The types the native compiler works out for each global and function."""
    from .native.driver import explain as native_explain

    start = time.perf_counter()
    try:
        text = _analyze(lambda: native_explain(_source(code), color=False))
    except Exception as e:  # noqa: BLE001
        return _native_failure(e, start, text="")
    return {"ok": True, "text": text, "stderr": "", "diagnostics": [], "time_ms": _elapsed_ms(start)}


# Editor support

def check(code: str, native: bool = False) -> dict[str, Any]:
    """Errors and warnings without running the program (ok is false if there are errors)."""
    from .analysis import check_source

    start = time.perf_counter()
    try:
        diags = _analyze(lambda: check_source(_source(code), native=bool(native)))
    except Exception as e:  # noqa: BLE001
        failed = _failure(e, time_ms=_elapsed_ms(start))
        if not failed["diagnostics"]:
            first = failed["stderr"].splitlines()[0]
            failed["diagnostics"] = [_plain_diagnostic(first.removeprefix("error: "), failed["stderr"])]
        failed.update(errors=len(failed["diagnostics"]), warnings=0)
        return failed
    items = [diagnostic_dict(d) for d in diags]
    errors = sum(1 for d in items if d["severity"] == "error")
    return {"ok": errors == 0, "diagnostics": items, "errors": errors,
            "warnings": len(items) - errors, "time_ms": _elapsed_ms(start)}


def format(code: str) -> dict[str, Any]:  # noqa: A001 - mirrors `jarlang fmt`
    """Format the code like jarlang fmt."""
    from .formatter import format_source

    start = time.perf_counter()
    try:
        formatted = _analyze(lambda: format_source(_source(code)))
    except Exception as e:  # noqa: BLE001
        return _failure(e, code=code, time_ms=_elapsed_ms(start))
    return {"ok": True, "code": formatted, "changed": formatted != code,
            "stderr": "", "diagnostics": [], "time_ms": _elapsed_ms(start)}


# Examples and info

def examples_dir() -> Path:
    """The examples folder next to the package (the playground bundle keeps the same layout)."""
    return Path(__file__).resolve().parent.parent / "examples"


def _title(stem: str) -> str:
    text = " ".join(stem.replace("-", " ").replace("_", " ").split())
    return text[:1].upper() + text[1:] if text else stem


def _description(code: str) -> str:
    """The program's first comment line, if it starts with one."""
    for line in code.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        break
    return ""


def examples(directory: str | Path | None = None) -> list[dict[str, str]]:
    """Example programs as a list of dicts, with hello first. Used to build the playground bundle."""
    folder = Path(directory) if directory is not None else examples_dir()
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.jlang")):
        try:
            code = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        except (OSError, UnicodeDecodeError):
            continue
        out.append({"name": path.stem, "title": _title(path.stem), "description": _description(code),
                    "code": code})
    out.sort(key=lambda e: (e["name"] != "hello", e["name"]))
    return out


def info() -> dict[str, Any]:
    """Versions and limits, shown in the playground's version tooltip."""
    return {
        "version": __version__,
        "python": sys.version.split()[0],
        "in_browser": IN_BROWSER,
        "max_depth": MAX_DEPTH,
        "max_output": MAX_OUTPUT,
        "targets": list(TARGETS),
    }


def language() -> dict[str, list[str]]:
    """Keywords, builtins and constants for syntax highlighting."""
    from .builtins import BUILTINS, CONSTANTS
    from .tokens import KEYWORDS
    from .values import is_callable

    return {
        "keywords": sorted(KEYWORDS),
        "builtins": sorted(k for k, v in BUILTINS.items() if is_callable(v)),
        "constants": sorted(CONSTANTS),
    }


# JSON bridge for JavaScript

def _jsonable(value: Any) -> Any:
    """Make value safe for strict JSON (no NaN, infinity, huge ints or other types)."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if abs(value) <= 2**53 else str(value)
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


COMMANDS: dict[str, Callable[..., Any]] = {
    "run": run, "tokens": tokens, "ast_tree": ast_tree, "ast_json": ast_json, "asm": asm,
    "explain": explain, "check": check, "format": format, "info": info,
}


def dispatch(request: str) -> str:
    """Run a command from a JSON request {"fn": name, "args": {...}} and return a JSON reply."""
    try:
        payload = json.loads(request)
        if not isinstance(payload, dict):
            raise ValueError("the request must be a JSON object")
        name = payload.get("fn")
        args = payload.get("args") or {}
        fn = COMMANDS.get(name)
        if fn is None:
            raise ValueError(f"unknown command {name!r}")
        if not isinstance(args, dict):
            raise ValueError("args must be an object")
        if "code" in args and not isinstance(args["code"], str):
            raise ValueError("code must be a string")
        result = fn(**args)
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "stderr": f"internal error: {type(e).__name__}: {e}", "diagnostics": [],
                  "error_kind": "internal"}
    try:
        with _recursion_limit(ANALYSIS_RECURSION_LIMIT):
            return json.dumps(_jsonable(result), ensure_ascii=False, allow_nan=False)
    except (RecursionError, ValueError) as e:
        return json.dumps({"ok": False, "stderr": f"error: the result could not be sent to the page ({e})",
                           "diagnostics": [], "error_kind": "internal"})


__all__ = ["run", "tokens", "ast_tree", "ast_json", "asm", "explain", "check", "format", "examples",
           "info", "language", "dispatch", "set_listener", "diagnostic_dict", "OutputLimitExceeded",
           "MAX_DEPTH", "MAX_OUTPUT"]
