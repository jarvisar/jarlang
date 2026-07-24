"""Checks that the examples still print their golden output (see tools/update_golden.py)."""

import sys
from pathlib import Path

import pytest

from support import EXAMPLES, GOLDEN

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from update_golden import run_example  # noqa: E402

EXAMPLE_FILES = sorted(EXAMPLES.glob("*.jlang"))


def test_every_example_has_a_golden_file():
    missing = [p.name for p in EXAMPLE_FILES if not (GOLDEN / (p.stem + ".out")).exists()]
    assert not missing, f"run tools/update_golden.py (missing: {missing})"


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=[p.stem for p in EXAMPLE_FILES])
def test_example_output(path):
    golden = GOLDEN / (path.stem + ".out")
    if not golden.exists():
        pytest.skip("no golden file yet")
    assert run_example(path) == golden.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=[p.stem for p in EXAMPLE_FILES])
def test_examples_have_no_lint_errors(path):
    from jarlang.analysis import check_source
    from jarlang.source import Source
    diags = check_source(Source(path.read_text(encoding="utf-8"), str(path)))
    assert [d.render() for d in diags] == []
