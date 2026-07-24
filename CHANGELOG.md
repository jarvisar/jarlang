# Changelog

### 2.0.0
JarLang 2 is a rewrite of the project that turns the calculator into a full programming language, and adds a native compiler that outputs the same result as the interpreter.

Language:
* If, while, and for loops, break, continue, return, let, const, and assigning several variables at once
* Functions with `fn`, `=>`, or `f(x) = ...`, default values, type annotations, and closures
* Strings with values inside them (`"x = {x}"`), lists, dicts, ranges, and list comprehensions
* Implicit multiplication (`2x`), square roots (`√x`), chained comparisons, and Unicode symbols such as `π`
* Pipelines (`x |> f`), method calls (`x.f()`), try, catch, throw, and imports
* Over 100 built-in functions

Implementation:
* The code is now a Python package with a `jarlang` command
* New lexer, Pratt parser, and semantic analyzer with better error messages
* Faster interpreter that converts the tree of nodes into Python functions
* Native compiler for x86-64 on Linux, macOS, and Windows with type inference, overflow checks, and optimizations

Tools:
* Interactive interpreter with highlighting and autocomplete
* Commands to check, format, and benchmark code, and to print the tokens, tree, and assembly code
* Language server, VS Code extension, and a playground that runs in the browser
* Tests that compare the interpreter and native compiler

Changes from 1.x:
* Use `_` instead of commas in numbers, e.g. `9_000`
* A semicolon at the end of a line no longer runs the assembly code. Use `jarlang run --native` instead
* `python main.py` has been replaced by the `jarlang` command

### 1.x
* Calculator with variables, math functions, and comparison and logical operators
* Interpreter and x86-64 code generator for integer expressions
