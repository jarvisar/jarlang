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


@dataclass(frozen=True)
class TupleT:
    """A list whose items have different types, e.g. ["apples", 3, 0.5]. Its length never changes."""

    elems: tuple["Type", ...]

    def __str__(self) -> str:
        return "[" + ", ".join(show_type(e) for e in self.elems) + "]"


@dataclass(frozen=True)
class DictT:
    key: "Type"
    val: "Type"

    def __str__(self) -> str:
        return f"dict[{show_type(self.key)}, {show_type(self.val)}]"


# None means the type is not known yet
Type = Union[str, ListT, TupleT, DictT, None]

NUMERIC = (INT, FLOAT)
# Types that can be dict keys
KEY_TYPES = (INT, FLOAT, BOOL, STR)


class TypeConflict(Exception):
    pass


class NumericConflict(TypeConflict):
    """An int mixed with a float where only one type can be stored."""


def show_type(t: Type) -> str:
    if t is None:
        return "?"
    return str(t)


def is_container(t: Type) -> bool:
    return isinstance(t, (ListT, TupleT, DictT))


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
        if is_container(a.elem) and is_container(b.elem):
            return ListT(join(a.elem, b.elem))
        raise TypeConflict(f"lists with different element types ({show_type(a)} and {show_type(b)})")
    if isinstance(a, TupleT) and isinstance(b, TupleT) and len(a.elems) == len(b.elems):
        return TupleT(tuple(join(x, y) for x, y in zip(a.elems, b.elems)))
    if isinstance(a, DictT) and isinstance(b, DictT):
        return DictT(join(a.key, b.key), join(a.val, b.val))
    raise TypeConflict(f"{show_type(a)} and {show_type(b)}")


def list_of(types: list[Type]) -> Type:
    """The type of a list literal with items of these types: a list[T], or a TupleT if the types differ."""
    elem: Type = None
    try:
        for t in types:
            elem = join(elem, t)
    except TypeConflict:
        return TupleT(tuple(types))
    return ListT(elem)


def is_complete(t: Type) -> bool:
    if t is None:
        return False
    if isinstance(t, ListT):
        return is_complete(t.elem)
    if isinstance(t, TupleT):
        return all(is_complete(e) for e in t.elems)
    if isinstance(t, DictT):
        return is_complete(t.key) and is_complete(t.val)
    return True


def is_float(t: Type) -> bool:
    return t == FLOAT


def in_rax(t: Type) -> bool:
    """Floats are stored in %xmm0, everything else in %rax."""
    return t != FLOAT


def descriptor(t: Type) -> str:
    """Type code passed to the runtime for printing (see sb_value in runtime.c)."""
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
    if isinstance(t, TupleT):
        return "T" + "".join(descriptor(e) for e in t.elems) + ")"
    if isinstance(t, DictT):
        return "D" + descriptor(t.key if t.key is not None else INT) + descriptor(t.val if t.val is not None else INT)
    return "i"


def key_kind(t: Type) -> int:
    """How the runtime hashes and compares dict keys of type t."""
    return ord(descriptor(t if t is not None else INT))


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
    if isinstance(t, TupleT):
        return "T" + "".join(mangle_type(e) for e in t.elems) + "_"
    if isinstance(t, DictT):
        return "D" + mangle_type(t.key) + mangle_type(t.val)
    return "x"
