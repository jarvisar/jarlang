# Tools
All of the tools are run with the `jarlang` command (or `python -m jarlang`).

### Running Code
* `jarlang file.jlang a b`: run a file. The program can read `a` and `b` from the `argv` variable
* `jarlang -c "print(2^100)"`: run code from the command line
* `jarlang - < file.jlang`: read the program from stdin
* `jarlang run --time file.jlang`: also print how long the program took
* `jarlang run --warn file.jlang`: also print warnings, such as unused variables
* `jarlang run --trace file.jlang`: print each line as it runs
* `jarlang run --seed 42 file.jlang`: use the same random numbers each time
* `jarlang -f file.jlang`: the command from JarLang 1, which still works

Errors are printed to stderr and the program exits with code 1 (or the code passed to `exit(n)`).

For example, `--trace` shows each line as it runs, indented by how deep the function calls are:

```
fact.jlang:5    x = fact(3)
fact.jlang:2      if n <= 1 { return 1 }
fact.jlang:3      n * fact(n - 1)
fact.jlang:2        if n <= 1 { return 1 }
```

### Interactive Interpreter
Run `jarlang` with no arguments to start the interactive interpreter. The result of each expression is printed and saved in the `ans` variable. Code with open brackets continues on the next line, and an empty line ends it. Input `exit` to exit.

If `prompt_toolkit` is installed (`pip install prompt_toolkit`), it also highlights code as you type, completes names with the tab key, and saves history.

* `:help`: show all commands
* `:vars`: list variables and their types
* `:type <code>`: show the type of a value
* `:doc <name>`: show the description of a built-in function
* `:ast <code>`, `:tokens <code>`: show the tree of nodes or the tokens
* `:asm <code>`: show the assembly code
* `:native <code>`: compile the code to native code and run it
* `:time <code>`: run code and show how long it took
* `:load <file>`: run a file
* `:reset`: delete all variables
* `:clear`: clear the screen

### Checking and Formatting
* `jarlang check file.jlang`: report errors and warnings without running the code. Folders can also be checked
* `jarlang check --native file.jlang`: also check that the native compiler supports the code
* `jarlang check --format json file.jlang`: print each error as JSON
* `jarlang fmt file.jlang`: print the formatted code
* `jarlang fmt -w file.jlang`: format the file in place
* `jarlang fmt --check folder`: exit with code 1 if a file isn't formatted
* `jarlang fmt --diff file.jlang`: show what would change

The formatter indents with 2 spaces, lines up comments at the end of lines, keeps blank lines and implicit multiplication, and adds parentheses around `and` inside `or`.

### Viewing the Tokens and Tree
* `jarlang tokens file.jlang`: print the tokens
* `jarlang ast file.jlang`: print the tree of nodes
* `jarlang ast --format dot file.jlang`: print the tree for Graphviz, e.g. `jarlang ast --format dot file.jlang | dot -Tsvg > ast.svg`
* `jarlang ast --format mermaid -c "4 + 2 * 10"`: print the tree as a Mermaid diagram
* `jarlang ast --format json file.jlang`: print the tree as JSON

### Native Code
* `jarlang build file.jlang -o program`: create an executable
* `jarlang build --emit asm file.jlang`: only create the assembly file
* `jarlang build --target linux file.jlang`: build for another system (needs zig)
* `jarlang run --native file.jlang`: compile and run the code
* `jarlang asm file.jlang`: print the assembly code
* `jarlang explain file.jlang`: print the types the compiler worked out
* `jarlang bench file.jlang`: compare the speed of the interpreter and native code
* `jarlang doctor`: show which C compilers were found

Set `JARLANG_CC` or use `--cc` to choose a C compiler, e.g. `JARLANG_CC="zig cc"`.

### Language Server
`jarlang lsp` starts a language server that editors can use. It shows errors and warnings while typing, descriptions of functions when hovering over them, and autocomplete. It also supports going to where a variable is defined, finding where it is used, renaming it, and formatting the file.

### VS Code
The `editors/vscode` folder contains a VS Code extension with syntax highlighting, snippets, and the language server. It also adds commands to run a file, run it natively, and show its assembly code or tree. It uses JarLang from the workspace's `.venv` if there is one, otherwise the `jarlang` command on PATH. To install it, run `scripts\vscode-extension.cmd` or `bash scripts/vscode-extension.sh`. See [editors/vscode/README.md](../editors/vscode/README.md) for more details.

### Playground
The `playground` folder contains a web page that runs JarLang in the browser using Pyodide (Python compiled to WebAssembly). It shows the output, tree, tokens, types, and assembly code for the code in the editor.

Run `jarlang playground` (or `scripts\playground.cmd` / `bash scripts/playground.sh`) to build the playground and open it in a browser. Use `--port` to choose a port and `--no-browser` to not open a browser.

To build the files without starting a server, run `python tools/build_playground.py`. `--out DIR` builds a complete copy of the site that can be uploaded anywhere.

The GitHub Actions workflow in `.github/workflows/pages.yml` publishes the playground to GitHub Pages.

### Using JarLang from Python

```python
import jarlang
jarlang.evaluate("2^10 + 5!")          # 1144
jarlang.run('print("hello")')

from jarlang.interpreter import Interpreter
interp = Interpreter()                  # keeps variables between runs
interp.run("f(x) = x^2")
interp.run("f(12)")                     # 144
```
