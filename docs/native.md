# Native Compiler
The native compiler converts JarLang code into x86-64 assembly code, then uses a C compiler to assemble and link it into an executable. A program that compiles natively outputs the same result as the interpreter, which is checked by the tests.

* `jarlang build file.jlang`: create an executable
* `jarlang run --native file.jlang`: compile and run the code
* `jarlang asm file.jlang`: print the assembly code
* `jarlang explain file.jlang`: print the types the compiler worked out

### Supported Code
The native compiler supports a subset of the language:

* Integers (64 bits), decimals, booleans, strings, nil, and lists
* Functions defined with `fn` or `f(x) = ...`, including recursion and default values
* If, while, and for loops, break, continue, and return
* Strings with values inside them, list comprehensions, pipelines, and method calls
* 65 of the built-in functions (marked with (native) in [builtins.md](builtins.md)), including `input`

Dicts, anonymous functions, closures, functions inside functions, try and catch, throw, imports, and `argv` have to be run with the interpreter. `jarlang check --native file.jlang` shows what isn't supported without compiling.

### Types
Since JarLang doesn't require types, the compiler first works out the type of every variable, function, and expression. The types are int, float, bool, str, nil, and list.

Each function is compiled once for every combination of argument types it is called with. For example, `square(3)` and `square(1.5)` create two versions of `square`, one for integers and one for decimals.

The type of a variable is worked out from the values assigned to it, and the return type of a function from its return statements and last value. `push(xs, 1)` and `xs[i] = 1` set the type of the values in a list, so `xs = []` also works. The compiler goes through the program several times until the types stop changing. For example, in the first pass it finds that `fib` returns `n`, an int, and in the second pass it uses that to work out the type of `fib(n - 1) + fib(n - 2)`.

A variable can't hold both an int and a decimal. The interpreter would print `1` for an int where native code would print `1.0`, so the compiler shows an error and suggests writing `0.0` instead of `0`. Adding an int and a decimal still works and returns a decimal.

### Code Generation
`codegen.py` generates the assembly code for each node:

* Integers, booleans, strings, and lists are stored in the %rax register and decimals in the %xmm0 register
* Binary operations generate the left side, save it on the stack, generate the right side, then combine them. If the right side is a number or variable, it is used directly
* Each function has a stack frame with a slot for each parameter, variable, and temporary value
* Arguments to JarLang functions are pushed onto the stack, and the result is returned in %rax or %xmm0
* Calls to C functions use the Linux and macOS calling convention (System V) or the Windows one (arguments in registers plus 32 bytes of shadow space)
* The compiler keeps track of how much is on the stack, so the stack is always aligned to 16 bytes before a call

Integer overflow, division by zero, list indexes, and math errors are checked, and stop the program with the same error message as the interpreter. Variables that might be used before they are assigned are also checked, and recursion deeper than 2500 calls stops with the same error as the interpreter. The assembly code includes comments showing which line of code each part comes from.

### Optimizations
* Calculations with constant numbers are done at compile time, e.g. `2^10` becomes `1024`
* `x^2` and `x^3` are converted to multiplications
* Conditions in if statements and loops jump directly instead of creating a boolean first
* `peephole.py` removes unnecessary instructions, e.g. `pushq %rax` followed by `popq %rcx` becomes `movq %rax, %rcx`

### Runtime
`runtime.c` is a small C library that the generated code calls for:

* Printing, including decimals printed the same way as Python
* String formatting such as `{x:>8.2f}`
* Strings and lists
* Math functions, with the same errors as the interpreter. On Windows, the math functions are loaded from the same library Python uses so the results match exactly

Memory is never freed, since JarLang programs are short.

### C Compilers
`toolchain.py` looks for a C compiler in this order: the `--cc` option or `JARLANG_CC`, then gcc, clang, cc, zig, the ziglang Python package (`pip install ziglang`), and gcc in WSL on Windows. `runtime.c` is compiled once and saved, so later builds are faster. `jarlang doctor` shows which compilers were found.

`--target linux`, `--target windows`, or `--target macos` can be used to generate assembly code for another system. With zig, executables can also be built for another system.

### Known Differences
* Native integers are 64 bits, so very large numbers cause an overflow error instead of working like in the interpreter
* `upper`, `lower`, and `trim` only work with ASCII characters
* The gamma function (e.g. `0.5!`) can be different in the last digit
* An int raised to a negative int variable, e.g. `2^x` with `x = -1`, stops with an error. The interpreter returns `0.5`. Written as a number, `2^-1` works
* Strings can't contain a zero character (`"\0"` or `chr(0)`), since native strings end at a zero byte
