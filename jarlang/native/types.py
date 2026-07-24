"""Types used by the native compiler."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

INT = "int"
FLOAT = "float"
BOOL = "bool"
STR = "str"
NIL = "nil"
NEVER = "never"  # no value is produced here (return, break, continue)


@dataclass(frozen=True)
class ListT:
    elem: "Type"

    def __str__(self) -> str:
        return f"list[{show_type(self.elem)}]"


# None means the type is not known yet
Type = Union[str, ListT, None]

NUMERIC = (INT, FLOAT)


class TypeConflict(Exception):
    pass


class NumericConflict(TypeConflict):
    """An int mixed with a float where only one type can be stored."""


def show_type(t: Type) -> str:
    if t is None:
        return "?"
    return str(t)


def join(a: Type, b: Type) -> Type:
    """Return the type that can hold values of both a and b."""
    if a is None or a == NEVER:
        return b
    if b is None or b == NEVER:
        return a
    if a == b:
        return a
    if a in NUMERIC and b in NUMERIC:
        # Don't widen int to float, or 1 would print as 1.0
        raise NumericConflict(f"{show_type(a)} and {show_type(b)}")
    if isinstance(a, ListT) and isinstance(b, ListT):
        if a.elem is None:
            return b
        if b.elem is None:
            return a
        if a.elem == b.elem:
            return a
        if isinstance(a.elem, ListT) and isinstance(b.elem, ListT):
            return ListT(join(a.elem, b.elem))
        raise TypeConflict(f"lists with different element types ({show_type(a)} and {show_type(b)})")
    raise TypeConflict(f"{show_type(a)} and {show_type(b)}")


def is_complete(t: Type) -> bool:
    if t is None:
        return False
    if isinstance(t, ListT):
        return is_complete(t.elem)
    return True


def is_float(t: Type) -> bool:
    return t == FLOAT


def in_rax(t: Type) -> bool:
    """Floats are stored in %xmm0, everything else in %rax."""
    return t != FLOAT


def descriptor(t: Type) -> str:
    """Type code passed to the runtime for printing lists."""
    if t == INT:
        return "i"
    if t == FLOAT:
        return "f"
    if t == BOOL:
        return "b"
    if t == STR:
        return "s"
    if t == NIL:
        return "n"
    if isinstance(t, ListT):
        return "L" + descriptor(t.elem if t.elem is not None else INT)
    return "i"


def mangle_type(t: Type) -> str:
    if t == INT:
        return "i"
    if t == FLOAT:
        return "f"
    if t == BOOL:
        return "b"
    if t == STR:
        return "s"
    if t == NIL:
        return "n"
    if isinstance(t, ListT):
        return "L" + mangle_type(t.elem)
    return "x"
