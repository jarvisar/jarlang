"""JarLang, a small programming language for math."""

from __future__ import annotations

__version__ = "2.0.0"

from typing import Any


def run(code: str, name: str = "<input>") -> Any:
    """Run a JarLang program and return the value of its final expression."""
    from .interpreter import Interpreter

    return Interpreter().run(code, name)


def evaluate(expression: str) -> Any:
    """Evaluate a JarLang expression and return the resulting Python value."""
    return run(expression)


__all__ = ["run", "evaluate", "__version__"]
