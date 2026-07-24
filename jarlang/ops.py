"""Operators like +, ==, and indexing, used when the interpreter can't take a shortcut."""

from __future__ import annotations

import math
import operator
from typing import Any

from .values import BuiltinError, show, type_name

_MAX_POW_BITS = 50_000_000  # don't build integers bigger than about 6 MB
_MAX_REPEAT = 100_000_000  # longest string or list that "ab" * n or [0] * n can build


def _num(v: Any) -> bool:
    t = type(v)
    return t is int or t is float


def _mismatch(op: str, a: Any, b: Any) -> BuiltinError:
    ta, tb = type_name(a), type_name(b)
    helps = []
    if op == "+" and (ta == "str" or tb == "str"):
        helps.append('to build text use interpolation, e.g. "total: {x}", or str(x)')
    if ta == "bool" or tb == "bool":
        helps.append("booleans are not numbers in JarLang, use int(b) to turn one into 0 or 1")
    if ta == "nil" or tb == "nil":
        helps.append("one side is `nil`, did a function forget to return a value?")
    return BuiltinError(f"cannot apply `{op}` to {ta} and {tb}", *helps)


def add(a: Any, b: Any) -> Any:
    if _num(a) and _num(b):
        return a + b
    ta, tb = type(a), type(b)
    if ta is str and tb is str:
        return a + b
    if ta is list and tb is list:
        return a + b
    raise _mismatch("+", a, b)


def sub(a: Any, b: Any) -> Any:
    if _num(a) and _num(b):
        return a - b
    raise _mismatch("-", a, b)


def mul(a: Any, b: Any) -> Any:
    if _num(a) and _num(b):
        return a * b
    ta, tb = type(a), type(b)
    if ta is int and (tb is str or tb is list):
        a, b = b, a
        ta, tb = tb, ta
    if (ta is str or ta is list) and tb is int:
        if len(a) * b > _MAX_REPEAT:
            raise BuiltinError(f"result of `*` is too large ({len(a) * b:,} items)")
        return a * b
    raise _mismatch("*", a, b)


def div(a: Any, b: Any) -> Any:
    if _num(a) and _num(b):
        if b == 0:
            raise BuiltinError("division by zero")
        try:
            return a / b
        except OverflowError:
            raise BuiltinError("integer too large to convert to float",
                               "use `//` for integer division") from None
    raise _mismatch("/", a, b)


def floordiv(a: Any, b: Any) -> Any:
    if _num(a) and _num(b):
        if b == 0:
            raise BuiltinError("division by zero")
        return a // b
    raise _mismatch("//", a, b)


def mod(a: Any, b: Any) -> Any:
    if _num(a) and _num(b):
        if b == 0:
            raise BuiltinError("modulo by zero")
        return a % b
    raise _mismatch("%", a, b)


def power(a: Any, b: Any) -> Any:
    if not (_num(a) and _num(b)):
        raise _mismatch("^", a, b)
    if type(a) is int and type(b) is int:
        if b >= 0:
            if abs(a) > 1 and b * max(1, abs(a).bit_length()) > _MAX_POW_BITS:
                raise BuiltinError(f"result of {a}^{b} is too large",
                                   "use a float base (e.g. 2.0^n) for an approximate answer")
            return a ** b
        if a == 0:
            raise BuiltinError("0 cannot be raised to a negative power")
    elif a == 0 and b < 0:
        raise BuiltinError("0 cannot be raised to a negative power")
    elif a < 0 and type(b) is float and math.isfinite(b) and not b.is_integer():
        raise BuiltinError("cannot raise a negative number to a fractional power",
                           "the result would be a complex number")
    try:
        base = float(a)
    except OverflowError:
        raise BuiltinError("integer too large to convert to float") from None
    try:
        return base ** b
    except OverflowError:
        raise BuiltinError("numeric overflow in `^`") from None


def factorial(a: Any) -> Any:
    t = type(a)
    if t is int:
        if a < 0:
            raise BuiltinError(f"factorial of a negative number ({a})")
        if a > 100_000:
            raise BuiltinError(f"{a}! is too large to compute")
        return math.factorial(a)
    if t is float:
        if a.is_integer() and a >= 0:
            return float(math.factorial(int(a))) if a <= 170 else math.inf
        try:
            return math.gamma(a + 1)
        except (ValueError, OverflowError):
            raise BuiltinError(f"factorial is undefined for {show(a)}") from None
    raise BuiltinError(f"cannot take the factorial of {type_name(a)}")


def sqrt(a: Any) -> float:
    if not _num(a):
        raise BuiltinError(f"cannot take the square root of {type_name(a)}")
    if a < 0:
        raise BuiltinError(f"square root of a negative number ({show(a)})")
    try:
        return math.sqrt(a)
    except OverflowError:
        raise BuiltinError("integer too large to convert to float") from None


def neg(a: Any) -> Any:
    if _num(a):
        return -a
    raise BuiltinError(f"cannot negate {type_name(a)}")


def pos(a: Any) -> Any:
    if _num(a):
        return a
    raise BuiltinError(f"unary `+` needs a number, found {type_name(a)}")


BINARY = {"+": add, "-": sub, "*": mul, "/": div, "//": floordiv, "%": mod, "^": power}


# Comparison

def eq(a: Any, b: Any) -> bool:
    ta, tb = type(a), type(b)
    # Python's == would say [true] == [1], so compare the items with JarLang's rules
    if ta is list and tb is list:
        return a is b or len(a) == len(b) and all(eq(x, y) for x, y in zip(a, b))
    if ta is dict and tb is dict:
        return a is b or len(a) == len(b) and all(k in b and eq(v, b[k]) for k, v in a.items())
    if (ta is bool) != (tb is bool):
        return False
    return a == b


_ORDER = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}


def _compare(a: Any, b: Any, op: str) -> bool:
    ta, tb = type(a), type(b)
    if ta is list and tb is list:
        # Like Python, the first items that differ decide, then the lengths
        for x, y in zip(a, b):
            if not eq(x, y):
                return _compare(x, y, op)
        return _ORDER[op](len(a), len(b))
    if not (_num(a) and _num(b) or ta is str and tb is str):
        raise BuiltinError(f"cannot compare {type_name(a)} and {type_name(b)} with `{op}`")
    return _ORDER[op](a, b)


def lt(a: Any, b: Any) -> bool:
    return _compare(a, b, "<")


def le(a: Any, b: Any) -> bool:
    return _compare(a, b, "<=")


def gt(a: Any, b: Any) -> bool:
    return _compare(a, b, ">")


def ge(a: Any, b: Any) -> bool:
    return _compare(a, b, ">=")


def contains(container: Any, item: Any) -> bool:
    t = type(container)
    if t is str:
        if type(item) is not str:
            raise BuiltinError(f"`in` on a string needs a string, found {type_name(item)}")
        return item in container
    if t is list:
        return any(eq(item, x) for x in container)
    if t is dict:
        try:
            return item in container
        except TypeError:
            return False
    if t is range:
        # 2.0 in 1..3 is true, like 2.0 in [1, 2, 3]
        if type(item) is float and item.is_integer():
            item = int(item)
        return type(item) is int and item in container
    raise BuiltinError(f"`in` needs a list, string, dict or range on the right, found {type_name(container)}")


COMPARE = {"==": eq, "!=": lambda a, b: not eq(a, b), "<": lt, "<=": le, ">": gt, ">=": ge,
           "in": lambda a, b: contains(b, a), "not in": lambda a, b: not contains(b, a)}


# Indexing

def index(obj: Any, key: Any) -> Any:
    t = type(obj)
    if t is list or t is str or t is range:
        if type(key) is not int:
            raise BuiltinError(f"{type_name(obj)} indices must be integers, found {type_name(key)}")
        try:
            return obj[key]
        except IndexError:
            raise BuiltinError(f"index {key} is out of range for {type_name(obj)} of length {len(obj)}") from None
    if t is dict:
        try:
            return obj[key]
        except KeyError:
            raise BuiltinError(f"key {show(key, True)} not found in dict",
                               "use get(d, key, default) to supply a fallback") from None
        except TypeError:
            raise BuiltinError(f"{type_name(key)} cannot be used as a dict key") from None
    raise BuiltinError(f"cannot index into {type_name(obj)}")


def set_index(obj: Any, key: Any, value: Any) -> None:
    t = type(obj)
    if t is list:
        if type(key) is not int:
            raise BuiltinError(f"list indices must be integers, found {type_name(key)}")
        try:
            obj[key] = value
        except IndexError:
            raise BuiltinError(f"index {key} is out of range for list of length {len(obj)}",
                               "use push(list, value) to append") from None
        return
    if t is dict:
        try:
            obj[key] = value
        except TypeError:
            raise BuiltinError(f"{type_name(key)} cannot be used as a dict key") from None
        return
    if t is str:
        raise BuiltinError("strings are immutable", "build a new string instead, e.g. with slices and `+`")
    raise BuiltinError(f"cannot assign into {type_name(obj)}")


def slice_(obj: Any, start: Any, stop: Any, step: Any) -> Any:
    if type(obj) not in (list, str, range):
        raise BuiltinError(f"cannot slice {type_name(obj)}")
    for part in (start, stop, step):
        if part is not None and type(part) is not int:
            raise BuiltinError(f"slice bounds must be integers, found {type_name(part)}")
    if step == 0:
        raise BuiltinError("slice step cannot be zero")
    return obj[start:stop:step]


def make_range(start: Any, end: Any, inclusive: bool) -> range:
    if type(start) is not int or type(end) is not int:
        raise BuiltinError(f"range bounds must be integers, found {type_name(start)}..{type_name(end)}",
                           "for evenly spaced floats use linspace(a, b, n)")
    return range(start, end + 1 if inclusive else end)


def hashable_key(key: Any) -> Any:
    try:
        hash(key)
    except TypeError:
        raise BuiltinError(f"{type_name(key)} cannot be used as a dict key") from None
    return key
