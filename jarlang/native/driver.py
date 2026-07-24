"""Compiles and runs code with the native compiler."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ..errors import MultipleErrors
from ..parser import parse
from ..resolver import Resolver
from ..source import Source
from ..term import style
from .codegen import CodeGen, Target
from .peephole import optimize as peephole
from .toolchain import ToolchainError, find_toolchain, host_is_x86_64, host_os, link
from .typecheck import TypeChecker, typecheck


@dataclass
class NativeResult:
    stdout: str
    stderr: str
    returncode: int
    compile_time: float
    run_time: float
    asm: str = ""


def analyze(source: Source, opt: bool = True) -> TypeChecker:
    program = parse(source)
    resolver = Resolver(filename=source.name, known_globals=["argv"])
    resolver.resolve_program(program)
    if resolver.errors:
        raise MultipleErrors(resolver.errors)
    if opt:
        from ..optimizer import fold_constants
        fold_constants(program)
    user_globals = [name for name, sym in resolver.global_scope.symbols.items() if sym.kind != "function"]
    return typecheck(program, user_globals)


def compile_to_asm(source: Source, target: str | None = None, opt: bool = True) -> str:
    checker = analyze(source, opt)
    gen = CodeGen(checker, Target(target or host_os()), os.path.basename(source.name), source.text)
    asm = gen.generate()
    return peephole(asm) if opt else asm


def build_executable(source: Source, output: str | None = None, cc: str | None = None,
                     target: str | None = None, opt: bool = True, keep_asm: bool = False) -> str:
    tc = find_toolchain(cc, target)
    target_os = tc.target_os(target)
    asm = compile_to_asm(source, target_os, opt)
    if output is None:
        stem = Path(source.name).stem if not source.name.startswith("<") else "a"
        output = stem + (".exe" if target_os == "windows" else "")
    out_path = Path(output).resolve()
    if keep_asm:
        asm_path = out_path.with_suffix(".s")
        asm_path.write_text(asm, encoding="utf-8", newline="\n")
        link(tc, target_os, asm_path, out_path)
    else:
        with tempfile.TemporaryDirectory(prefix="jarlang-") as tmp:
            asm_path = Path(tmp) / "program.s"
            asm_path.write_text(asm, encoding="utf-8", newline="\n")
            link(tc, target_os, asm_path, out_path)
    return str(out_path)


def run_native(source: Source, cc: str | None = None, opt: bool = True, capture: bool = True,
               argv: list[str] | None = None, timeout: float | None = None) -> NativeResult:
    """Compile the code to a temporary executable and run it."""
    start = time.perf_counter()
    tc = find_toolchain(cc)
    target_os = tc.target_os(None)
    if not host_is_x86_64() and host_os() != "macos":
        raise ToolchainError("native executables are x86-64; this machine cannot run them")
    asm = compile_to_asm(source, target_os, opt)
    with tempfile.TemporaryDirectory(prefix="jarlang-") as tmp:
        asm_path = Path(tmp) / "program.s"
        exe = Path(tmp) / ("program.exe" if target_os == "windows" else "program")
        asm_path.write_text(asm, encoding="utf-8", newline="\n")
        link(tc, target_os, asm_path, exe)
        compile_time = time.perf_counter() - start
        command = tc.run_prefix() + [tc.path(exe)] + list(argv or [])
        run_start = time.perf_counter()
        if capture:
            proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout)
            stdout, stderr = proc.stdout, proc.stderr
        else:
            proc = subprocess.run(command, timeout=timeout)
            stdout = stderr = ""
        run_time = time.perf_counter() - run_start
    return NativeResult(stdout, stderr, proc.returncode, compile_time, run_time, asm)


# Printing assembly and types

_REG = re.compile(r"%[a-z0-9]+")
_IMM = re.compile(r"\$-?[0-9a-fx]+")


def highlight_asm(asm: str, color: bool) -> str:
    if not color:
        return asm
    out = []
    for line in asm.split("\n"):
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            spec = "bright_green" if re.match(r"#\s*\d+:", stripped) else "gray"
            out.append(style(line, spec, True))
        elif stripped.endswith(":"):
            out.append(style(line, "bold bright_yellow", True))
        elif stripped.startswith("."):
            out.append(style(line, "gray", True))
        else:
            indent = line[:len(line) - len(line.lstrip())]
            code, _, comment = stripped.partition("#")
            mnemonic, _, operands = code.partition(" ")
            operands = _REG.sub(lambda m: style(m.group(0), "magenta", True), operands)
            operands = _IMM.sub(lambda m: style(m.group(0), "bright_cyan", True), operands)
            text = indent + style(mnemonic, "bold bright_blue", True) + (" " + operands if operands else "")
            if comment:
                text += style(" #" + comment, "gray", True)
            out.append(text)
    return "\n".join(out)


def explain(source: Source, color: bool = False) -> str:
    """Show the types found by type inference (used by jarlang explain)."""
    checker = analyze(source)

    def s(text: str, spec: str) -> str:
        return style(text, spec, color)

    lines = [s("native type inference for " + source.name, "bold")]
    if checker.globals:
        lines.append(s("globals:", "bold"))
        for name, t in sorted(checker.globals.items()):
            lines.append(f"  {name}: {s(str(t), 'bright_cyan')}")
    lines.append(s("function instances (monomorphized):", "bold"))
    insts = sorted(checker.live_instances, key=lambda i: i.asm_name)
    if not insts:
        lines.append("  (none)")
    for inst in insts:
        params = ", ".join(f"{p.name}: {s(str(t), 'bright_cyan')}" for p, t in zip(inst.func.params, inst.argtypes))
        lines.append(f"  fn {s(inst.name, 'bright_yellow')}({params}) -> {s(str(inst.ret), 'bright_cyan')}"
                     f"   {s('[' + inst.asm_name + ']', 'gray')}")
    return "\n".join(lines)


__all__ = ["compile_to_asm", "build_executable", "run_native", "NativeResult", "highlight_asm", "explain",
           "ToolchainError"]
