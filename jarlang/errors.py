"""Error and warning messages, and printing them with the line of code they point to."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Iterable

from .source import Span
from .term import style


@dataclass
class Label:
    span: Span
    message: str = ""


@dataclass
class Diagnostic:
    severity: str  # "error" | "warning" | "note"
    message: str
    span: Span | None = None
    label: str = ""
    notes: list[str] = field(default_factory=list)
    helps: list[str] = field(default_factory=list)
    secondary: list[Label] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    code: str = ""

    def render(self, color: bool = False) -> str:
        return render_diagnostic(self, color)


class JarLangError(Exception):
    """Base class for every error JarLang reports to users."""

    kind = "error"

    def __init__(self, message: str, span: Span | None = None, label: str = "",
                 *, helps: Iterable[str] = (), notes: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.diagnostic = Diagnostic("error", message, span, label, list(notes), list(helps))

    @property
    def message(self) -> str:
        return self.diagnostic.message

    @property
    def span(self) -> Span | None:
        return self.diagnostic.span

    def render(self, color: bool = False) -> str:
        return self.diagnostic.render(color)


class LexError(JarLangError):
    kind = "syntax error"


class ParseError(JarLangError):
    kind = "syntax error"

    def __init__(self, *args, incomplete: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # True if the input ended too early, so the REPL can ask for another line
        self.incomplete = incomplete


class ResolveError(JarLangError):
    kind = "name error"


class JLRuntimeError(JarLangError):
    kind = "runtime error"


class MultipleErrors(JarLangError):
    """Raised when a pass finds more than one error."""

    def __init__(self, diagnostics: list[Diagnostic]) -> None:
        first = diagnostics[0]
        super().__init__(first.message, first.span, first.label)
        self.diagnostic = first
        self.diagnostics = diagnostics

    def render(self, color: bool = False) -> str:
        return "\n".join(d.render(color) for d in self.diagnostics)


def arity_message(name: str, lo: int, hi: int | None, n: int) -> str:
    if hi is None:
        expected = f"at least {lo}"
    elif lo == hi:
        expected = str(lo)
    else:
        expected = f"{lo} to {hi}"
    plural = "" if expected == "1" else "s"
    return f"`{name}` takes {expected} argument{plural} but {n} {'was' if n == 1 else 'were'} given"


def suggest(name: str, candidates: Iterable[str]) -> str | None:
    """Return the closest match to name, for "did you mean" hints."""
    pool = [c for c in candidates if c and not c.startswith("__")]
    matches = difflib.get_close_matches(name, pool, n=1, cutoff=0.6)
    if matches:
        return matches[0]
    lowered = {c.lower(): c for c in pool}
    return lowered.get(name.lower())


_SEVERITY_STYLE = {"error": "bold bright_red", "warning": "bold bright_yellow", "note": "bold bright_cyan"}


def render_diagnostic(d: Diagnostic, color: bool = False) -> str:
    def s(text: str, spec: str) -> str:
        return style(text, spec, color)

    sev_style = _SEVERITY_STYLE.get(d.severity, "bold")
    head = s(d.severity, sev_style)
    out = [f"{head}{s(': ' + d.message, 'bold')}"]

    if d.span is None:
        for note in d.notes:
            out.append(f"  {s('= note:', 'bold')} {note}")
        for help_ in d.helps:
            out.append(f"  {s('= help:', 'bold bright_cyan')} {help_}")
        out.extend(f"  {line}" for line in d.trace)
        return "\n".join(out)

    src = d.span.source
    line, col = src.line_col(d.span.start)
    labels = [(d.span, d.label, True)] + [(lab.span, lab.message, False) for lab in d.secondary]
    labels = [lab for lab in labels if lab[0].source is src]
    lines_needed = sorted({src.line_col(sp.start)[0] for sp, _, _ in labels})
    gutter = len(str(max(lines_needed)))
    bar = s("|", "bold bright_blue")
    pad = " " * gutter

    out.append(f"{pad}{s('-->', 'bold bright_blue')} {src.name}:{line}:{col}")
    out.append(f"{pad} {bar}")
    previous = None
    for ln in lines_needed:
        if previous is not None and ln > previous + 1:
            out.append(s("...", "bold bright_blue"))
        raw = src.line_text(ln)
        out.append(f"{s(str(ln).rjust(gutter), 'bold bright_blue')} {bar} {_expand_tabs(raw)}")
        for sp, message, primary in labels:
            sl, sc = src.line_col(sp.start)
            if sl != ln:
                continue
            el, ec = src.line_col(max(sp.end, sp.start + 1))
            end_col = ec if el == sl else len(raw) + 1
            # Tabs are shown as 4 spaces, so measure the marker on the expanded text
            before = len(_expand_tabs(raw[:sc - 1]))
            width = max(1, len(_expand_tabs(raw[:end_col - 1])) - before)
            marker = ("^" if primary else "-") * width
            mark_style = sev_style if primary else "bold bright_blue"
            underline = " " * before + s(marker, mark_style)
            if message:
                underline += " " + s(message, mark_style)
            out.append(f"{pad} {bar} {underline}")
        previous = ln

    if d.notes or d.helps or d.trace:
        out.append(f"{pad} {bar}")
    for note in d.notes:
        out.append(f"{pad} {s('= note:', 'bold')} {note}")
    for help_ in d.helps:
        out.append(f"{pad} {s('= help:', 'bold bright_cyan')} {help_}")
    for frame in d.trace:
        out.append(f"{pad} {s('= trace:', 'bold')} {frame}")
    return "\n".join(out)


def _expand_tabs(text: str) -> str:
    return text.replace("\t", "    ")
