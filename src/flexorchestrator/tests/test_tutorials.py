"""Every marimo tutorial runs from start to finish as a script (integration tier).

A cell that raises fails the run, so this catches a tutorial that no longer
matches the API. It doesn't check the charts look right; export one with
``marimo export html`` to see them.
"""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.timeout(1800)]

TUTORIALS = Path(__file__).parents[3] / "tutorials"
NOTEBOOKS = sorted(p for p in TUTORIALS.glob("[0-9]*.py"))


@pytest.mark.parametrize("notebook", NOTEBOOKS, ids=lambda p: p.stem)
def test_tutorial_runs(notebook):
    pytest.importorskip("marimo")
    pytest.importorskip("altair")
    pytest.importorskip("flexdesign")
    pytest.importorskip("flexparameterize")
    done = subprocess.run(
        [sys.executable, str(notebook)],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-4000:]
