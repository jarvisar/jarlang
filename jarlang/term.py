"""Helpers for printing colors in the terminal (turned off by NO_COLOR)."""

from __future__ import annotations

import os
import sys
from typing import TextIO

_CODES = {
    "reset": "0",
    "bold": "1",
    "dim": "2",
    "italic": "3",
    "underline": "4",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "white": "37",
    "gray": "90",
    "bright_red": "91",
    "bright_green": "92",
    "bright_yellow": "93",
    "bright_blue": "94",
    "bright_magenta": "95",
    "bright_cyan": "96",
}

_vt_enabled = False


def _enable_windows_vt() -> None:
    """Turn on color codes in older Windows consoles."""
    global _vt_enabled
    if _vt_enabled or os.name != "nt":
        return
    _vt_enabled = True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def supports_color(stream: TextIO | None = None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR") or os.environ.get("JARLANG_COLOR") == "always":
        return True
    stream = stream or sys.stdout
    try:
        is_tty = stream.isatty()
    except Exception:
        return False
    if is_tty:
        _enable_windows_vt()
    return is_tty


def style(text: str, spec: str, enabled: bool = True) -> str:
    """Add color codes to text, like style("x", "bold red")."""
    if not enabled or not spec:
        return text
    codes = ";".join(_CODES[s] for s in spec.split())
    return f"\x1b[{codes}m{text}\x1b[0m"


def configure_utf8() -> None:
    """Use UTF-8 for stdin, stdout and stderr, since Windows defaults to the ANSI code page for pipes."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
