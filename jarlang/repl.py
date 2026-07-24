"""The interactive REPL (uses prompt_toolkit if it's installed)."""

from __future__ import annotations

import os
import re
import sys
import time
from typing import Callable, Iterable

from . import __version__
from .builtins import BUILTINS, ProgramExit
from .errors import JarLangError, MultipleErrors, ParseError
from .highlight import classify, highlight
from .interpreter import Interpreter, run_with_big_stack
from .lexer import Lexer
from .source import Source
from .term import style, supports_color
from .tokens import KEYWORDS
from .values import Builtin, repr_value, type_name

HELP = """\
Enter code to run it. The last result is saved in `ans`.

  :help               show this help
  :vars               list variables
  :type <code>        show the type of a value
  :doc <name>         show the description of a built-in function
  :ast <code>         show the tree of nodes
  :tokens <code>      show the tokens
  :asm <code>         show the assembly code
  :native <code>      compile the code to native code and run it
  :time <code>        run code and show how long it took
  :load <file>        run a file
  :reset              delete all variables
  :clear              clear the screen
  :quit               exit (or type `exit`)
"""


class Repl:
    def __init__(self, color: bool | None = None) -> None:
        self.color = supports_color(sys.stdout) if color is None else color
        self.interp = Interpreter()
        self.interp.globals["ans"] = 0
        self.counter = 0
        self._read: Callable[[str], str] = self._make_reader()

    # Input
    def _make_reader(self) -> Callable[[str], str]:
        if not sys.stdin.isatty():
            return self._plain_input
        try:
            return self._make_prompt_toolkit_reader()
        except Exception:
            try:
                import readline  # noqa: F401  (adds line editing)
            except ImportError:
                pass
            return self._plain_input

    def _plain_input(self, prompt: str) -> str:
        return input(prompt)

    def _make_prompt_toolkit_reader(self) -> Callable[[str], str]:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.history import FileHistory, InMemoryHistory

        lexer, completer, pt_style = prompt_toolkit_parts(lambda: self.interp.globals)
        history_path = os.path.join(os.path.expanduser("~"), ".jarlang_history")
        try:
            history = FileHistory(history_path)
        except OSError:
            history = InMemoryHistory()
        session = PromptSession(
            lexer=lexer,
            completer=completer,
            style=pt_style,
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            complete_while_typing=False,
            enable_history_search=True,
        )

        def read(prompt: str) -> str:
            return session.prompt(ANSI(prompt))
        return read

    # Output helpers
    def s(self, text: str, spec: str) -> str:
        return style(text, spec, self.color)

    def print_error(self, err: JarLangError) -> None:
        print(err.render(self.color), file=sys.stdout)

    # Main loop
    def banner(self) -> str:
        return f"JarLang {__version__}\nType :help for help or :quit to exit."

    def run(self) -> int:
        print(self.banner())
        while True:
            try:
                text = self.read_input()
            except EOFError:
                print()
                return 0
            except KeyboardInterrupt:
                print(self.s("(interrupted, use :quit to exit)", "gray"))
                continue
            if text is None:
                continue
            stripped = text.strip()
            if stripped in ("exit", "quit", ":q", ":quit", ":exit"):
                return 0
            try:
                if stripped.startswith(":"):
                    self.command(stripped)
                else:
                    self.evaluate(text)
            except ProgramExit as e:
                return e.code
            except KeyboardInterrupt:
                print(self.s("interrupted", "bold bright_red"))

    def read_input(self) -> str | None:
        prompt = self.s("jl> ", "bold bright_magenta")
        cont = self.s("..> ", "gray")
        lines = [self._read(prompt)]
        if not lines[0].strip() or lines[0].strip().startswith(":"):
            return lines[0]
        while self._incomplete("\n".join(lines)):
            try:
                more = self._read(cont)
            except EOFError:
                break
            if not more.strip():  # a blank line ends the entry
                break
            lines.append(more)
        return "\n".join(lines)

    @staticmethod
    def _incomplete(text: str) -> bool:
        from .parser import parse
        try:
            parse(Source(text, "<repl>"))
        except ParseError as e:
            return e.incomplete
        except MultipleErrors as e:
            return bool(getattr(e, "incomplete", False))
        except JarLangError as e:
            # Strings can span lines, so this error only happens when the input ends inside one
            return e.message == "unterminated string literal"
        return False

    def evaluate(self, text: str) -> None:
        self.counter += 1
        source = Source(text, "<repl>")
        try:
            compiled = self.interp.compile(source)
            for w in compiled.warnings:
                if "unused variable" not in w.message:
                    print(w.render(self.color))
            value = run_with_big_stack(compiled.run)
        except JarLangError as e:
            self.print_error(e)
            return
        # Skip nil so `print(x)` doesn't overwrite the last real result
        if compiled.ends_with_expression and value is not None:
            self.interp.globals["ans"] = value
            text_value = repr_value(value)
            print(highlight(text_value, self.color) if self.color else text_value)

    # Commands
    def command(self, line: str) -> None:
        name, _, arg = line[1:].partition(" ")
        arg = arg.strip()
        handler = getattr(self, "cmd_" + name, None)
        if handler is None:
            print(f"unknown command :{name}, type :help to see all commands")
            return
        usage = re.search(rf"^  (:{name} <\w+>)", HELP, re.M)
        if usage and not arg:
            print(f"usage: {usage.group(1)}")
            return
        handler(arg)

    def cmd_help(self, arg: str) -> None:
        print(HELP)

    def cmd_vars(self, arg: str) -> None:
        user = {k: v for k, v in self.interp.globals.items()}
        if not user:
            print("(no variables yet)")
            return
        width = max(len(k) for k in user)
        for k, v in sorted(user.items()):
            text = repr_value(v)
            if len(text) > 60:
                text = text[:57] + "..."
            print(f"{self.s(k.ljust(width), 'bold')}  {self.s(type_name(v).ljust(5), 'gray')}  {text}")

    def cmd_type(self, arg: str) -> None:
        try:
            value = self.interp.run(Source(arg, "<repl>"))
        except JarLangError as e:
            self.print_error(e)
            return
        print(type_name(value))

    def cmd_doc(self, arg: str) -> None:
        b = BUILTINS.get(arg)
        if isinstance(b, Builtin):
            print(self.s(b.signature, "bold") + "\n  " + b.doc)
        elif arg in self.interp.globals:
            from .builtins import bi_doc
            print(bi_doc(self.interp, self.interp.globals[arg]))
        elif b is not None:
            from .builtins import CONSTANT_DOCS
            print(f"{arg} = {b!r}  {CONSTANT_DOCS.get(arg, '')}")
        else:
            print(f"no documentation for `{arg}`")

    def cmd_ast(self, arg: str) -> None:
        from .parser import parse
        from .tree import render_tree
        try:
            program = parse(Source(arg, "<repl>"))
        except JarLangError as e:
            self.print_error(e)
            return
        print(render_tree(program, color=self.color))

    def cmd_tokens(self, arg: str) -> None:
        try:
            tokens = Lexer(Source(arg, "<repl>")).tokenize()
        except JarLangError as e:
            self.print_error(e)
            return
        print(" ".join(repr(t) for t in tokens))

    def _native_source(self, arg: str) -> str:
        from .parser import parse
        from . import ast
        try:
            program = parse(Source(arg, "<repl>"))
        except JarLangError:
            return arg
        if program.stmts and isinstance(program.stmts[-1], ast.ExprStmt):
            last = program.stmts[-1]
            if not (isinstance(last.expr, ast.Call) and isinstance(last.expr.callee, ast.Name)
                    and last.expr.callee.name in ("print", "write")):
                s, e = last.span.start, last.span.end
                return arg[:s] + "print(" + arg[s:e] + ")" + arg[e:]
        return arg

    def cmd_asm(self, arg: str) -> None:
        from .native.driver import compile_to_asm
        try:
            asm = compile_to_asm(Source(self._native_source(arg), "<repl>"))
        except JarLangError as e:
            self.print_error(e)
            return
        from .native.driver import highlight_asm
        print(highlight_asm(asm, self.color))

    def cmd_native(self, arg: str) -> None:
        from .native.driver import run_native
        from .native.toolchain import ToolchainError
        try:
            result = run_native(Source(self._native_source(arg), "<repl>"))
        except JarLangError as e:
            self.print_error(e)
            return
        except ToolchainError as e:
            print(self.s("error: ", "bold bright_red") + str(e))
            return
        sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stdout.write(result.stderr)
        print(self.s(f"[native: compiled in {result.compile_time * 1000:.0f} ms, "
                     f"ran in {result.run_time * 1000:.1f} ms]", "gray"))

    def cmd_time(self, arg: str) -> None:
        start = time.perf_counter()
        self.evaluate(arg)
        elapsed = time.perf_counter() - start
        print(self.s(f"[{elapsed * 1000:.2f} ms]", "gray"))

    def cmd_load(self, arg: str) -> None:
        path = arg.strip().strip('"').strip("'")
        try:
            with open(path, encoding="utf-8-sig") as fh:
                text = fh.read()
        except OSError as exc:
            print(f"cannot read {path}: {exc.strerror}")
            return
        try:
            compiled = self.interp.compile(Source(text, path))
            run_with_big_stack(compiled.run)
        except JarLangError as e:
            self.print_error(e)
            return
        print(self.s(f"loaded {path}", "gray"))

    def cmd_reset(self, arg: str) -> None:
        self.interp = Interpreter()
        self.interp.globals["ans"] = 0
        print("all variables cleared")

    def cmd_clear(self, arg: str) -> None:
        print("\x1b[2J\x1b[H", end="")


def prompt_toolkit_parts(user_names: Callable[[], Iterable[str]]):
    """Build the prompt_toolkit lexer, completer and colors for the REPL."""
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.lexers import Lexer as PTLexer
    from prompt_toolkit.styles import Style

    class JarLexer(PTLexer):
        def lex_document(self, document):
            ranges = classify(document.text)
            lines = document.lines
            starts = []
            offset = 0
            for line in lines:
                starts.append(offset)
                offset += len(line) + 1

            def get_line(lineno: int):
                line = lines[lineno]
                base = starts[lineno]
                frags = []
                pos = 0
                for s, e, cat in ranges:
                    s, e = s - base, e - base
                    if e <= 0 or s >= len(line):
                        continue
                    s, e = max(s, 0), min(e, len(line))
                    if s < pos:
                        continue
                    if s > pos:
                        frags.append(("", line[pos:s]))
                    frags.append((f"class:{cat}", line[s:e]))
                    pos = e
                if pos < len(line):
                    frags.append(("", line[pos:]))
                return frags
            return get_line

    class JarCompleter(Completer):
        def get_completions(self, document, complete_event):
            word = document.get_word_before_cursor(WORD=False)
            if not word or not (word[0].isalpha() or word[0] == "_"):
                return
            names = set(BUILTINS) | set(KEYWORDS) | set(user_names())
            for name in sorted(names):
                if name.startswith(word) and name != word:
                    b = BUILTINS.get(name)
                    meta = b.signature if isinstance(b, Builtin) else ("keyword" if name in KEYWORDS else "")
                    yield Completion(name, start_position=-len(word), display_meta=meta)

    pt_style = Style.from_dict({
        "keyword": "ansimagenta bold", "constant": "ansicyan", "number": "ansicyan",
        "string": "ansigreen", "comment": "ansibrightblack italic", "builtin": "ansiblue",
        "function": "ansiyellow", "operator": "ansiyellow",
    })
    return JarLexer(), JarCompleter(), pt_style
