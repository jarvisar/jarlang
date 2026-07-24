"""Checks code for errors without running it (used by jarlang check and the language server)."""

from __future__ import annotations

from . import ast
from .errors import Diagnostic, JarLangError, MultipleErrors
from .parser import Parser, parse
from .resolver import Resolver
from .source import Source
from .tokens import Token


def analyze(source: Source, tokens: list[Token] | None = None
            ) -> tuple[ast.Program | None, Resolver | None, list[Diagnostic]]:
    """Parse and resolve the code, returning all errors instead of raising them.

    Pass tokens if the code was already lexed, to skip lexing it again.
    """
    try:
        program = parse(source) if tokens is None else Parser(tokens, source).parse_program()
    except MultipleErrors as e:
        return None, None, list(e.diagnostics)
    except JarLangError as e:
        return None, None, [e.diagnostic]
    resolver = resolve(program, source.name)
    return program, resolver, list(resolver.diagnostics)


def resolve(program: ast.Program, filename: str) -> Resolver:
    """Resolve names like the interpreter does, including the names imported from other files."""
    from .interpreter import Interpreter
    resolver = Resolver(filename=filename, module_names=Interpreter()._module_names(filename),
                        known_globals=["argv"])
    resolver.resolve_program(program)
    return resolver


def check_source(source: Source, native: bool = False) -> list[Diagnostic]:
    program, resolver, diags = analyze(source)
    if native and program is not None and resolver is not None and not resolver.errors:
        from .native.typecheck import typecheck
        try:
            typecheck(program)
        except MultipleErrors as e:
            diags.extend(e.diagnostics)
        except JarLangError as e:
            diags.append(e.diagnostic)
    return sorted(diags, key=lambda d: (d.span.start if d.span else -1))
