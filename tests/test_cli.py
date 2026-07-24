"""Tests for the jarlang command, run as a subprocess."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from support import EXAMPLES, ROOT, requires_native


def jarlang(*args: str, stdin: str | None = None, cwd=None) -> subprocess.CompletedProcess:
    env = dict(os.environ, NO_COLOR="1", PYTHONIOENCODING="utf-8", PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-m", "jarlang", *args], input=stdin, capture_output=True,
                          text=True, encoding="utf-8", env=env, cwd=cwd, timeout=300)


def test_version():
    r = jarlang("--version")
    assert r.returncode == 0 and r.stdout.startswith("JarLang 2.")


def test_run_file_directly_and_with_run(tmp_path):
    prog = tmp_path / "p.jlang"
    prog.write_text('print("hello", 6 * 7)\n', encoding="utf-8")
    for args in ([str(prog)], ["run", str(prog)]):
        r = jarlang(*args)
        assert r.returncode == 0 and r.stdout == "hello 42\n"


def test_legacy_f_flag(tmp_path):
    prog = tmp_path / "old.jlang"
    prog.write_text("print(sqrt(16) + 9_000)\n", encoding="utf-8")
    r = jarlang("-f", str(prog))
    assert r.stdout == "9004.0\n"


def test_dash_c():
    r = jarlang("-c", "print([n! for n in 1..5])")
    assert r.stdout == "[1, 2, 6, 24, 120]\n"


def test_program_arguments(tmp_path):
    prog = tmp_path / "args.jlang"
    prog.write_text("print(argv)\n", encoding="utf-8")
    r = jarlang(str(prog), "a", "b")
    assert r.stdout == '["a", "b"]\n'


def test_trace_flag():
    r = jarlang("run", "--trace", "-c", "fn sq(n) {\n  n * n\n}\nx = sq(3)\nprint(x)")
    assert r.stdout == "9\n"
    lines = r.stderr.splitlines()
    assert lines[0].endswith("x = sq(3)")
    assert lines[1].split(":", 1)[1].split(None, 1)[1] == "n * n"  # indented one call deeper
    assert lines[-1].endswith("print(x)")


def test_errors_go_to_stderr_with_exit_code_1():
    r = jarlang("-c", "print(1)\nprint(nope)")
    assert r.returncode == 1
    assert r.stdout == ""  # name errors are found before the program runs
    assert "undefined variable `nope`" in r.stderr and "-->" in r.stderr


def test_runtime_error_exit_code():
    r = jarlang("-c", 'print("partial")\nprint(1 / 0)')
    assert r.returncode == 1 and r.stdout == "partial\n" and "division by zero" in r.stderr


def test_exit_builtin_sets_status():
    assert jarlang("-c", "exit(4)").returncode == 4


def test_missing_file():
    r = jarlang("run", "definitely_missing.jlang")
    assert r.returncode == 1 and "file not found" in r.stderr


def test_directory_instead_of_file():
    # Windows raises PermissionError for this, which used to print a traceback
    r = jarlang("run", str(EXAMPLES))
    assert r.returncode == 1 and "is a directory" in r.stderr and "Traceback" not in r.stderr


def test_file_with_byte_order_mark(tmp_path):
    prog = tmp_path / "bom.jlang"
    prog.write_bytes(b'\xef\xbb\xbfprint("ok")\n')
    r = jarlang(str(prog))
    assert r.returncode == 0 and r.stdout == "ok\n"


def test_dash_c_keeps_every_argument():
    r = jarlang("-c", "print(argv)", "a", "b")
    assert r.stdout == '["a", "b"]\n'


def test_dash_reads_the_program_from_stdin():
    r = jarlang("-", "x", stdin="print(argv)\n")
    assert r.returncode == 0 and r.stdout == '["x"]\n'


def test_stdin_is_read_as_utf8():
    # Without PYTHONIOENCODING, Windows used to decode piped code with the ANSI code page
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env.update(NO_COLOR="1", PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, "-m", "jarlang", "run", "-"], input='print("π", √16)\n'.encode("utf-8"),
                       capture_output=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert r.stdout.decode("utf-8").strip() == "π 4.0"


def test_closed_stdout_pipe_is_not_an_error():
    env = dict(os.environ, NO_COLOR="1", PYTHONPATH=str(ROOT))
    proc = subprocess.Popen([sys.executable, "-m", "jarlang", "-c", "for i in 1..1000000 { print(i) }"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    assert proc.stdout.readline().strip() == b"1"
    proc.stdout.close()  # like `jarlang ... | head -1`
    stderr = proc.stderr.read()
    proc.stderr.close()
    assert proc.wait(timeout=60) == 0 and b"Traceback" not in stderr, stderr.decode("utf-8", "replace")


def test_check_reports_errors_and_warnings(tmp_path):
    bad = tmp_path / "bad.jlang"
    bad.write_text("fn f() {\n  unused = 1\n}\nprint(missing)\n", encoding="utf-8")
    r = jarlang("check", str(bad))
    assert r.returncode == 1
    assert "undefined variable `missing`" in r.stderr and "unused variable `unused`" in r.stderr
    assert "1 error, 1 warning" in r.stderr
    j = jarlang("check", "--format", "json", str(bad))
    rows = [json.loads(line) for line in j.stdout.splitlines()]
    assert {row["severity"] for row in rows} == {"error", "warning"}


def test_check_examples_directory_is_clean():
    r = jarlang("check", str(EXAMPLES))
    assert r.returncode == 0, r.stderr
    assert "0 errors" in r.stderr


def test_check_native_flag(tmp_path):
    prog = tmp_path / "dicty.jlang"
    prog.write_text("d = {a: 1}\nprint(d)\n", encoding="utf-8")
    assert jarlang("check", str(prog)).returncode == 0
    r = jarlang("check", "--native", str(prog))
    assert r.returncode == 1 and "not supported by the native compiler" in r.stderr


def test_fmt_stdin():
    r = jarlang("fmt", stdin="x=1\n")
    assert r.returncode == 0 and r.stdout == "x = 1\n"
    # Already formatted code is printed too, so fmt can be used as a filter
    r = jarlang("fmt", "-", stdin="x = 1\n")
    assert r.returncode == 0 and r.stdout == "x = 1\n"
    r = jarlang("fmt", "--check", stdin="x=1\n")
    assert r.returncode == 1 and "would reformat <stdin>" in r.stderr
    assert jarlang("fmt", "--check", stdin="x = 1\n").returncode == 0


def test_fmt_write_with_stdin_prints_instead(tmp_path):
    r = jarlang("fmt", "-w", "-", stdin="x=1\n", cwd=tmp_path)
    assert r.returncode == 0 and r.stdout == "x = 1\n"
    assert not (tmp_path / "-").exists()


def test_fmt_folder_keeps_going_after_a_syntax_error(tmp_path):
    (tmp_path / "a.jlang").write_text("x = (\n", encoding="utf-8")
    (tmp_path / "b.jlang").write_text("y=2\n", encoding="utf-8")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "c.jlang").write_text("z=3\n", encoding="utf-8")
    r = jarlang("fmt", "-w", str(tmp_path))
    assert r.returncode == 1 and "expected an expression" in r.stderr
    assert (tmp_path / "b.jlang").read_text(encoding="utf-8") == "y = 2\n"
    assert (tmp_path / ".hidden" / "c.jlang").read_text(encoding="utf-8") == "z=3\n"  # hidden folders are skipped


def test_commands_that_read_stdin_by_default_fail_on_a_terminal(monkeypatch, capsys):
    from jarlang import cli

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert cli.main(["fmt"]) == 1
    assert cli.main(["ast"]) == 1
    assert capsys.readouterr().err.count("no file") == 2


def test_tokens_and_ast():
    r = jarlang("tokens", str(EXAMPLES / "hello.jlang"))
    assert "IDENT" in r.stdout and "FLOAT" not in r.stdout.split("\n")[0]
    for fmt, marker in [("tree", "Program"), ("sexpr", "(assign"), ("dot", "digraph AST"),
                        ("mermaid", "graph TD"), ("json", '"type": "Program"')]:
        r = jarlang("ast", "--format", fmt, "-c", "x = 2 + 3")
        assert r.returncode == 0 and marker in r.stdout, fmt


def test_asm_command_needs_no_toolchain():
    r = jarlang("asm", "--target", "linux", str(EXAMPLES / "primes.jlang"))
    assert r.returncode == 0
    assert "jl_main:" in r.stdout and "# Generated by JarLang" in r.stdout


def test_build_emit_asm(tmp_path):
    out = tmp_path / "prog.s"
    r = jarlang("build", "--emit", "asm", "--target", "windows", "-o", str(out), str(EXAMPLES / "numerics.jlang"))
    assert r.returncode == 0 and out.read_text(encoding="utf-8").startswith("# Generated by JarLang")


def test_explain():
    r = jarlang("explain", str(EXAMPLES / "sorting.jlang"))
    assert r.returncode == 0 and "merge_sort(xs: list[int])" in r.stdout


def test_doctor():
    r = jarlang("doctor")
    assert r.returncode == 0 and "C compilers:" in r.stdout


def test_repl_with_piped_input():
    session = "x = 20\nx + 22\nf(n) = n^2\nf(ans)\n:type 1.5\n:vars\nexit\n"
    r = jarlang("repl", stdin=session)
    assert r.returncode == 0
    assert "42" in r.stdout and "1764" in r.stdout and "float" in r.stdout
    assert "JarLang" in r.stdout  # banner


def test_repl_multiline_and_errors():
    session = "fn sq(n) {\n  n * n\n}\n\nsq(9)\nprint(undefined_thing)\n:quit\n"
    r = jarlang("repl", stdin=session)
    assert "81" in r.stdout and "undefined variable" in r.stdout


def test_repl_asm_command():
    r = jarlang("repl", stdin=":asm 2 + 3 * 4\n:quit\n")
    assert "jl_main" in r.stdout


def test_build_and_run_native(tmp_path):
    requires_native()
    exe = tmp_path / ("fib.exe" if os.name == "nt" else "fib")
    src = tmp_path / "fib.jlang"
    src.write_text("fn fib(n) { if n < 2 { return n }; fib(n - 1) + fib(n - 2) }\nprint(fib(20))\n", encoding="utf-8")
    r = jarlang("build", str(src), "-o", str(exe))
    assert r.returncode == 0, r.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert run.stdout.strip() == "6765"


def test_run_native_flag():
    requires_native()
    r = jarlang("run", "--native", "--time", str(EXAMPLES / "primes.jlang"))
    assert r.returncode == 0 and "primes below 100" in r.stdout and "native: compiled in" in r.stderr


@pytest.mark.parametrize("name", ["hello", "mandelbrot"])
def test_bench(name):
    requires_native()
    r = jarlang("bench", str(EXAMPLES / f"{name}.jlang"))
    assert r.returncode == 0, r.stderr
    assert "speedup" in r.stdout and "outputs match" in r.stdout


def test_bench_rejects_a_non_utf8_file(tmp_path):
    path = tmp_path / "latin1.jlang"
    path.write_bytes("print('caf\xe9')\n".encode("latin-1"))
    r = jarlang("bench", str(path))
    assert r.returncode == 1
    assert "not a UTF-8 text file" in r.stderr and "Traceback" not in r.stderr


def test_einval_on_a_pipe_counts_as_closed(monkeypatch):
    # What GitHub's Windows runners do: the write fails with EINVAL, then a flush succeeds
    import errno
    import io

    from jarlang import cli
    read_end, write_end = os.pipe()
    try:
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.FileIO(write_end, "w", closefd=False)))
        assert cli._stdout_closed(OSError(errno.EINVAL, "Invalid argument"))
        assert not cli._stdout_closed(OSError(errno.ENOENT, "No such file"))
    finally:
        os.close(read_end)
        os.close(write_end)
