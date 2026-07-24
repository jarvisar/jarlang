# JarLang for VS Code

This extension adds JarLang 2.0 support to Visual Studio Code. It includes syntax highlighting, snippets, and commands to run JarLang files, and it uses the JarLang language server (`jarlang lsp`) to show errors, hints, and autocomplete while typing.

```jarlang
f(x) = x^2 + 1
fn fib(n: int) -> int {
    if n < 2 { return n }
    fib(n - 1) + fib(n - 2)
}
print("f(3) = {f(3)}, fib(20) = {fib(20):>6}")
1..10 |> filter(is_prime) |> sum
```

## Features

* Syntax highlighting: covers the whole language, including string interpolation with format specs (`"{name:>8}"`), escapes, hex, binary, and octal numbers, math-style functions (`f(x) = ...`), type annotations, pipelines, unicode math symbols (`√ π × ÷ ≤ ≥ ≠ ∞`), and all built-in functions
* Errors and warnings: shows the same errors and warnings as `jarlang check` while typing. Unused variables and unreachable code are faded out
* Quick fixes: apply "did you mean `len`?" suggestions, and add `_` to the start of unused variables
* Hover: shows documentation for built-in functions, constants, keywords, number literals, format specs, variables, and your own functions (with their signature and the comment above them)
* Autocomplete: variables in scope, functions, built-in functions (with their signatures and docs), keywords, type names, and methods after `.`, even when the file has syntax errors
* Signature help: shows the parameters while typing the arguments of a call, `xs.method(...)`, or `xs |> f(...)`
* Navigation: go to definition, find all references, highlight occurrences, and rename (including names used inside string interpolations)
* Outline: lists the functions and variables in the file
* Folding: blocks and runs of comments can be folded
* Formatting: formats the file with `jarlang fmt`
* Snippets: `fn`, `fna`, `mf`, `if`, `ife`, `for`, `fori`, `while`, `try`, `print`, `pf`, and `comp`

## Commands

These commands are in the command palette under JarLang:

* Run File: runs the file in the terminal. Also runs with <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>R</kbd> (<kbd>Cmd</kbd>+<kbd>Alt</kbd>+<kbd>R</kbd> on a Mac) or the ▶ button in the editor title
* Run File Natively (x86-64): compiles the file to native code and runs it in the terminal
* Show Assembly: shows the generated assembly code in a new editor
* Show Syntax Tree: shows the tree of nodes in a new editor
* Restart Language Server: restarts the JarLang language server

## Requirements

The editor features come from the JarLang language server, which is included with JarLang. To use it, either:

* Install JarLang so the `jarlang` command is on your `PATH` (run `pip install -e .` in the JarLang repository), or
* Set `jarlang.pythonPath` to a Python interpreter that has JarLang installed, e.g. `${workspaceFolder}/.venv/Scripts/python` on Windows or `${workspaceFolder}/.venv/bin/python` on Linux and macOS

Without the language server, syntax highlighting and snippets still work, and the extension shows a warning. Run File Natively also needs a C compiler. Run `jarlang doctor` to see which C compilers were found.

## Installing the Extension

From the root of the repository, run `scripts\vscode-extension.cmd` on Windows or `bash scripts/vscode-extension.sh` on macOS and Linux. The script does the steps below.

1. Change directory into the extension folder and install its dependencies:

	`cd editors/vscode`

	`npm install`

2. Build the extension package:

	`npm run package`

3. Install the package into VS Code:

	`code --install-extension jarlang.vsix`

This runs `vsce package` with `--allow-missing-repository` and `--skip-license`, which are needed since `package.json` has no `repository` field and the repository has no license file. Don't use `--no-dependencies`, since the package needs to include `vscode-languageclient`.

To try the extension without installing it, open the `editors/vscode` folder in VS Code and press <kbd>F5</kbd> (Run JarLang Extension). This opens a new VS Code window with the extension loaded and the `examples` folder open.

## Settings

* `jarlang.path`: command used to run JarLang, either `jarlang` if it is on your `PATH` or the full path to it (default: `jarlang`)
* `jarlang.pythonPath`: if set, JarLang runs as `<python> -m jarlang` with this Python interpreter instead (default: empty)
* `jarlang.lsp.enabled`: start the language server (default: `true`)
* `jarlang.trace.server`: log the messages between VS Code and the language server in the JarLang output channel (`off`, `messages`, or `verbose`, default: `off`)

## Developing the Extension

The extension is plain JavaScript, so it doesn't need to be built. After running `npm install`, run `npm test` to check the syntax highlighting. The test tokenizes sample code with the same TextMate engine that VS Code uses.

The grammar file (`syntaxes/jarlang.tmLanguage.json`) is generated from the JarLang source code (the keywords and built-in functions) by `scripts/gen-grammar.py`. To change the grammar, edit the script instead of the JSON file, then run `python scripts/gen-grammar.py` and `npm test`.

The language server is part of the JarLang package (`jarlang/lsp.py`) and is tested by `tests/test_lsp.py`. Set `jarlang.trace.server` to `verbose` to see the messages sent to the server.
