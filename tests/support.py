"""Shared helpers for the tests."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
GOLDEN = Path(__file__).resolve().parent / "golden"

sys.path.insert(0, str(ROOT))

from jarlang.errors import JarLangError  # noqa: E402
from jarlang.interpreter import Interpreter  # noqa: E402
from jarlang.source import Source  # noqa: E402


# Run code with the interpreter and return what it printed
def run_program(code: str, name: str = "<test>", seed: int | None = 0) -> str:
    out = io.StringIO()
    Interpreter(stdout=out, seed=seed).run(Source(code, name))
    return out.getvalue()


# Run code that should fail and return the error
def run_error(code: str) -> JarLangError:
    out = io.StringIO()
    with pytest.raises(JarLangError) as info:
        Interpreter(stdout=out).run(Source(code, "<test>"))
    return info.value


def evaluate(code: str):
    return Interpreter(stdout=io.StringIO()).run(Source(code, "<test>"))


_toolchain_cache: dict[str, object] = {}


# Find the C toolchain for native tests, or None
def native_toolchain():
    if "tc" not in _toolchain_cache:
        from jarlang.native.toolchain import ToolchainError, find_toolchain, host_is_x86_64
        try:
            tc = find_toolchain() if host_is_x86_64() else None
        except ToolchainError:
            tc = None
        if os.environ.get("JARLANG_SKIP_NATIVE"):
            tc = None
        _toolchain_cache["tc"] = tc
    return _toolchain_cache["tc"]


# Skip the test if there is no C toolchain
def requires_native():
    if native_toolchain() is None:
        pytest.skip("no C toolchain available for native compilation")
