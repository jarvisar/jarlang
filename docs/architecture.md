# How It Works
JarLang runs code in several steps. The source code is converted into tokens by the lexer, the tokens are parsed into a tree of nodes, and the tree is checked by the semantic analyzer. The tree is then either run by the interpreter or compiled into x86-64 assembly code by the native compiler.

### Files
* source.py: stores the source code and positions (spans) used in error messages
* tokens.py, lexer.py: the lexer, which converts source code into tokens
* ast.py: the types of nodes in the tree
* parser.py: the parser, which converts tokens into a tree of nodes
* resolver.py: the semantic analyzer, which checks the tree and works out where each variable is stored
* interpreter.py: the interpreter
* values.py, ops.py: values and operators used by the interpreter
* builtins.py: the built-in functions
* errors.py: error messages
* native/: the native compiler
* cli.py, repl.py: the `jarlang` command and the interactive interpreter
* formatter.py, lsp.py, analysis.py: the formatter and language server
* web.py: functions used by the playground
* serve.py: builds the playground and serves it for `jarlang playground`

### Lexer
The lexer reads the source code one character at a time and creates a list of tokens. Each token stores its position in the source code so that errors can point to the right place.

A new line ends a statement, unless the line ends with an operator or comma, a bracket is still open, or the next line starts with `|>` or `.name`. Each token also stores whether there was a space before it, which is how `2x` is read as `2 * x` while `2 x` is an error. Strings with values inside curly brackets are split into parts, and the code inside the brackets is parsed separately.

### Parser
The parser is a Pratt parser, which gives each operator a precedence (binding power) and uses it to decide how to group operators. For example, `*` has a higher precedence than `+`, so `2 + 3 * 4` is parsed as `2 + (3 * 4)`. `^` groups from the right, so `2^3^2` is `2^(3^2)`.

Chained comparisons such as `0 < x <= 10` are stored in a single Compare node. A line such as `f(x) = x^2` is turned into a function definition.

If the parser finds an error, it skips to the next statement and keeps going, so several errors can be reported at once. Errors at the end of the input are marked as incomplete, which the interactive interpreter uses to ask for another line.

### Semantic Analyzer
The semantic analyzer goes through the tree before the program runs. For each function, it first finds every variable that is created in it, then works out which scope each variable belongs to and gives it a slot number. This lets functions change global variables that are created later in the file.

It also reports errors such as undefined variables (with suggestions), wrong numbers of arguments, changing constants, and `break` or `return` in the wrong place. Unused variables and unreachable code are reported as warnings.

### Interpreter
The interpreter converts each node into a small Python function once, then runs those functions. For example, a Binary node for `a + b` becomes:

```python
def add(fr):
    a, b = left(fr), right(fr)
    if a.__class__ is int and b.__class__ is int:
        return a + b
    ...
```

This avoids checking the type of each node every time it runs. Common cases, such as adding two integers or calling a function, have faster versions.

Each function call creates a frame, which is a Python list holding the function's variables at the slots chosen by the semantic analyzer. The first slot points to the frame of the function it was created in, which is how functions use variables from outside them. Global variables are stored in a dict.

`return`, `break`, and `continue` are passed back as values instead of exceptions, which keeps function calls fast. Errors store the line where they happened and the list of function calls that led to them.

### Error Messages
Errors show the file, line, and column, the line of code, and a marker under the part that caused the error, along with a hint when there is one:

```
error: `area` takes 2 arguments but 1 was given
 --> shapes.jlang:4:7
  |
4 | print(area(3))
  |       ^^^^^^^
  |
  = help: signature: area(w, h)
```

### Tests
* test_lexer.py, test_parser.py: the lexer and parser
* test_interpreter.py: the output of many small programs
* test_diagnostics.py: error messages and where they point
* test_examples.py: compares the output of each example with the files in tests/golden
* test_native.py: compiles programs to native code and checks that they output the same result as the interpreter
* test_cli.py, test_repl.py, test_formatter.py, test_lsp.py, test_web.py, test_serve.py: the other tools
