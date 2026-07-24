"""Finds a C compiler to assemble and link the generated code."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import platform
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

RUNTIME_C = Path(__file__).with_name("runtime.c")


class ToolchainError(Exception):
    pass


def host_os() -> str:
    system = platform.system()
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    return "linux"


def host_is_x86_64() -> bool:
    return platform.machine().lower() in ("x86_64", "amd64", "x64")


@dataclass
class Toolchain:
    name: str
    command: list[str]
    kind: str  # gcc, clang, zig, or wsl
    version: str = ""
    extra: list[str] = field(default_factory=list)

    def target_os(self, requested: str | None) -> str:
        if self.kind == "wsl":
            return "linux"
        if requested:
            return requested
        return host_os()

    def target_flags(self, target: str) -> list[str]:
        if self.kind == "zig":
            triple = {"windows": "x86_64-windows-gnu", "linux": "x86_64-linux-musl",
                      "macos": "x86_64-macos"}[target]
            return ["-target", triple]
        if host_os() == "macos" and self.kind in ("clang", "gcc") and not host_is_x86_64():
            return ["-arch", "x86_64"]
        return []

    def can_target(self, target: str) -> bool:
        if self.kind == "zig":
            return True
        if self.kind == "wsl":
            return target == "linux"
        return target == host_os()

    def run_prefix(self) -> list[str]:
        return ["wsl"] if self.kind == "wsl" else []

    def path(self, p: str | Path) -> str:
        """Convert a Windows path for WSL (C:\\x becomes /mnt/c/x)."""
        p = str(p)
        if self.kind != "wsl":
            return p
        p = os.path.abspath(p)
        drive, rest = os.path.splitdrive(p)
        return f"/mnt/{drive[0].lower()}{rest.replace(chr(92), '/')}" if drive else p.replace("\\", "/")


def _probe(command: list[str], timeout: float = 60) -> str | None:
    try:
        proc = subprocess.run(command + ["--version"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    first = (proc.stdout or proc.stderr).strip().splitlines()
    return first[0] if first else "unknown version"


def _kind_of(command: list[str], version: str) -> str:
    joined = " ".join(command).lower()
    if "zig" in joined:
        return "zig"
    if "clang" in joined or "clang" in version.lower():
        return "clang"
    return "gcc"


def _candidates() -> list[list[str]]:
    cands: list[list[str]] = []
    for name in ("gcc", "clang", "cc"):
        if shutil.which(name):
            cands.append([name])
    if shutil.which("zig"):
        cands.append(["zig", "cc"])
    if importlib.util.find_spec("ziglang") is not None:
        cands.append([sys.executable, "-m", "ziglang", "cc"])
    return cands


@lru_cache(maxsize=None)
def _probe_cached(command: tuple[str, ...]) -> str | None:
    return _probe(list(command))


def _wsl_toolchain() -> Toolchain | None:
    if host_os() != "windows" or not shutil.which("wsl"):
        return None
    try:
        proc = subprocess.run(["wsl", "-e", "gcc", "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return Toolchain("gcc (WSL)", ["wsl", "-e", "gcc"], "wsl", proc.stdout.splitlines()[0] if proc.stdout else "")


# Try --cc or JARLANG_CC first, then gcc, clang, cc, zig, and finally gcc inside WSL
def find_toolchain(cc: str | None = None, target: str | None = None) -> Toolchain:
    spec = cc or os.environ.get("JARLANG_CC")
    if spec:
        command = shlex.split(spec, posix=os.name != "nt")
        version = _probe_cached(tuple(command))
        if version is None:
            raise ToolchainError(f"the C compiler `{spec}` could not be run")
        kind = _kind_of(command, version)
        return Toolchain(spec, command, kind, version)
    for command in _candidates():
        version = _probe_cached(tuple(command))
        if version is None:
            continue
        kind = _kind_of(command, version)
        tc = Toolchain(" ".join(command) if command[0] != sys.executable else "zig cc (ziglang package)",
                       command, kind, version)
        if target is None or tc.can_target(target):
            return tc
    wsl = _wsl_toolchain()
    if wsl is not None and (target in (None, "linux")):
        return wsl
    raise ToolchainError(
        "no C toolchain found for native compilation.\n"
        "  The easiest fix on any platform:   pip install ziglang\n"
        "  Or install gcc/clang (Linux: apt install gcc; macOS: xcode-select --install;\n"
        "  Windows: MSYS2/MinGW-w64), then run `jarlang doctor`.")


def describe_toolchains() -> list[dict]:
    rows = []
    seen = set()
    for command in _candidates():
        key = tuple(command)
        if key in seen:
            continue
        seen.add(key)
        version = _probe_cached(key)
        name = "zig cc (ziglang package)" if command[0] == sys.executable else " ".join(command)
        rows.append({"name": name, "ok": version is not None, "detail": version or "failed to run"})
    for name in ("gcc", "clang", "zig"):
        if not any(r["name"].startswith(name) for r in rows):
            rows.append({"name": name, "ok": False, "detail": "not found on PATH"})
    if host_os() == "windows":
        wsl = _wsl_toolchain()
        rows.append({"name": "gcc (WSL)", "ok": wsl is not None,
                     "detail": wsl.version if wsl else "unavailable"})
    return rows


def cache_dir() -> Path:
    base = os.environ.get("JARLANG_CACHE")
    if base:
        path = Path(base)
    elif host_os() == "windows":
        path = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "jarlang" / "cache"
    else:
        path = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "jarlang"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run(command: list[str], what: str) -> None:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except OSError as exc:
        raise ToolchainError(f"{what} failed: {exc}") from None
    except subprocess.TimeoutExpired:
        raise ToolchainError(f"{what} timed out") from None
    if proc.returncode != 0:
        output = (proc.stderr or proc.stdout).strip()
        raise ToolchainError(f"{what} failed:\n  $ {' '.join(command)}\n{output}")


def runtime_object(tc: Toolchain, target: str) -> Path:
    """Compile runtime.c once for each toolchain and target, and return the object file."""
    source = RUNTIME_C.read_bytes()
    key = hashlib.sha256(source + repr((tc.command, tc.version, target)).encode()).hexdigest()[:16]
    obj = cache_dir() / f"runtime-{target}-{key}.o"
    if obj.exists():
        return obj
    tmp = obj.with_suffix(f".{os.getpid()}.tmp.o")
    _run(tc.command + tc.target_flags(target) + ["-O2", "-c", tc.path(RUNTIME_C), "-o", tc.path(tmp)],
         "compiling the JarLang runtime")
    os.replace(tmp, obj)
    return obj


def link(tc: Toolchain, target: str, asm_path: Path, output: Path) -> None:
    obj = runtime_object(tc, target)
    command = tc.command + tc.target_flags(target) + ["-o", tc.path(output), tc.path(asm_path), tc.path(obj)]
    if target != "windows":
        command.append("-lm")
    _run(command, "assembling and linking")
