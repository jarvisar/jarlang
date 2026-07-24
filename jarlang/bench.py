"""Compares the speed and output of the interpreter and native compiler (jarlang bench)."""

from __future__ import annotations

import io
import sys
import time

from .errors import JarLangError
from .interpreter import Interpreter, run_with_big_stack
from .source import Source
from .term import supports_color


def bench_file(path: str, repeat: int = 1, cc: str | None = None) -> int:
    from .native.driver import run_native
    from .native.toolchain import ToolchainError

    try:
        with open(path, encoding="utf-8-sig") as fh:
            source = Source(fh.read(), path)
    except OSError as exc:
        print(f"error: cannot read {path}: {exc.strerror}", file=sys.stderr)
        return 1
    except UnicodeDecodeError:
        print(f"error: {path} is not a UTF-8 text file", file=sys.stderr)
        return 1

    repeat = max(1, repeat)
    interp_times = []
    interp_out = ""
    try:
        for _ in range(repeat):
            out = io.StringIO()
            interp = Interpreter(stdout=out, seed=0)
            compiled = interp.compile(source)
            start = time.perf_counter()
            run_with_big_stack(compiled.run)
            interp_times.append(time.perf_counter() - start)
            interp_out = out.getvalue()
    except JarLangError as e:
        print(e.render(supports_color(sys.stderr)), file=sys.stderr)
        return 1

    native_times = []
    compile_time = 0.0
    native_out = ""
    try:
        for _ in range(repeat):
            result = run_native(source, cc=cc)
            if result.returncode != 0:
                print(result.stderr, file=sys.stderr)
                return 1
            native_times.append(result.run_time)
            compile_time = result.compile_time
            native_out = result.stdout
    except JarLangError as e:
        print(e.render(supports_color(sys.stderr)), file=sys.stderr)
        return 1
    except ToolchainError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    ti, tn = min(interp_times), min(native_times)
    speedup = ti / tn if tn > 0 else float("inf")
    match = interp_out.replace("\r\n", "\n") == native_out.replace("\r\n", "\n")
    best = f" (best of {repeat})" if repeat > 1 else ""
    print(f"{path}{best}")
    print(f"  interpreter: {ti * 1000:.1f} ms")
    print(f"  native: {tn * 1000:.1f} ms (compiled in {compile_time * 1000:.0f} ms)")
    print(f"  speedup: {speedup:.1f}x")
    if match:
        print("  outputs match")
        return 0
    print("  outputs differ")
    import difflib
    diff = difflib.unified_diff(interp_out.splitlines(), native_out.splitlines(), "interpreter", "native",
                                lineterm="", n=1)
    for line in list(diff)[:40]:
        print("    " + line)
    return 1
