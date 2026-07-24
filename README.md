# Interpreter & Custom Programming Language
This project is a custom-built programming language called JarLang, developed to gain more experience in software design and development. It features a lexer, parser, semantic analyzer, interpreter, and a native compiler that generates x86-64 assembly code. Users can write and execute code using the included interpreter or native compiler, both of which output the same result.

Visit the [GitHub Pages site](https://ajarvis.co/jarlang/) to try JarLang in the browser.

<br>
<p align="center">
  <img width="200" src="assets/jarlang.png"/>
</p>

### Lexer
The input code is first converted into a token stream by the lexer, which identifies the different parts of the code (such as numbers, strings, operators, keywords, and variables). Each new line ends a statement, unless the line ends with an operator or is inside parentheses.

### Parser
The token stream is then parsed by the parser, which creates a tree of nodes that represents the program. The tree is made up of several types of nodes, such as:

* Literal nodes: represent individual numbers, booleans, and nil
* Name nodes: represent variables
* Binary nodes: represent binary operations between two sub-expressions
* Unary nodes: represent unary operations on a single sub-expression, such as negation or factorials
* Call nodes: represent function calls such as sine, cosine, and tangent
* Statement nodes: represent statements such as if, while, for, assignments, and function definitions

Each Binary node has a left child node and a right child node, which can be any of the node types themselves (such as a number or another Binary node), depending on the complexity of the expression. The parser uses operator precedence to decide how to group operators, so `2 + 3 * 4` is parsed as `2 + (3 * 4)`.

Example abstract syntax tree (AST) for `4 + 2 * 10 + 3 * (5 + 1)`:

<p align="center">
  <img src="https://keleshev.com/abstract-syntax-tree-an-example-in-c/ast.svg"/>
</p>

The tree for any code can be printed with `jarlang ast`.

### Semantic Analyzer
Before the program runs, the semantic analyzer (`resolver.py`) goes through the tree and works out which scope each variable belongs to. It also checks for errors such as undefined variables, calling a function with the wrong number of arguments, and using `break` outside of a loop, so they are reported before any code runs.

### Interpreter
Once the parser has constructed the tree of nodes, the Interpreter class is used to run the program. Instead of visiting every node each time it runs, the interpreter first converts each node into a small Python function, then runs those functions. This avoids checking the type of each node over and over and makes the interpreter much faster.

For example, a Binary node for `a + b` becomes a function that runs the functions for `a` and `b` and adds the results. If it encounters a Call node, it calls the function with the given arguments. Return, break, and continue are passed back as values instead of exceptions, which keeps function calls fast.

### Native Compiler

The `jarlang/native` folder contains the native compiler, which generates x86-64 assembly code from the AST and links it into an executable. Since JarLang doesn't require types, the compiler first works out the type of every variable and function (int, float, bool, str, or list). Each function is compiled once for every combination of argument types it is called with, e.g. `square(3)` and `square(1.5)` create two versions of `square`.

* typecheck.py: works out the types of all variables, functions, and expressions
* codegen.py: generates the assembly code for each node. Integers, strings, and lists are stored in the %rax register and decimals in the %xmm0 register
* peephole.py: removes unnecessary instructions from the generated code
* runtime.c: a small C library used for printing, strings, lists, and math functions
* toolchain.py: finds a C compiler (gcc, clang, or zig) to assemble and link the code into an executable

The generated assembly code includes comments showing which line of code each part comes from:

```asm
# fn fib(n: int) -> int
jlf_fib__i:
    pushq %rbp
    movq %rsp, %rbp
    subq $16, %rsp
    incq jl_depth(%rip)
    cmpq $2500, jl_depth(%rip)
    jg .Lpanic6
    movq 16(%rbp), %rax
    movq %rax, -8(%rbp)
    # 2: if n < 2 { return n }
    cmpq $2, %rax
    jge .Lelse4
    movq -8(%rbp), %rax
    jmp .Lret2
```

Integer overflow, division by zero, list indexes, and recursion depth are checked, so the native code stops with the same error message as the interpreter instead of returning a wrong result. The compiler supports both the Linux/macOS and Windows calling conventions.

### Features
Currently, it supports arithmetic binary operations such as addition (+), subtraction (-), multiplication (*), division (/), exponents (^ or **), floor division (//), and modulus (%). Unary operations such as negation (-), square roots (√), and factorials (!) are also supported.

Logical and comparison operators are also supported, such as and (and, &&), or (or, ||), not (not, !), less than or equal to (<=), greater than or equal to (>=), less than (<), greater than (>), equivalent (==), and not equal (!=). Comparisons can also be chained, e.g. `0 < x <= 10`.

It also supports over 100 built-in functions, such as sin, cos, tan, sqrt, log, exp, gcd, is_prime, mean, sort, and plot.

Works with integers of any size, decimals, negative numbers, strings, lists, and dicts. Also supports parentheses, e.g. `(3 + 4) / 2` outputs `3.5` and `3 + 4 / 2` outputs `5.0`.

Users can also define custom variables by assigning them values, e.g. entering `x = 45` and `y = 3 ** 3` and running `x + y` will output `72`. The symbol table defines several math constants as variables such as pi, Euler's number (e), tau, and infinity (inf) by default.

Other features include:

* Functions: `f(x) = x^2 + 1` or `fn f(x) { ... }`, including recursion and closures
* Implicit multiplication: `2x`, `3(x + 1)`
* If, while, and for loops: `for i in 1..10 { ... }`
* Strings with variables inside them: `"x = {x}"` or `"{pi:.2f}"`
* Lists and dicts: `[1, 2, 3]`, `{name: "Ada"}`
* Pipelines: `1..10 |> filter(is_prime) |> sum`
* Error handling with try, catch, and throw
* Importing other files with `import "file"`

```
f(x) = x^2 - 2x + 1
print(f(3), 2x^2, √16, 5!)

fn fib(n) {
  if n < 2 { return n }
  fib(n - 1) + fib(n - 2)
}
print([fib(n) for n in 0..10])
```

More details about the language can be found in the [docs](docs) folder, and example programs can be found in the [examples](examples) folder.

## How to Use

### Requirements

* [Python 3.10+](https://www.python.org/)
* A C compiler to use the native compiler: gcc, clang, or [zig](https://pypi.org/project/ziglang/) (`pip install ziglang`). On Windows, [Windows Subsystem for Linux](https://learn.microsoft.com/en-us/windows/wsl/install) also works

### Running the CLI

To use the interpreter, follow these steps:

1. Clone the repository to your local machine using the following command:

	`git clone https://github.com/jarvisar/jarlang.git`

2. Change directory into the root folder of the cloned repository:

	`cd jarlang`

3. Run the setup script, which creates a virtual environment in `.venv` and installs JarLang:

	`scripts\setup.cmd` on Windows, or `bash scripts/setup.sh` on macOS and Linux

4. Start the interpreter:

	`scripts\jarlang.cmd` on Windows, or `bash scripts/jarlang.sh` on macOS and Linux

5. After the interpreter is running, enter code to evaluate:

	`jl> (2 + 3) * 4`

    `20`


    `jl> cos(0)`

    `1.0`

6. To compile and execute code as native assembly code, enter `:native` followed by the code, e.g. `:native (2 + 3) * 4`.
7. To print the generated assembly code without executing it, enter `:asm` followed by the code.
8. Enter `:help` to see all commands, and input `exit` to exit the program.

The setup script only needs to be run once. To install JarLang without it, run `pip install -e .` and then use the `jarlang` command.

### Running from a File

The interpreter can also run code from a file. To do so, create a file with a `.jlang` extension and enter code in the file. For example, after creating a file called `example.jlang` with the following contents:

	# Define variables here
	x = 8
	y = 9_000 # Can also use underscores in numbers

	# Add comments using hash symbol

	z = x + y

	print(sqrt(x) + z)

Execute the code in the file with the following command:

`jarlang example.jlang` (or `scripts\jarlang.cmd example.jlang` if JarLang was installed with the setup script)

To compile the file to assembly code and run it, use `jarlang run --native example.jlang`. To create an executable, use `jarlang build example.jlang`.

### Scripts

The `scripts` folder contains scripts for common tasks. Each one has a `.cmd` version for Windows (which can also be double-clicked) and a `.sh` version for macOS and Linux:

* setup: create `.venv` and install JarLang
* jarlang: run JarLang, e.g. `scripts\jarlang.cmd examples\hello.jlang`
* playground: open the playground in a browser
* test: run the tests
* vscode-extension: build the VS Code extension and install it (needs Node.js)

The scripts run setup automatically if `.venv` doesn't exist yet. In VS Code, the same tasks can be run from Terminal > Run Task.

### Other Commands

* `jarlang asm example.jlang`: print the generated assembly code
* `jarlang check example.jlang`: check for errors without running the code
* `jarlang fmt example.jlang`: format the code
* `jarlang ast example.jlang`: print the tree of nodes
* `jarlang bench example.jlang`: compare the speed of the interpreter and native compiler
* `jarlang playground`: open the playground in a browser
* `jarlang doctor`: show which C compilers were found

### Editor Support

The `editors/vscode` folder contains a VS Code extension with syntax highlighting and support for the JarLang language server (`jarlang lsp`), which shows errors, hints, and autocomplete while typing. To install it, run `scripts\vscode-extension.cmd` or `bash scripts/vscode-extension.sh`.

The `playground` folder contains a web page that runs JarLang in the browser. To open it, run `scripts\playground.cmd`, `bash scripts/playground.sh`, or `jarlang playground`.

### Running the Tests

Run `scripts\test.cmd` or `bash scripts/test.sh`. This is the same as running `pytest` after installing JarLang with `pip install -e ".[dev]"`. The tests also compile the examples to native code and check that they output the same result as the interpreter.

### Known Issues & Limitations

The native compiler supports a subset of the language: integers, decimals, booleans, strings, lists, and functions. Programs that use dicts, closures, or try/catch have to be run with the interpreter. Native integers are also limited to 64 bits, so very large numbers that the interpreter can handle will cause an overflow error.

### Differences from JarLang 1

* Use `_` instead of commas in numbers, e.g. `9_000`
* A semicolon at the end of a line no longer runs the assembly code. Use `jarlang run --native` or `:native` instead
* `python main.py` has been replaced by the `jarlang` command (`jarlang -f example.jlang` still works)
