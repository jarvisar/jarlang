# JarLang Language Guide
This page describes the JarLang language. A list of all built-in functions can be found in [builtins.md](builtins.md).

### Basics
A program is a list of statements, one per line. A semicolon can also be used to separate statements, and comments start with the hash symbol.

```
print("Hello, world!")      # print a line
x = 2; y = 3                # two statements on one line
```

A statement can continue onto the next line if the line ends with an operator or comma, if a bracket is still open, or if the next line starts with `|>` or `.name`:

```
total = price +
  tax
result = numbers
  |> filter(is_prime)
  |> sum
```

The types of values are int, float, bool (`true` or `false`), str, nil, list, dict, range, and functions. `type(x)` returns the type of a value.

### Numbers
Numbers can be written as `42`, `1_000_000`, `3.14`, `6.02e23`, `0xFF`, `0b1010`, or `0o17`. Integers have no size limit in the interpreter, e.g. `100!` is exact.

* `+`, `-`, `*`: addition, subtraction, and multiplication
* `/`: division, which always returns a decimal, e.g. `7 / 2` is `3.5` and `4 / 2` is `2.0`
* `//`: floor division, e.g. `7 // 2` is `3` and `-7 // 2` is `-4`
* `%`: modulus, e.g. `-7 % 3` is `2`
* `^` or `**`: exponents, e.g. `2^10` is `1024` and `2^-1` is `0.5`
* `-x`: negation. Exponents are done first, so `-2^2` is `-4`
* `√x`: square root, e.g. `√16` is `4.0`
* `n!`: factorial, e.g. `5!` is `120`. Decimals use the gamma function, e.g. `0.5!` is `0.886...`

A number followed directly by a variable, `(` or `√` is multiplied by it, e.g. `2x`, `3(x + 1)`, and `2√x`. This is done before other operators, so `2x^2` is `2 * x^2` and `1/2x` is `1 / (2x)`.

The symbols `×`, `÷`, `≤`, `≥`, `≠`, `√`, and `∞` can also be used, as well as the constants `π`, `τ`, and `φ`. The constants pi, e, tau, phi, inf, and nan can't be changed, but `let` can be used to create a variable with the same name.

### Strings
Strings in double quotes can include values using curly brackets. Strings in single quotes are left as they are.

```
name = "Ada"
print("Hello, {name}! 2 + 2 = {2 + 2}")
print('no {values} here')
print("literal brackets: \{ \}")
```

A format can be added after a colon inside the brackets, using the same format as Python:

```
print("{pi:.3f}")           # 3.142
print("{42:>6}|{42:<6}|")   #     42|42    |
print("{1234567:,}")        # 1,234,567
print("{255:x} {5:08b}")    # ff 00000101
print("{0.25:.1%}")         # 25.0%
```

Strings can be added together with `+`, repeated with `*`, indexed with `s[i]`, and sliced with `s[a:b]`. `x in s` checks if a string contains another string, and `len(s)` returns the number of characters.

### Comparisons and Logic
The comparison operators are `==`, `!=`, `<`, `<=`, `>`, and `>=`. Comparisons can be chained, e.g. `0 < x <= 10` is the same as `0 < x and x <= 10`.

The logical operators are `and` (or `&&`), `or` (or `||`), and `not` (or `!`). `and` and `or` return one of their values, so `name or "anonymous"` can be used to give a default value. The values `false`, `nil`, `0`, `0.0`, `""`, `[]`, and `{}` count as false.

`x in list` checks if a list, string, dict, or range contains a value. Booleans are not numbers, so `true == 1` is `false`.

### Variables
Variables are created by assigning them a value. `let` creates a new variable and `const` creates one that can't be changed.

```
count = 0
count += 1                  # also -=, *=, /=, //=, %=, and ^=
let label: str = "total"    # type annotations are optional
const G = 9.81
a, b = b, a + b             # assign several variables at once
x, y = point                # unpack a list
```

Functions, loops, and comprehensions can use variables from outside them. Assigning a variable inside a function changes the outside variable if it exists, otherwise it creates a new variable inside the function. `let` always creates a new variable. If blocks and loops don't create new variables.

```
total = 0
fn add(n) { total += n }    # changes the outside variable
fn test() { let total = 1 } # creates a new variable
```

`let x = x + 1` reads the outside `x` on the right side, then creates the new variable.

Functions keep using a variable, not a copy of its value. Since loops don't create new variables, functions made in a loop all see the last value, e.g. `[fn() => i for i in 1..3][0]()` returns `3`. To keep each value, pass it to a function, e.g. `fn always(v) => fn() => v` and `[always(i) for i in 1..3]`.

Undefined variables are reported before the program runs.

### Control Flow

```
if n < 0 {
  print("negative")
} elif n == 0 {
  print("zero")
} else {
  print("positive")
}

while n > 1 { n //= 2 }

for i in 1..10 { }              # 1 to 10
for i in 0..<len(xs) { }        # 0 to len(xs) - 1
for i in range(10, 0, -2) { }   # 10, 8, 6, 4, 2
for x in [1, 2, 3] { }
for ch in "text" { }
for i, x in enumerate(xs) { }
for k, v in items(d) { }
```

`break` and `continue` work in both loops. If statements can also return a value, which is the last value in the block:

```
kind = if n % 2 == 0 { "even" } else { "odd" }
```

### Functions
Functions can be written in three ways:

```
f(x) = x^2 + 1                  # like math
fn area(w, h) => w * h          # one expression
fn greet(name, greeting = "Hello") {
  "{greeting}, {name}!"         # the last value is returned
}
```

`return` can be used to return early. Parameters can have default values and type annotations. Functions can be called before the line that defines them.

Functions are values, so they can be passed to other functions and stored in variables. Anonymous functions are written as `fn(x) => x * 2` or `fn(x) { ... }`, and can use variables from the function that created them:

```
fn make_counter() {
  count = 0
  fn() { count += 1; count }
}
next = make_counter()
next()
print(next())                   # 2

fib = memoize(fib)              # save the results of a recursive function
```

### Lists, Ranges, and Dicts
Lists hold values in order and can be changed:

```
xs = [3, 1, 4]
xs[0] = 9
push(xs, 5)
print(xs[-1], xs[1:3], len(xs), 3 in xs, sort(xs), sum(xs))
print([1, 2] + [3], [0] * 5)
```

List comprehensions create a list from a loop, e.g. `[n^2 for n in 1..10 if n % 2 == 0]`.

Ranges are written as `a..b` (including `b`) or `a..<b` (not including `b`). `range(a, b, step)` works the same as in Python, and `list(r)` turns a range into a list.

Dicts store values by key. Keys that are names can be written without quotes, and `d.key` is the same as `d["key"]`. Dicts can also hold functions:

```
p = {name: "Ada", born: 1815, "full name": "Augusta Ada King"}
print(p.born, p["full name"], keys(p), has(p, "name"), get(p, "age", 0))

account = {balance: 0}
account.deposit = fn(amount) { account.balance += amount }
account.deposit(10)
```

As in Python, `1` and `1.0` are the same key, and so are `true` and `1`.

### Pipelines and Methods
`x |> f` is the same as `f(x)`, and `x |> f(a)` is the same as `f(x, a)`. `x.f(a)` is also the same as `f(x, a)`, unless `x` is a dict with a function called `f`.

```
words = split("the quick brown fox")
print(words.map(len).sum())
print(words |> filter(fn(w) => len(w) > 3) |> map(upper) |> join(" "))
```

### Errors
Errors stop the program and show where they happened. `throw` creates an error with any value, and `try` and `catch` handle errors:

```
try {
  withdraw(50, 80)
} catch err {
  print("could not withdraw:", err)
}
throw {code: 404, reason: "not found"}
error("something went wrong")
assert(total >= 0, "total went negative")
```

### Imports
`import "file"` runs another `.jlang` file and adds its variables and functions to the program (names that start with `_` are not added). `import "file" as name` stores them in a dict instead. Paths are relative to the current file.

```
import "lib/geometry"
import "lib/geometry" as geo
print(circle_area(2), geo.hypotenuse(3, 4))
```

### Type Annotations
Type annotations are optional. The interpreter checks them when a function is called or returns, and the native compiler uses them as the types of variables. The types are int, float, num (int or float), bool, str, list, dict, range, fn, nil, and any. An int passed as a float is converted to a float.

```
fn mean(xs: list, weight: float = 1.0) -> float {
  sum(xs) * weight / len(xs)
}
```

### Operator Precedence
From lowest to highest:

1. `|>`
2. `or`, `||`
3. `and`, `&&`
4. `not`
5. `==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not in`
6. `..`, `..<`
7. `+`, `-`
8. `*`, `/`, `//`, `%`, and implicit multiplication
9. `-`, `+`, `√`, and `!` before a value
10. `^`, `**`
11. function calls, indexes, fields, methods, and `!` after a value
