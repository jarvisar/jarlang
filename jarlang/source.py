"""Source code and spans (the part of the code each token and node came from)."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

_LONE_CR = re.compile(r"\r(?!\n)")


class Source:
    """A named piece of JarLang code, such as a file or a REPL line."""

    __slots__ = ("name", "text", "_line_starts")

    def __init__(self, text: str, name: str = "<input>") -> None:
        self.name = name
        if "\r" in text:
            # Old Mac line endings. Same length, so positions still match the original text
            text = _LONE_CR.sub("\n", text)
        self.text = text
        starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                starts.append(i + 1)
        self._line_starts = starts

    def line_col(self, offset: int) -> tuple[int, int]:
        """Convert a character position into a (line, column) pair, starting at 1."""
        offset = max(0, min(offset, len(self.text)))
        line = bisect.bisect_right(self._line_starts, offset) - 1
        return line + 1, offset - self._line_starts[line] + 1

    def line_text(self, line: int) -> str:
        """Return the text of a line (starting at 1) without the newline."""
        if line < 1 or line > len(self._line_starts):
            return ""
        start = self._line_starts[line - 1]
        end = self.text.find("\n", start)
        if end == -1:
            end = len(self.text)
        return self.text[start:end].rstrip("\r")

    @property
    def line_count(self) -> int:
        return len(self._line_starts)

    def span(self, start: int, end: int) -> "Span":
        return Span(self, start, end)

    def __repr__(self) -> str:
        return f"Source({self.name!r})"


@dataclass(frozen=True, slots=True)
class Span:
    """A range of characters in a Source, from start up to (not including) end."""

    source: Source
    start: int
    end: int

    @property
    def text(self) -> str:
        return self.source.text[self.start:self.end]

    @property
    def line(self) -> int:
        return self.source.line_col(self.start)[0]

    @property
    def column(self) -> int:
        return self.source.line_col(self.start)[1]

    def to(self, other: "Span") -> "Span":
        """Return a span that covers both spans."""
        return Span(self.source, min(self.start, other.start), max(self.end, other.end))

    def location(self) -> str:
        line, col = self.source.line_col(self.start)
        return f"{self.source.name}:{line}:{col}"

    def __repr__(self) -> str:
        return f"Span({self.location()}, {self.start}..{self.end})"
