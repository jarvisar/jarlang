"""The jarlang command line interface."""

from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import sys
import time
from typing import Sequence

from . import __version__
from .errors import Diagnostic, JarLangError
from .source import Source
from .term import configure_utf8, style, supports_color

COMMANDS = ("run", "repl", "check", "fmt", "build", "asm", "tokens", "ast", "doctor", "bench", "lsp", "explain", "playground")

EPILOG = """\
examples:
  jarlang                          start the interactive REPL
  jarlang hello.jlang              run a program
  jarlang -c "print(2^10)"         run a snippet
  jarlang run --native fib.jlang   compile to x86-64 and run natively
  jarlang build fib.jlang -o fib   produce a standalone executable
  jarlang asm fib.jlang            show the generated assembly
  jarlang check *.jlang            find errors without running
  jarlang fmt -w src/              format files in place
  jarlang ast --format mermaid f.jlang
  jarlang playground               open the web playground
"""


def _read_source(path: str) -> Source:
    if path == "-":
        return Source(sys.stdin.read(), "<stdin>")
    # Windows raises PermissionError for folders, so check first
    if os.path.isdir(path):
        raise SystemExit(_fatal(f"{path} is a directory"))
    try:
        # utf-8-sig drops the byte order mark that some Windows editors add
        with open(path, encoding="utf-8-sig") as fh:
            return Source(fh.read(), path)
    except FileNotFoundError:
        raise SystemExit(_fatal(f"file not found: {path}"))
    except UnicodeDecodeError:
        raise SystemExit(_fatal(f"{path} is not a UTF-8 text file"))
    except OSError as e:
        raise SystemExit(_fatal(f"cannot read {path}: {e.strerror or e}"))


def _fatal(message: str) -> int:
    print(style("error: ", "bold bright_red", supports_color(sys.stderr)) + message, file=sys.stderr)
    return 1


def _report(err: JarLangError) -> int:
    print(err.render(supports_color(sys.stderr)), file=sys.stderr)
    return 1


def _print_diagnostics(diags: list[Diagnostic]) -> None:
    color = supports_color(sys.stderr)
    for d in diags:
        print(d.render(color), file=sys.stderr)
        print(file=sys.stderr)


# Commands

def cmd_run(args: argparse.Namespace) -> int:
    argv = list(args.args)
    if args.code is not None:
        source = Source(args.code, "<command line>")
        # There's no file with -c, so the first word after the code is an argument too
        if args.file is not None:
            argv.insert(0, args.file)
    else:
        source = _read_source(args.file or "-")
    if args.native:
        return _run_native(source, args, argv)
    from .builtins import ProgramExit
    from .interpreter import Interpreter, run_with_big_stack

    interp = Interpreter(seed=args.seed, trace=sys.stderr if args.trace else None)
    interp.globals["argv"] = argv
    start = time.perf_counter()
    try:
        compiled = interp.compile(source)
        if args.warn:
            _print_diagnostics(compiled.warnings)
        run_with_big_stack(compiled.run)
    except JarLangError as e:
        sys.stdout.flush()
        return _report(e)
    except ProgramExit as e:
        return e.code
    except KeyboardInterrupt:
        return 130
    finally:
        sys.stdout.flush()
    if args.time:
        elapsed = time.perf_counter() - start
        print(style(f"[interpreter: {elapsed * 1000:.1f} ms]", "gray", supports_color(sys.stderr)), file=sys.stderr)
    return 0


def _run_native(source: Source, args: argparse.Namespace, argv: list[str]) -> int:
    from .native.driver import run_native
    from .native.toolchain import ToolchainError

    try:
        result = run_native(source, cc=args.cc, opt=not args.no_opt, capture=False, argv=argv)
    except JarLangError as e:
        return _report(e)
    except ToolchainError as e:
        return _fatal(str(e))
    if args.time:
        color = supports_color(sys.stderr)
        print(style(f"[native: compiled in {result.compile_time * 1000:.0f} ms, ran in "
                    f"{result.run_time * 1000:.1f} ms]", "gray", color), file=sys.stderr)
    return result.returncode


def cmd_repl(args: argparse.Namespace) -> int:
    from .repl import Repl
    return Repl().run()


def cmd_check(args: argparse.Namespace) -> int:
    from .analysis import check_source

    total_errors = total_warnings = 0
    files = _expand(args.files)
    for path in files:
        source = _read_source(path)
        diags = check_source(source, native=args.native)
        errors = [d for d in diags if d.severity == "error"]
        warnings = [d for d in diags if d.severity == "warning"]
        total_errors += len(errors)
        total_warnings += len(warnings)
        if args.format == "json":
            for d in diags:
                line, col = d.span.source.line_col(d.span.start) if d.span else (0, 0)
                print(json.dumps({"file": path, "line": line, "column": col,
                                  "severity": d.severity, "message": d.message}))
        else:
            _print_diagnostics(errors + ([] if args.quiet else warnings))
    if args.format != "json":
        color = supports_color(sys.stderr)
        summary = f"checked {len(files)} file{'s' if len(files) != 1 else ''}: " \
                  f"{total_errors} error{'s' if total_errors != 1 else ''}, " \
                  f"{total_warnings} warning{'s' if total_warnings != 1 else ''}"
        spec = "bold bright_red" if total_errors else ("bold bright_yellow" if total_warnings else "bold bright_green")
        print(style(summary, spec, color), file=sys.stderr)
    return 1 if total_errors else 0


def _expand(paths: Sequence[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                # Skip folders like .git, .venv and node_modules
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "node_modules")
                for n in sorted(names):
                    if n.endswith(".jlang"):
                        out.append(os.path.join(root, n))
        else:
            out.append(p)
    return out


def cmd_fmt(args: argparse.Namespace) -> int:
    from .formatter import format_source

    paths = args.files
    if not paths:
        if sys.stdin.isatty():
            return _fatal("no files given (use - to read from stdin)")
        paths = ["-"]
    changed = failed = 0
    for path in _expand(paths):
        source = _read_source(path)
        try:
            formatted = format_source(source)
        except JarLangError as e:
            _report(e)
            failed += 1
            continue
        if not (args.check or args.write or args.diff):
            # Print the code even when it doesn't change, so fmt works as a filter
            sys.stdout.write(formatted)
            continue
        if formatted == source.text:
            continue
        changed += 1
        if args.check:
            print(f"would reformat {source.name}", file=sys.stderr)
        elif args.diff:
            import difflib
            sys.stdout.writelines(difflib.unified_diff(
                source.text.splitlines(True), formatted.splitlines(True), source.name, source.name + " (formatted)"))
        elif path == "-":
            sys.stdout.write(formatted)
        else:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(formatted)
            print(f"formatted {path}", file=sys.stderr)
    return 1 if failed or (args.check and changed) else 0


def cmd_build(args: argparse.Namespace) -> int:
    from .native.driver import build_executable, compile_to_asm
    from .native.toolchain import ToolchainError

    source = _read_source(args.file)
    try:
        if args.emit == "asm":
            asm = compile_to_asm(source, target=args.target, opt=not args.no_opt)
            out = args.output or os.path.splitext(args.file)[0] + ".s"
            if out == "-":
                sys.stdout.write(asm)
            else:
                with open(out, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(asm)
                print(f"wrote {out}", file=sys.stderr)
            return 0
        start = time.perf_counter()
        exe = build_executable(source, args.output, cc=args.cc, target=args.target,
                               opt=not args.no_opt, keep_asm=args.keep_asm)
        elapsed = time.perf_counter() - start
        color = supports_color(sys.stderr)
        print(style("built ", "bold bright_green", color) + f"{exe} in {elapsed:.2f}s", file=sys.stderr)
        return 0
    except JarLangError as e:
        return _report(e)
    except ToolchainError as e:
        return _fatal(str(e))


def cmd_asm(args: argparse.Namespace) -> int:
    from .native.driver import compile_to_asm, highlight_asm

    source = _read_source(args.file)
    try:
        asm = compile_to_asm(source, target=args.target, opt=not args.no_opt)
    except JarLangError as e:
        return _report(e)
    sys.stdout.write(highlight_asm(asm, supports_color(sys.stdout)))
    if not asm.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def cmd_tokens(args: argparse.Namespace) -> int:
    from .lexer import Lexer

    source = _read_source(args.file)
    try:
        tokens = Lexer(source).tokenize()
    except JarLangError as e:
        return _report(e)
    for tok in tokens:
        line, col = source.line_col(tok.span.start)
        print(f"{line:>4}:{col:<4} {tok.kind.name:<14} {tok.text!r}")
    return 0


def cmd_ast(args: argparse.Namespace) -> int:
    from .parser import parse
    from .tree import render_tree, to_dot, to_json, to_mermaid

    if args.code is not None:
        source = Source(args.code, "<command line>")
    elif args.file is None and sys.stdin.isatty():
        return _fatal("no file given (use -c to parse code, or - to read from stdin)")
    else:
        source = _read_source(args.file or "-")
    try:
        program = parse(source)
    except JarLangError as e:
        return _report(e)
    if args.format == "tree":
        print(render_tree(program, supports_color(sys.stdout)))
    elif args.format == "dot":
        print(to_dot(program))
    elif args.format == "mermaid":
        print(to_mermaid(program))
    elif args.format == "sexpr":
        from .ast import dump
        for stmt in program.stmts:
            print(dump(stmt))
    else:
        print(json.dumps(to_json(program), indent=2, ensure_ascii=False))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    import importlib.util
    import platform
    from .native.toolchain import describe_toolchains

    print(f"JarLang {__version__}")
    print(f"Python: {platform.python_version()} ({sys.executable})")
    print(f"Platform: {platform.system()} {platform.machine()}")
    found = importlib.util.find_spec("prompt_toolkit") is not None
    print("prompt_toolkit: " + ("installed" if found else "not installed (pip install prompt_toolkit)"))
    print("C compilers:")
    found_any = False
    for info in describe_toolchains():
        found_any |= info["ok"]
        print(f"  {info['name']}: {info['detail']}")
    if not found_any:
        print("No C compiler found. Install gcc or clang, or run: pip install ziglang")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from .bench import bench_file
    return bench_file(args.file, repeat=args.repeat, cc=args.cc)


def cmd_playground(args: argparse.Namespace) -> int:
    from .serve import serve
    return serve(port=args.port, open_browser=not args.no_browser)


def cmd_lsp(args: argparse.Namespace) -> int:
    from .lsp import serve
    return serve()


def cmd_explain(args: argparse.Namespace) -> int:
    from .native.driver import explain
    source = _read_source(args.file)
    try:
        print(explain(source, supports_color(sys.stdout)))
    except JarLangError as e:
        return _report(e)
    return 0


# Argument parsing

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarlang",
        description="JarLang, a small programming language for math with an interpreter and a native x86-64 compiler.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"JarLang {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    targets = ["linux", "windows", "macos"]

    def native_opts(p: argparse.ArgumentParser) -> None:
        p.add_argument("--cc", help="C compiler to use (default: auto-detect)")
        p.add_argument("--no-opt", action="store_true", help="turn off the optimizer")

    p = sub.add_parser("run", help="run a program (default when given a file)")
    p.add_argument("file", nargs="?", help="the .jlang file to run (- for stdin)")
    p.add_argument("args", nargs=argparse.REMAINDER, help="arguments available to the program as `argv`")
    p.add_argument("-c", dest="code", help="run this code instead of a file")
    p.add_argument("--native", action="store_true", help="compile to x86-64 and run the executable")
    p.add_argument("--time", action="store_true", help="report how long the program took")
    p.add_argument("--warn", action="store_true", help="show lint warnings before running")
    p.add_argument("--trace", action="store_true", help="echo each statement to stderr as it runs")
    p.add_argument("--seed", type=int, help="seed for random()")
    native_opts(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("repl", help="start the interactive REPL (default with no arguments)")
    p.set_defaults(func=cmd_repl)

    p = sub.add_parser("check", help="report errors and warnings without running")
    p.add_argument("files", nargs="+", help="files or folders to check (- for stdin)")
    p.add_argument("--native", action="store_true", help="also check that the native compiler supports the code")
    p.add_argument("--format", choices=["text", "json"], default="text", help="output format (default: text)")
    p.add_argument("-q", "--quiet", action="store_true", help="only show errors")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("fmt", help="format source files")
    p.add_argument("files", nargs="*", help="files or folders to format (- or nothing for stdin)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("-w", "--write", action="store_true", help="rewrite files in place")
    g.add_argument("--check", action="store_true", help="exit with 1 if any file would change")
    g.add_argument("--diff", action="store_true", help="print a unified diff")
    p.set_defaults(func=cmd_fmt)

    p = sub.add_parser("build", help="compile to a native executable")
    p.add_argument("file", help="the .jlang file to compile")
    p.add_argument("-o", "--output", help="output path")
    p.add_argument("--emit", choices=["exe", "asm"], default="exe", help="create an executable or only the .s file")
    p.add_argument("--target", choices=targets, help="target OS (default: this machine)")
    p.add_argument("--keep-asm", action="store_true", help="keep the generated .s file next to the executable")
    native_opts(p)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("asm", help="print the generated x86-64 assembly")
    p.add_argument("file", help="the .jlang file to compile")
    p.add_argument("--target", choices=targets, help="target OS (default: this machine)")
    p.add_argument("--no-opt", action="store_true", help="turn off the optimizer")
    p.set_defaults(func=cmd_asm)

    p = sub.add_parser("explain", help="show inferred native types for each function")
    p.add_argument("file", help="the .jlang file to check")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("tokens", help="print the token stream")
    p.add_argument("file", help="the .jlang file to read (- for stdin)")
    p.set_defaults(func=cmd_tokens)

    p = sub.add_parser("ast", help="print the syntax tree")
    p.add_argument("file", nargs="?", help="the .jlang file to parse (- for stdin)")
    p.add_argument("-c", dest="code", help="parse this code instead of a file")
    p.add_argument("--format", choices=["tree", "sexpr", "json", "dot", "mermaid"], default="tree",
                   help="output format (default: tree)")
    p.set_defaults(func=cmd_ast)

    p = sub.add_parser("bench", help="compare interpreter and native performance")
    p.add_argument("file", help="the .jlang file to time")
    p.add_argument("--repeat", type=int, default=1, help="run each version this many times (default: 1)")
    p.add_argument("--cc", help="C compiler to use (default: auto-detect)")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("doctor", help="show environment and native toolchain status")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("playground", help="build the web playground and open it in a browser")
    p.add_argument("--port", type=int, default=8000, help="port to serve on (default: 8000)")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser")
    p.set_defaults(func=cmd_playground)

    p = sub.add_parser("lsp", help="run the language server (stdio)")
    p.set_defaults(func=cmd_lsp)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_utf8()
    argv = list(sys.argv[1:] if argv is None else argv)

    # Support jarlang -f file.jlang from JarLang 1
    if argv[:1] in (["-f"], ["--file"]):
        argv = ["run"] + argv[1:]
    # Handle jarlang -c "code", jarlang file.jlang args and jarlang - (stdin)
    if argv[:1] == ["-c"]:
        argv = ["run"] + argv
    elif argv and argv[0] not in COMMANDS and (argv[0] == "-" or not argv[0].startswith("-")):
        argv = ["run"] + argv
    if not argv:
        argv = ["repl"]

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    if args.command == "run" and args.code is None and args.file is None and sys.stdin.isatty():
        return cmd_repl(args)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except OSError as e:
        if not _stdout_closed(e):
            raise
        # Python flushes stdout again at exit, so send it to devnull to avoid a second error
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 0


def _stdout_closed(e: OSError) -> bool:
    """Check if the output was piped into a program that stopped reading, as in `jarlang tokens f.jlang | head`."""
    if isinstance(e, BrokenPipeError):
        return True
    # Windows raises EINVAL instead of EPIPE, so check that stdout really is broken
    if e.errno != errno.EINVAL:
        return False
    try:
        sys.stdout.flush()
    except OSError:
        return True
    # The failed write can leave nothing to flush, so EINVAL while writing to a pipe counts too
    try:
        return stat.S_ISFIFO(os.fstat(sys.stdout.fileno()).st_mode)
    except (OSError, ValueError):
        return False


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
