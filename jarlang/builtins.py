"""Built-in functions (each one takes the interpreter, ctx, as its first argument)."""

from __future__ import annotations

import inspect
import math
import random as _random
import statistics
import time
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import Any, Callable, Iterable

from . import ops
from .errors import JarLangError
from .values import Builtin, BuiltinError, Function, is_callable, is_number, show, type_name

BUILTINS: dict[str, Any] = {}

# Math constants (these can't be assigned to, but let pi = 3 shadows them)
PHI = (1 + math.sqrt(5)) / 2
CONSTANTS = {
    "pi": math.pi, "π": math.pi,
    "tau": math.tau, "τ": math.tau,
    "e": math.e,
    "phi": PHI, "φ": PHI,
    "inf": math.inf,
    "nan": math.nan,
}
BUILTINS.update(CONSTANTS)
CONSTANT_DOCS = {
    "pi": "π ≈ 3.14159, the ratio of a circle's circumference to its diameter",
    "tau": "τ = 2π ≈ 6.28318",
    "e": "Euler's number ≈ 2.71828",
    "phi": "the golden ratio φ ≈ 1.61803",
    "inf": "positive infinity",
    "nan": "not-a-number",
}


def builtin(name: str | None = None, *, category: str = "misc", sig: str = ""):
    """Register a Python function as a builtin, working out how many arguments it takes."""

    def deco(fn: Callable) -> Callable:
        params = list(inspect.signature(fn).parameters.values())[1:]  # skip ctx
        lo = sum(1 for p in params if p.default is inspect.Parameter.empty
                 and p.kind is not inspect.Parameter.VAR_POSITIONAL)
        hi: int | None = len(params)
        if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
            hi = None
        jl_name = name or fn.__name__.removeprefix("bi_")
        signature = sig or f"{jl_name}({', '.join(_param_text(p) for p in params)})"
        doc = inspect.cleandoc(fn.__doc__ or "")
        BUILTINS[jl_name] = Builtin(jl_name, fn, lo, hi, doc, signature, category)
        return fn

    return deco


def _param_text(p: inspect.Parameter) -> str:
    if p.kind is inspect.Parameter.VAR_POSITIONAL:
        return f"...{p.name}"
    if p.default is not inspect.Parameter.empty:
        return f"{p.name}={show(p.default, True)}"
    return p.name


# Argument helpers

def _need_num(v: Any, fname: str, what: str = "argument") -> None:
    if not is_number(v):
        raise BuiltinError(f"{fname}() expects a number {what}, found {type_name(v)}")


def _need_int(v: Any, fname: str, what: str = "argument") -> None:
    if type(v) is not int:
        raise BuiltinError(f"{fname}() expects an integer {what}, found {type_name(v)}")


def _need_str(v: Any, fname: str) -> None:
    if type(v) is not str:
        raise BuiltinError(f"{fname}() expects a string, found {type_name(v)}")


def _need_fn(v: Any, fname: str) -> None:
    if not is_callable(v):
        raise BuiltinError(f"{fname}() expects a function, found {type_name(v)}",
                           "pass a function, e.g. fn(x) => x * 2")


def _iterable(v: Any, fname: str) -> Iterable:
    t = type(v)
    if t in (list, range, str):
        return v
    if t is dict:
        return list(v.keys())
    raise BuiltinError(f"{fname}() expects a list, range, string or dict, found {type_name(v)}")


def _seq_and_fn(ctx, a: Any, b: Any, fname: str) -> tuple[Iterable, Any]:
    """Accept both map(xs, f) and map(f, xs)."""
    if is_callable(a) and not is_callable(b):
        a, b = b, a
    _need_fn(b, fname)
    return _iterable(a, fname), b


def _numbers(xs: Iterable, fname: str) -> list:
    out = list(xs)
    for x in out:
        if not is_number(x):
            raise BuiltinError(f"{fname}() expects numbers, found {type_name(x)}")
    return out


def _math1(fname: str, fn: Callable[[float], float], doc: str, domain: str = "") -> None:
    def impl(ctx, x):
        _need_num(x, fname)
        try:
            return fn(x)
        except (ValueError, OverflowError):
            msg = f"{fname}({show(x)}) is undefined"
            raise BuiltinError(msg, domain) if domain else BuiltinError(msg)
    impl.__doc__ = doc
    impl.__name__ = fname
    builtin(fname, category="math")(impl)


# I/O

@builtin(category="io")
def bi_print(ctx, *values):
    """Print values separated by spaces, followed by a newline."""
    ctx.write(" ".join(show(v) for v in values) + "\n")


@builtin(category="io")
def bi_write(ctx, *values):
    """Print values separated by spaces, without a trailing newline."""
    ctx.write(" ".join(show(v) for v in values))


@builtin(category="io")
def bi_input(ctx, prompt=""):
    """Read a line of text from the user."""
    return ctx.read_line(show(prompt))


@builtin(category="io")
def bi_read_file(ctx, path):
    """Return the contents of a text file."""
    _need_str(path, "read_file")
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    except OSError as exc:
        raise BuiltinError(f"cannot read {path!r}: {exc.strerror or exc}") from None
    except UnicodeDecodeError:
        raise BuiltinError(f"cannot read {path!r}: it is not a UTF-8 text file") from None


@builtin(category="io")
def bi_write_file(ctx, path, text):
    """Write text to a file (replacing it)."""
    _need_str(path, "write_file")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(show(text))
    except OSError as exc:
        raise BuiltinError(f"cannot write {path!r}: {exc.strerror or exc}") from None


@builtin(category="io")
def bi_exit(ctx, code=0):
    """Stop the program with an exit code."""
    _need_int(code, "exit")
    raise ProgramExit(code)


class ProgramExit(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


# Conversions

@builtin(category="convert")
def bi_str(ctx, value):
    """Convert any value to its text form."""
    return show(value)


@builtin(category="convert")
def bi_int(ctx, value, base=10):
    """Convert to an integer (floats are truncated, strings can use a `base`)."""
    t = type(value)
    if t is int:
        return value
    if t is bool:
        return int(value)
    if t is float:
        if value != value or value in (math.inf, -math.inf):
            raise BuiltinError(f"cannot convert {show(value)} to int")
        return int(value)
    if t is str:
        _need_int(base, "int", "base")
        if not 2 <= base <= 36:
            raise BuiltinError(f"int() base must be between 2 and 36, found {base}")
        try:
            return int(value.strip().replace("_", ""), base)
        except ValueError:
            raise BuiltinError(f"cannot parse {show(value, True)} as an integer") from None
    raise BuiltinError(f"cannot convert {type_name(value)} to int")


@builtin(category="convert")
def bi_float(ctx, value):
    """Convert to a floating-point number."""
    t = type(value)
    if t in (int, float, bool):
        try:
            return float(value)
        except OverflowError:
            raise BuiltinError("integer too large to convert to float") from None
    if t is str:
        try:
            return float(value.strip().replace("_", ""))
        except ValueError:
            raise BuiltinError(f"cannot parse {show(value, True)} as a number") from None
    raise BuiltinError(f"cannot convert {type_name(value)} to float")


@builtin(category="convert")
def bi_bool(ctx, value):
    """Convert to true or false (nil, false, 0, "", [] and {} are false)."""
    return bool(value)


@builtin(category="convert")
def bi_list(ctx, value=None):
    """Make a list from a range, string, dict (its keys) or list."""
    if value is None:
        return []
    return list(_iterable(value, "list"))


@builtin(category="convert")
def bi_dict(ctx, pairs=None):
    """Make a dict from a list of [key, value] pairs."""
    if pairs is None:
        return {}
    out = {}
    for pair in _iterable(pairs, "dict"):
        if type(pair) is not list or len(pair) != 2:
            raise BuiltinError("dict() expects a list of [key, value] pairs")
        out[ops.hashable_key(pair[0])] = pair[1]
    return out


@builtin(category="convert")
def bi_type(ctx, value):
    """The name of a value's type, e.g. "int" or "list"."""
    return type_name(value)


@builtin(category="convert")
def bi_repr(ctx, value):
    """Text form of a value with strings quoted."""
    return show(value, True)


@builtin(category="convert")
def bi_fmt(ctx, value, spec):
    """Format a value with a format spec, e.g. fmt(pi, ".3f") == "3.142"."""
    return format_value(value, spec)


@builtin(category="convert")
def bi_hex(ctx, n):
    """Hexadecimal text for an integer, e.g. hex(255) == "0xff"."""
    _need_int(n, "hex")
    return hex(n)


@builtin(category="convert")
def bi_bin(ctx, n):
    """Binary text for an integer, e.g. bin(5) == "0b101"."""
    _need_int(n, "bin")
    return bin(n)


def format_value(value: Any, spec: str | None) -> str:
    if not spec:
        return show(value)
    try:
        if type(value) is bool or value is None or not is_number(value):
            return format(show(value), spec)
        return format(value, spec)
    except (ValueError, TypeError, OverflowError):
        raise BuiltinError(f"invalid format spec {spec!r} for {type_name(value)}",
                           "examples: {x:.2f}  {n:>6}  {n:,}  {x:e}  {n:08b}") from None


@builtin(category="misc")
def bi_help(ctx, value=None):
    """Show documentation for a function (or list all builtins)."""
    if value is None:
        by_cat: dict[str, list[str]] = {}
        for name, b in BUILTINS.items():
            if isinstance(b, Builtin):
                by_cat.setdefault(b.category, []).append(name)
        lines = []
        for cat in sorted(by_cat):
            lines.append(f"{cat}: {', '.join(sorted(by_cat[cat]))}")
        ctx.write("\n".join(lines) + "\n")
        return None
    ctx.write(bi_doc(ctx, value) + "\n")
    return None


@builtin(category="misc")
def bi_doc(ctx, value):
    """The documentation text of a function."""
    if isinstance(value, Builtin):
        return f"{value.signature}\n  {value.doc}"
    if isinstance(value, Function):
        return f"fn {value.signature}"
    return f"{show(value, True)} : {type_name(value)}"


# Math

_math1("sin", math.sin, "Sine (radians).")
_math1("cos", math.cos, "Cosine (radians).")
_math1("tan", math.tan, "Tangent (radians).")
_math1("asin", math.asin, "Inverse sine, in radians.", "the input must be between -1 and 1")
_math1("acos", math.acos, "Inverse cosine, in radians.", "the input must be between -1 and 1")
_math1("atan", math.atan, "Inverse tangent, in radians.")
_math1("sinh", math.sinh, "Hyperbolic sine.")
_math1("cosh", math.cosh, "Hyperbolic cosine.")
_math1("tanh", math.tanh, "Hyperbolic tangent.")
_math1("exp", math.exp, "e raised to the power x.")
_math1("ln", math.log, "Natural logarithm.", "the input must be positive")
_math1("log2", math.log2, "Base-2 logarithm.", "the input must be positive")
_math1("log10", math.log10, "Base-10 logarithm.", "the input must be positive")
_math1("cbrt", lambda x: math.copysign(abs(x) ** (1 / 3), x) if not hasattr(math, "cbrt") else math.cbrt(x),
       "Cube root.")
_math1("deg", math.degrees, "Convert radians to degrees.")
_math1("rad", math.radians, "Convert degrees to radians.")


@builtin(category="math")
def bi_sqrt(ctx, x):
    """Square root (also written √x)."""
    return ops.sqrt(x)


@builtin(category="math")
def bi_log(ctx, x, base=None):
    """Natural logarithm, or logarithm in `base` when given."""
    _need_num(x, "log")
    if x <= 0:
        raise BuiltinError(f"log({show(x)}) is undefined", "the input must be positive")
    if base is None:
        return math.log(x)
    _need_num(base, "log", "base")
    if base <= 0 or base == 1:
        raise BuiltinError(f"invalid logarithm base {show(base)}")
    return math.log(x, base)


@builtin(category="math")
def bi_atan2(ctx, y, x):
    """Angle of the point (x, y) from the positive x-axis, in radians."""
    _need_num(y, "atan2")
    _need_num(x, "atan2")
    return math.atan2(y, x)


@builtin(category="math")
def bi_hypot(ctx, x, y):
    """Length of the hypotenuse √(x² + y²)."""
    _need_num(x, "hypot")
    _need_num(y, "hypot")
    return math.hypot(x, y)


@builtin(category="math")
def bi_abs(ctx, x):
    """Absolute value."""
    _need_num(x, "abs")
    return abs(x)


@builtin(category="math")
def bi_sign(ctx, x):
    """-1, 0 or 1 depending on the sign of x."""
    _need_num(x, "sign")
    return (x > 0) - (x < 0)


@builtin(category="math")
def bi_floor(ctx, x):
    """Round down to an integer."""
    _need_num(x, "floor")
    try:
        return math.floor(x)
    except (ValueError, OverflowError):
        raise BuiltinError(f"cannot take floor of {show(x)}") from None


@builtin(category="math")
def bi_ceil(ctx, x):
    """Round up to an integer."""
    _need_num(x, "ceil")
    try:
        return math.ceil(x)
    except (ValueError, OverflowError):
        raise BuiltinError(f"cannot take ceil of {show(x)}") from None


@builtin(category="math")
def bi_trunc(ctx, x):
    """Round toward zero to an integer."""
    _need_num(x, "trunc")
    try:
        return math.trunc(x)
    except (ValueError, OverflowError):
        raise BuiltinError(f"cannot truncate {show(x)}") from None


def _round_half_away(x: float) -> int:
    ax = abs(x)
    r = math.floor(ax)
    if ax - r >= 0.5:
        r += 1
    return -r if x < 0 else r


# Enough precision for any float rounded to at most 400 decimals either way
_ROUND_CONTEXT = Context(prec=1000)


@builtin(category="math")
def bi_round(ctx, x, digits=None):
    """Round to the nearest integer (halves away from zero), or to `digits` decimals."""
    _need_num(x, "round")
    if digits is not None:
        _need_int(digits, "round", "digit count")
    if type(x) is int:
        if digits is None or digits >= 0:
            return x
        if -digits > len(str(abs(x))):
            return 0
        unit = 10 ** -digits
        q, r = divmod(abs(x), unit)
        if 2 * r >= unit:
            q += 1
        return q * unit if x >= 0 else -q * unit
    if digits is None:
        if x != x or x in (math.inf, -math.inf):
            raise BuiltinError(f"cannot round {show(x)}")
        return _round_half_away(x)
    if x != x or x in (math.inf, -math.inf):
        return x
    # Past 400 digits either way, a float is either unchanged or rounds to 0
    digits = max(-400, min(digits, 400))
    q = Decimal(repr(x)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP, context=_ROUND_CONTEXT)
    return float(q)


@builtin(category="math")
def bi_min(ctx, *values):
    """Smallest of the arguments (or of a single list)."""
    items = _flatten_args(values, "min")
    try:
        return min(items)
    except TypeError:
        raise BuiltinError("min() needs values that can be compared") from None


@builtin(category="math")
def bi_max(ctx, *values):
    """Largest of the arguments (or of a single list)."""
    items = _flatten_args(values, "max")
    try:
        return max(items)
    except TypeError:
        raise BuiltinError("max() needs values that can be compared") from None


def _flatten_args(values: tuple, fname: str) -> list:
    if len(values) == 1 and type(values[0]) in (list, range, dict, str):
        items = list(_iterable(values[0], fname))
    else:
        items = list(values)
    if not items:
        raise BuiltinError(f"{fname}() of an empty sequence")
    return items


@builtin(category="math")
def bi_clamp(ctx, x, lo, hi):
    """Limit x to the range [lo, hi]."""
    for v in (x, lo, hi):
        _need_num(v, "clamp")
    return max(lo, min(hi, x))


@builtin(category="math")
def bi_gcd(ctx, a, b):
    """Greatest common divisor."""
    _need_int(a, "gcd")
    _need_int(b, "gcd")
    return math.gcd(a, b)


@builtin(category="math")
def bi_lcm(ctx, a, b):
    """Least common multiple."""
    _need_int(a, "lcm")
    _need_int(b, "lcm")
    return abs(a * b) // math.gcd(a, b) if a and b else 0


@builtin(category="math")
def bi_isqrt(ctx, n):
    """Integer square root, the largest k where k^2 <= n."""
    _need_int(n, "isqrt")
    if n < 0:
        raise BuiltinError(f"isqrt of a negative number ({n})")
    return math.isqrt(n)


@builtin(category="math")
def bi_factorial(ctx, n):
    """Factorial of n, also written n! (floats use the gamma function)."""
    return ops.factorial(n)


@builtin(category="math")
def bi_choose(ctx, n, k):
    """Binomial coefficient: ways to choose k items from n."""
    _need_int(n, "choose")
    _need_int(k, "choose")
    if n < 0 or k < 0:
        raise BuiltinError("choose() needs non-negative integers")
    return math.comb(n, k)


@builtin(category="math")
def bi_perm(ctx, n, k):
    """Number of ordered arrangements of k items from n."""
    _need_int(n, "perm")
    _need_int(k, "perm")
    if n < 0 or k < 0:
        raise BuiltinError("perm() needs non-negative integers")
    return math.perm(n, k)


@builtin(category="math")
def bi_pow(ctx, base, exponent):
    """base raised to exponent (same as base ^ exponent)."""
    return ops.power(base, exponent)


def _is_prime(n: int) -> bool:
    if n < 2:
        return False
    small = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)
    for p in small:
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for a in small:
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


@builtin(category="math")
def bi_is_prime(ctx, n):
    """True if n is a prime number."""
    _need_int(n, "is_prime")
    return _is_prime(n)


@builtin(category="math")
def bi_primes(ctx, limit):
    """All primes up to and including `limit` (sieve of Eratosthenes)."""
    _need_int(limit, "primes")
    if limit < 2:
        return []
    if limit > 50_000_000:
        raise BuiltinError("primes() limit is too large (max 50,000,000)")
    sieve = bytearray([1]) * (limit + 1)
    sieve[0] = sieve[1] = 0
    for i in range(2, math.isqrt(limit) + 1):
        if sieve[i]:
            sieve[i * i::i] = bytearray(len(range(i * i, limit + 1, i)))
    return [i for i, flag in enumerate(sieve) if flag]


def _pollard_rho(n: int) -> int:
    if n % 2 == 0:
        return 2
    rng = _random.Random(n)
    while True:
        x = rng.randrange(2, n)
        y, c, d = x, rng.randrange(1, n), 1
        while d == 1:
            x = (x * x + c) % n
            y = (y * y + c) % n
            y = (y * y + c) % n
            d = math.gcd(abs(x - y), n)
        if d != n:
            return d


@builtin(category="math")
def bi_factors(ctx, n):
    """Prime factorization, e.g. factors(360) == [2, 2, 2, 3, 3, 5]."""
    _need_int(n, "factors")
    if n < 1:
        raise BuiltinError("factors() needs a positive integer")
    out: list[int] = []
    for p in (2, 3, 5, 7, 11, 13):
        while n % p == 0:
            out.append(p)
            n //= p
    stack = [n] if n > 1 else []
    while stack:
        m = stack.pop()
        if _is_prime(m):
            out.append(m)
            continue
        d = _pollard_rho(m)
        stack.extend([d, m // d])
    return sorted(out)


@builtin(category="math")
def bi_divisors(ctx, n):
    """All positive divisors of n, in increasing order."""
    _need_int(n, "divisors")
    if n < 1:
        raise BuiltinError("divisors() needs a positive integer")
    small, large = [], []
    for i in range(1, math.isqrt(n) + 1):
        if n % i == 0:
            small.append(i)
            if i != n // i:
                large.append(n // i)
    return small + large[::-1]


@builtin(category="math")
def bi_linspace(ctx, start, stop, count):
    """`count` evenly spaced floats from start to stop (inclusive)."""
    _need_num(start, "linspace")
    _need_num(stop, "linspace")
    _need_int(count, "linspace", "count")
    if count < 2:
        return [float(start)] if count == 1 else []
    step = (stop - start) / (count - 1)
    return [start + i * step for i in range(count - 1)] + [float(stop)]


# Statistics

@builtin(category="stats")
def bi_sum(ctx, *values):
    """Sum of a list (or of the arguments)."""
    items = list(values[0]) if len(values) == 1 and type(values[0]) in (list, range) else list(values)
    total: Any = 0
    for x in items:
        if not is_number(x):
            raise BuiltinError(f"sum() expects numbers, found {type_name(x)}")
        total += x
    return total


@builtin(category="stats")
def bi_product(ctx, *values):
    """Product of a list (or of the arguments)."""
    items = list(values[0]) if len(values) == 1 and type(values[0]) in (list, range) else list(values)
    total: Any = 1
    for x in items:
        if not is_number(x):
            raise BuiltinError(f"product() expects numbers, found {type_name(x)}")
        total *= x
    return total


def _stat(fname: str, fn: Callable, doc: str, min_len: int = 1) -> None:
    def impl(ctx, xs):
        items = _numbers(_iterable(xs, fname), fname)
        if len(items) < min_len:
            raise BuiltinError(f"{fname}() needs at least {min_len} value{'s' if min_len > 1 else ''}")
        return fn(items)
    impl.__doc__ = doc
    builtin(fname, category="stats")(impl)


_stat("mean", statistics.fmean, "Arithmetic mean (average).")
_stat("median", statistics.median, "Middle value.")
_stat("mode", statistics.mode, "Most common value.")
_stat("stdev", statistics.stdev, "Sample standard deviation.", 2)
_stat("variance", statistics.variance, "Sample variance.", 2)


# Random numbers

@builtin(category="random")
def bi_random(ctx):
    """A random float in [0, 1)."""
    return ctx.random.random()


@builtin(category="random")
def bi_randint(ctx, lo, hi):
    """A random integer in [lo, hi] (inclusive)."""
    _need_int(lo, "randint")
    _need_int(hi, "randint")
    if lo > hi:
        raise BuiltinError("randint() needs lo <= hi")
    return ctx.random.randint(lo, hi)


@builtin(category="random")
def bi_choice(ctx, xs):
    """A random element of a list."""
    items = list(_iterable(xs, "choice"))
    if not items:
        raise BuiltinError("choice() from an empty list")
    return ctx.random.choice(items)


@builtin(category="random")
def bi_shuffle(ctx, xs):
    """A shuffled copy of a list."""
    items = list(_iterable(xs, "shuffle"))
    ctx.random.shuffle(items)
    return items


@builtin(category="random")
def bi_seed(ctx, n):
    """Seed the random number generator for reproducible results."""
    if type(n) not in (int, float, str):
        raise BuiltinError(f"seed() expects a number or string, found {type_name(n)}")
    ctx.random.seed(n)


# Lists

@builtin(category="list")
def bi_len(ctx, x):
    """Number of items in a list, string, dict or range."""
    if type(x) in (list, str, dict, range):
        return len(x)
    raise BuiltinError(f"len() of {type_name(x)} is undefined")


@builtin(category="list")
def bi_range(ctx, a, b=None, step=1):
    """range(stop), range(start, stop) or range(start, stop, step), not including stop."""
    if b is None:
        a, b = 0, a
    for v in (a, b, step):
        _need_int(v, "range")
    if step == 0:
        raise BuiltinError("range() step cannot be zero")
    return range(a, b, step)


@builtin(category="list")
def bi_push(ctx, xs, *values):
    """Append values to a list (in place) and return the list."""
    if type(xs) is not list:
        raise BuiltinError(f"push() needs a list, found {type_name(xs)}")
    xs.extend(values)
    return xs


@builtin(category="list")
def bi_pop(ctx, xs, index=-1):
    """Remove and return the last item (or the item at `index`)."""
    if type(xs) is not list:
        raise BuiltinError(f"pop() needs a list, found {type_name(xs)}")
    if not xs:
        raise BuiltinError("pop() from an empty list")
    _need_int(index, "pop", "index")
    try:
        return xs.pop(index)
    except IndexError:
        raise BuiltinError(f"pop index {index} out of range for list of length {len(xs)}") from None


@builtin(category="list")
def bi_insert(ctx, xs, index, value):
    """Insert a value before `index` (in place) and return the list."""
    if type(xs) is not list:
        raise BuiltinError(f"insert() needs a list, found {type_name(xs)}")
    _need_int(index, "insert", "index")
    xs.insert(index, value)
    return xs


@builtin(category="list")
def bi_remove(ctx, coll, item):
    """Remove the first occurrence of `item` from a list, or a key from a dict."""
    if type(coll) is list:
        for i, x in enumerate(coll):
            if ops.eq(x, item):
                del coll[i]
                return coll
        raise BuiltinError(f"remove(): {show(item, True)} is not in the list")
    if type(coll) is dict:
        try:
            coll.pop(item, None)
        except TypeError:  # an unhashable key can't be in the dict
            pass
        return coll
    raise BuiltinError(f"remove() needs a list or dict, found {type_name(coll)}")


@builtin(category="list")
def bi_contains(ctx, coll, item):
    """True if the collection contains the item (same as `item in coll`)."""
    return ops.contains(coll, item)


@builtin(category="list")
def bi_index_of(ctx, coll, item):
    """Position of the first occurrence of item, or -1."""
    if type(coll) is str:
        _need_str(item, "index_of")
        return coll.find(item)
    for i, x in enumerate(_iterable(coll, "index_of")):
        if ops.eq(x, item):
            return i
    return -1


@builtin(category="list")
def bi_count(ctx, coll, item):
    """How many items equal `item` (or satisfy it, if it is a function)."""
    if type(coll) is str and type(item) is str:
        return coll.count(item)
    items = _iterable(coll, "count")
    if is_callable(item):
        return sum(1 for x in items if ctx.call(item, [x]))
    return sum(1 for x in items if ops.eq(x, item))


@builtin(category="list")
def bi_sort(ctx, xs, key=None):
    """A sorted copy of a list, optionally ordered by `key(item)`."""
    items = list(_iterable(xs, "sort"))
    try:
        if key is None:
            return sorted(items)
        _need_fn(key, "sort")
        return sorted(items, key=lambda x: ctx.call(key, [x]))
    except TypeError:
        raise BuiltinError("sort() needs items that can be compared with each other") from None


@builtin(category="list")
def bi_reverse(ctx, xs):
    """A reversed copy of a list or string."""
    if type(xs) is str:
        return xs[::-1]
    return list(_iterable(xs, "reverse"))[::-1]


@builtin(category="functional")
def bi_map(ctx, xs, f):
    """Apply `f` to every item: map([1, 2, 3], fn(x) => x * 2) == [2, 4, 6]."""
    items, f = _seq_and_fn(ctx, xs, f, "map")
    call = ctx.call
    return [call(f, [x]) for x in items]


@builtin(category="functional")
def bi_filter(ctx, xs, f):
    """Keep the items for which `f(item)` is truthy."""
    items, f = _seq_and_fn(ctx, xs, f, "filter")
    call = ctx.call
    return [x for x in items if call(f, [x])]


@builtin(category="functional")
def bi_reduce(ctx, xs, f, initial=None):
    """Combine items left to right: reduce([1, 2, 3], fn(a, b) => a + b) == 6."""
    items, f = _seq_and_fn(ctx, xs, f, "reduce")
    it = iter(items)
    if initial is None:
        try:
            acc = next(it)
        except StopIteration:
            raise BuiltinError("reduce() of an empty sequence with no initial value") from None
    else:
        acc = initial
    for x in it:
        acc = ctx.call(f, [acc, x])
    return acc


@builtin(category="functional")
def bi_each(ctx, xs, f):
    """Call `f` on every item (for side effects)."""
    items, f = _seq_and_fn(ctx, xs, f, "each")
    for x in items:
        ctx.call(f, [x])


@builtin(category="functional")
def bi_find(ctx, xs, f):
    """The first item for which `f(item)` is truthy, or nil."""
    items, f = _seq_and_fn(ctx, xs, f, "find")
    for x in items:
        if ctx.call(f, [x]):
            return x
    return None


@builtin(category="functional")
def bi_any(ctx, xs, f=None):
    """True if any item is truthy (or satisfies `f`)."""
    items = _iterable(xs, "any")
    if f is None:
        return any(items)
    return any(ctx.call(f, [x]) for x in items)


@builtin(category="functional")
def bi_all(ctx, xs, f=None):
    """True if every item is truthy (or satisfies `f`)."""
    items = _iterable(xs, "all")
    if f is None:
        return all(items)
    return all(ctx.call(f, [x]) for x in items)


@builtin(category="functional")
def bi_memoize(ctx, f):
    """A version of `f` that remembers its results, like `fib = memoize(fib)`."""
    _need_fn(f, "memoize")
    cache: dict = {}

    def memo(inner_ctx, *args):
        try:
            key = tuple(args)
            hash(key)
        except TypeError:
            return inner_ctx.call(f, list(args))
        if key not in cache:
            cache[key] = inner_ctx.call(f, list(args))
        return cache[key]

    name = getattr(f, "name", "fn")
    return Builtin(f"memoized {name}", memo, 0, None, f"memoized version of {name}")


@builtin(category="functional")
def bi_compose(ctx, f, g):
    """compose(f, g)(x) == f(g(x))."""
    _need_fn(f, "compose")
    _need_fn(g, "compose")
    return Builtin("composed", lambda c, *args: c.call(f, [c.call(g, list(args))]), 0, None,
                   "composition of two functions")


@builtin(category="list")
def bi_zip(ctx, *seqs):
    """Pair up items: zip([1, 2], ["a", "b"]) == [[1, "a"], [2, "b"]]."""
    return [list(t) for t in zip(*(_iterable(s, "zip") for s in seqs))]


@builtin(category="list")
def bi_enumerate(ctx, xs, start=0):
    """List of [index, item] pairs, for use with `for i, x in enumerate(xs)`."""
    _need_int(start, "enumerate", "start")
    return [[i, x] for i, x in enumerate(_iterable(xs, "enumerate"), start)]


@builtin(category="list")
def bi_take(ctx, xs, n):
    """The first n items."""
    _need_int(n, "take")
    return list(_iterable(xs, "take"))[:max(n, 0)] if type(xs) is not str else xs[:max(n, 0)]


@builtin(category="list")
def bi_drop(ctx, xs, n):
    """Everything except the first n items."""
    _need_int(n, "drop")
    return list(_iterable(xs, "drop"))[max(n, 0):] if type(xs) is not str else xs[max(n, 0):]


@builtin(category="list")
def bi_first(ctx, xs):
    """The first item (nil if empty)."""
    items = _iterable(xs, "first")
    return items[0] if len(items) else None


@builtin(category="list")
def bi_last(ctx, xs):
    """The last item (nil if empty)."""
    items = _iterable(xs, "last")
    return items[-1] if len(items) else None


@builtin(category="list")
def bi_unique(ctx, xs):
    """Items with duplicates removed (first occurrence wins)."""
    out, seen = [], set()
    for x in _iterable(xs, "unique"):
        # Tag booleans, since the set would treat true and 1 as the same item
        key = (x.__class__ is bool, x)
        try:
            if key in seen:
                continue
            seen.add(key)
        except TypeError:
            if any(ops.eq(x, y) for y in out):
                continue
        out.append(x)
    return out


@builtin(category="list")
def bi_flatten(ctx, xs):
    """Flatten one level of nesting, e.g. flatten([[1, 2], [3]]) == [1, 2, 3]."""
    out = []
    for x in _iterable(xs, "flatten"):
        if type(x) is list:
            out.extend(x)
        else:
            out.append(x)
    return out


@builtin(category="list")
def bi_chunk(ctx, xs, size):
    """Split into lists of `size` items."""
    _need_int(size, "chunk", "size")
    if size < 1:
        raise BuiltinError("chunk() size must be positive")
    items = list(_iterable(xs, "chunk"))
    return [items[i:i + size] for i in range(0, len(items), size)]


@builtin(category="list")
def bi_copy(ctx, x):
    """A shallow copy of a list or dict."""
    if type(x) is list:
        return list(x)
    if type(x) is dict:
        return dict(x)
    return x


@builtin(category="list")
def bi_join(ctx, xs, sep=""):
    """Join items into a string: join([1, 2, 3], ", ") == "1, 2, 3"."""
    if type(xs) is str and type(sep) in (list, range):
        xs, sep = sep, xs  # also accept the Python order: join(", ", xs)
    _need_str(sep, "join")
    return sep.join(show(x) for x in _iterable(xs, "join"))


# Strings

def _str1(fname: str, fn: Callable[[str], Any], doc: str) -> None:
    def impl(ctx, s):
        _need_str(s, fname)
        return fn(s)
    impl.__doc__ = doc
    builtin(fname, category="string")(impl)


_str1("upper", str.upper, "Uppercase copy of a string.")
_str1("lower", str.lower, "Lowercase copy of a string.")
_str1("trim", str.strip, "Copy without leading/trailing whitespace.")
_str1("chars", list, "List of the characters in a string.")
_str1("lines", str.splitlines, "List of the lines in a string.")
_str1("is_digit", str.isdigit, "True if the string is non-empty and all digits.")
_str1("is_alpha", str.isalpha, "True if the string is non-empty and all letters.")


@builtin(category="string")
def bi_split(ctx, s, sep=None):
    """Split a string on `sep` (default: any whitespace)."""
    _need_str(s, "split")
    if sep is not None:
        _need_str(sep, "split")
        if sep == "":
            return list(s)
    return s.split(sep)


@builtin(category="string")
def bi_replace(ctx, s, old, new):
    """Replace every occurrence of `old` with `new`."""
    for v in (s, old, new):
        _need_str(v, "replace")
    return s.replace(old, new)


@builtin(category="string")
def bi_starts_with(ctx, s, prefix):
    """True if s starts with prefix."""
    _need_str(s, "starts_with")
    _need_str(prefix, "starts_with")
    return s.startswith(prefix)


@builtin(category="string")
def bi_ends_with(ctx, s, suffix):
    """True if s ends with suffix."""
    _need_str(s, "ends_with")
    _need_str(suffix, "ends_with")
    return s.endswith(suffix)


@builtin(category="string")
def bi_ord(ctx, ch):
    """Unicode code point of a one-character string."""
    _need_str(ch, "ord")
    if len(ch) != 1:
        raise BuiltinError(f"ord() expects a single character, found a string of length {len(ch)}")
    return ord(ch)


@builtin(category="string")
def bi_chr(ctx, code):
    """One-character string for a Unicode code point."""
    _need_int(code, "chr")
    try:
        return chr(code)
    except (ValueError, OverflowError):
        raise BuiltinError(f"{code} is not a valid code point") from None


@builtin(category="string")
def bi_pad(ctx, s, width, fill=" "):
    """Pad on the left to `width` (negative width pads on the right)."""
    _need_int(width, "pad", "width")
    _need_str(fill, "pad")
    text = show(s)
    return text.rjust(width, fill[:1] or " ") if width >= 0 else text.ljust(-width, fill[:1] or " ")


# Dicts

def _need_dict(d: Any, fname: str) -> None:
    if type(d) is not dict:
        raise BuiltinError(f"{fname}() needs a dict, found {type_name(d)}")


@builtin(category="dict")
def bi_keys(ctx, d):
    """List of a dict's keys."""
    _need_dict(d, "keys")
    return list(d.keys())


@builtin(category="dict")
def bi_values(ctx, d):
    """List of a dict's values."""
    _need_dict(d, "values")
    return list(d.values())


@builtin(category="dict")
def bi_items(ctx, d):
    """List of [key, value] pairs, for use with `for k, v in items(d)`."""
    _need_dict(d, "items")
    return [[k, v] for k, v in d.items()]


@builtin(category="dict")
def bi_get(ctx, d, key, default=None):
    """d[key] if present, otherwise `default` (nil). Also works with a list and an index."""
    if type(d) is list:
        _need_int(key, "get", "index")
        return d[key] if -len(d) <= key < len(d) else default
    _need_dict(d, "get")
    try:
        return d.get(key, default)
    except TypeError:
        return default


@builtin(category="dict")
def bi_has(ctx, d, key):
    """True if the dict has the key."""
    _need_dict(d, "has")
    try:
        return key in d
    except TypeError:
        return False


# Misc

@builtin(category="misc")
def bi_assert(ctx, cond, message="assertion failed"):
    """Stop with an error if `cond` is false."""
    if not cond:
        raise BuiltinError(show(message))


@builtin(category="misc")
def bi_error(ctx, message):
    """Raise an error (catchable with try/catch)."""
    raise ThrownValue(message)


class ThrownValue(Exception):
    def __init__(self, value: Any) -> None:
        super().__init__(show(value))
        self.value = value


@builtin(category="misc")
def bi_clock(ctx):
    """Seconds elapsed on a high-resolution timer (for measuring code)."""
    return time.perf_counter()


@builtin(category="misc")
def bi_sleep(ctx, seconds):
    """Pause for a number of seconds."""
    _need_num(seconds, "sleep")
    time.sleep(max(0, seconds))


# Plotting in the terminal (using braille characters)

_BRAILLE_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))


def render_plot(ys_by_x: list[tuple[float, float]], width: int, height: int,
                xlo: float, xhi: float) -> str:
    finite = [y for _, y in ys_by_x if y == y and abs(y) != math.inf]
    if not finite:
        return "(nothing to plot)"
    ylo, yhi = min(finite), max(finite)
    if ylo == yhi:
        ylo, yhi = ylo - 1, yhi + 1
    pw, ph = width * 2, height * 4
    grid = [[0] * width for _ in range(height)]

    def dot(px: int, py: int) -> None:
        if 0 <= px < pw and 0 <= py < ph:
            grid[py // 4][px // 2] |= _BRAILLE_BITS[py % 4][px % 2]

    # Draw the axes
    if ylo <= 0 <= yhi:
        py0 = round((yhi - 0) / (yhi - ylo) * (ph - 1))
        for px in range(0, pw, 2):
            dot(px, py0)
    if xlo <= 0 <= xhi:
        px0 = round((0 - xlo) / (xhi - xlo) * (pw - 1))
        for py in range(0, ph, 2):
            dot(px0, py)
    prev = None
    for x, y in ys_by_x:
        if not (y == y and abs(y) != math.inf):
            prev = None
            continue
        px = round((x - xlo) / (xhi - xlo) * (pw - 1)) if xhi != xlo else 0
        py = round((yhi - y) / (yhi - ylo) * (ph - 1))
        if prev is not None:
            # Connect to the previous point so the curve is continuous
            (qx, qy) = prev
            steps = max(abs(px - qx), abs(py - qy), 1)
            if steps < ph:
                for i in range(1, steps):
                    dot(qx + (px - qx) * i // steps, qy + (py - qy) * i // steps)
        dot(px, py)
        prev = (px, py)

    top, bottom = f"{yhi:.4g}", f"{ylo:.4g}"
    label_w = max(len(top), len(bottom))
    lines = []
    for r, row in enumerate(grid):
        label = top if r == 0 else bottom if r == height - 1 else ""
        lines.append(f"{label.rjust(label_w)} ┤" + "".join(chr(0x2800 + cell) for cell in row))
    left, right = f"{xlo:.4g}", f"{xhi:.4g}"
    lines.append(" " * label_w + " └" + "─" * width)
    lines.append(" " * (label_w + 2) + left + right.rjust(width - len(left)))
    return "\n".join(lines)


@builtin(category="io")
def bi_plot(ctx, f, a=None, b=None, width=60, height=15):
    """Plot a function over [a, b] (default [-10, 10]) or a list of numbers, in the terminal."""
    _need_int(width, "plot", "width")
    _need_int(height, "plot", "height")
    width, height = max(10, min(width, 200)), max(3, min(height, 80))
    if type(f) in (list, range):
        ys = _numbers(f, "plot")
        if not ys:
            raise BuiltinError("plot() of an empty list")
        points = [(float(i), float(y)) for i, y in enumerate(ys)]
        text = render_plot(points, width, height, 0.0, float(max(len(ys) - 1, 1)))
    else:
        _need_fn(f, "plot")
        lo = -10.0 if a is None else a
        hi = 10.0 if b is None else b
        _need_num(lo, "plot", "start")
        _need_num(hi, "plot", "end")
        if hi <= lo:
            raise BuiltinError("plot() needs start < end")
        samples = width * 2 * 2
        points = []
        for i in range(samples + 1):
            x = lo + (hi - lo) * i / samples
            try:
                y = ctx.call(f, [x])
            except (JarLangError, BuiltinError, ArithmeticError, ValueError):
                y = math.nan
            points.append((x, float(y) if is_number(y) else math.nan))
        text = render_plot(points, width, height, float(lo), float(hi))
    ctx.write(text + "\n")
