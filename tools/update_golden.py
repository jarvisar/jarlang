"""Regenerates tests/golden/*.out after changing an example or the output format."""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarlang.interpreter import Interpreter, run_with_big_stack  # noqa: E402
from jarlang.source import Source  # noqa: E402


def run_example(path: Path) -> str:
    out = io.StringIO()
    interp = Interpreter(stdout=out, seed=0)
    source = Source(path.read_text(encoding="utf-8"), str(path))
    run_with_big_stack(lambda: interp.run(source))
    return out.getvalue()


def main() -> int:
    golden = ROOT / "tests" / "golden"
    golden.mkdir(parents=True, exist_ok=True)
    for path in sorted((ROOT / "examples").glob("*.jlang")):
        target = golden / (path.stem + ".out")
        text = run_example(path)
        old = target.read_text(encoding="utf-8") if target.exists() else None
        if old != text:
            target.write_text(text, encoding="utf-8", newline="\n")
            print(f"{'updated' if old is not None else 'created'} {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
