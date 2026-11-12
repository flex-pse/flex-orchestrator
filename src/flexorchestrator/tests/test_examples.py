"""Every example runs from start to finish (integration tier).

Each runs in its own process. Processes on one data directory share its
Acquirium, so a holder process starts it once for all of them; starting one
takes about half a minute.

- The step-by-step RO example, the six study-config ones, and the surrogate
  sweep stored on disk (no Acquirium).
- The sample plugin (``examples/plugins/ro_tools``), installed with pip into
  a temporary folder: it loads only when ``FLEXORCH_PLUGINS`` lists it, its
  command gives the same result from Python and from ``flex-orch``, its own
  tests pass, and the CLI exports and imports the study it ran on.
"""

import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.timeout(1800)]

EXAMPLES = Path(__file__).parents[3] / "examples"


def run(command: list[str], env: dict | None = None, cwd: Path | None = None) -> str:
    done = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=900,
        env=env,
        cwd=cwd,
    )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-4000:]
    return done.stdout


def script(path: Path, *args: str, env: dict | None = None) -> str:
    return run([sys.executable, str(path), *args], env=env)


HOLD = """
import sys
from flexorchestrator.connection import connect

connection = connect(data_dir=sys.argv[1])
print("ready", flush=True)
sys.stdin.read()
connection.close()
"""


@contextmanager
def shared_acquirium(data_dir: Path):
    """Keep ``data_dir``'s Acquirium running while the block runs.

    Its first process owns a server and stops it on exit; the others attach.
    The holder is that first process, so each script attaches to it.
    """
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(data_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        lines = []
        for line in holder.stdout:
            if line.strip() == "ready":
                break
            lines.append(line)
        else:
            raise AssertionError("Acquirium didn't start:\n" + "".join(lines))
        yield
    finally:
        holder.stdin.close()
        holder.wait(timeout=120)


@pytest.mark.needs_highs
def test_ro_surrogates():
    pytest.importorskip("acquirium")
    pytest.importorskip("flexparameterize")
    out = script(EXAMPLES / "ro_surrogates" / "example.py")
    for tag in ("fixed_intensity", "linear_surrogate"):
        assert f"\n{tag}: $" in out


STUDY_TAGS = {
    "sweep_surrogates": ["fixed_intensity", "linear_surrogate"],
    "sweep_variables": ["intensity_2.5", "intensity_3.0", "intensity_3.5"],
    "sweep_configuration": ["withdrawal_160", "withdrawal_180", "withdrawal_200"],
    "sweep_nested": [
        f"{s}_{w}"
        for s in ("fixed_intensity", "linear_surrogate")
        for w in (160, 180, 200)
    ],
    "sweep_cases": ["linear_tight_feed", "fixed_loose_feed", "aged_membranes"],
    "model_inputs": ["optimized", "as_operated"],
}


@pytest.fixture(scope="module")
def ro_study_acquirium():
    """A fresh ``ro_study/tmp_flexorch`` and its Acquirium, kept running.

    Yields the scripts' environment, which tells them to keep the store:
    its server holds it open.
    """
    pytest.importorskip("acquirium")
    store = EXAMPLES / "ro_study" / "tmp_flexorch"
    shutil.rmtree(store, ignore_errors=True)
    with shared_acquirium(store):
        yield {**os.environ, "FLEXORCH_KEEP_EXAMPLE_STORE": "1"}


@pytest.fixture
def export_folder():
    """``sweep_nested``'s export folder, removed before and after the test."""
    folder = EXAMPLES / "ro_study" / "export"
    shutil.rmtree(folder, ignore_errors=True)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


@pytest.mark.needs_highs
@pytest.mark.parametrize("name", sorted(STUDY_TAGS))
def test_ro_study(name, export_folder, ro_study_acquirium):
    pytest.importorskip("flexparameterize")
    out = "\n" + script(EXAMPLES / "ro_study" / f"{name}.py", env=ro_study_acquirium)
    for tag in STUDY_TAGS[name]:
        assert f"\n{tag}: $" in out
    if name == "sweep_nested":
        # The full model is stored, exported and read back unchanged.
        assert "'results': 6}" in out
        assert (export_folder / "ro_nested_sweep" / "checksums.sha256").is_file()
        for tag in STUDY_TAGS[name]:
            [line] = [x for x in out.splitlines() if x.startswith(f"{tag}: all ")]
            assert int(line.split()[2]) > 400  # every variable, not a selection


@pytest.mark.needs_highs
def test_ro_study_on_disk():
    pytest.importorskip("flexparameterize")
    out = "\n" + script(EXAMPLES / "ro_study" / "store_on_disk.py")
    assert "Read back from" in out
    for tag in STUDY_TAGS["sweep_surrogates"]:
        assert f"\n{tag}: $" in out
    shutil.rmtree(EXAMPLES / "ro_study" / "on_disk")


def test_design_study():
    pytest.importorskip("flexdesign")
    out = script(EXAMPLES / "design_study" / "example.py")
    assert "{'succeeded': 3}" in out
    assert "solar_600: {'battery_energy'" in out
    assert "July 2030 battery charge starts 2030-07-01" in out
    # Each horizon read its own month of its year's projection.
    assert (
        "horizon[2035,7] read projection_2035 2035-07-01 00:00:00 -> "
        "2035-07-02 00:00:00" in out
    )
    shutil.rmtree(EXAMPLES / "design_study" / "on_disk")


def test_design_study_scenarios():
    pytest.importorskip("flexdesign")
    out = script(EXAMPLES / "design_study" / "sweep_scenarios.py")
    assert "{'succeeded': 3}" in out
    for scenario in ("growth", "noisy_1", "noisy_2"):
        assert f"{scenario}: {{'battery_energy'" in out
    # Each scenario read its own datasets.
    assert "noisy_2 read noisy_2_2030, noisy_2_2035" in out
    # The growth model, rebuilt from its result, solves to the same size.
    assert "Rebuilt growth: ok" in out
    [stored] = [x for x in out.splitlines() if x.startswith("growth: ")]
    [rebuilt] = [x for x in out.splitlines() if x.startswith("growth battery: ")]
    assert f"'battery_energy': '{rebuilt.split()[2]} kWh'" in stored
    shutil.rmtree(EXAMPLES / "design_study" / "on_disk_scenarios")


POPULATE = """
import json, sys
from flexorchestrator.export import ExportedSeries, ModelData
from flexorchestrator.ext import create_orchestrator

def cost(value):
    return ModelData([ExportedSeries(
        key="costing.aggregate_operating_cost", kind="scalar",
        component="costing.aggregate_operating_cost", index=[], values=[value],
        timestamps=None, unit_pyomo="USD", unit_qudt=None)])

with create_orchestrator(data_dir=sys.argv[1]) as orch:
    if "cost_table" not in orch.commands:
        print(json.dumps({"loaded": False}))
        sys.exit(0)
    orch.create_study("plugin_demo")
    orch.store_model_data("a", cost(1.25))
    orch.store_model_data("b", cost(2.5))
    print(json.dumps({
        "loaded": True,
        "class": type(orch).__name__,
        "stamp": orch.result("a").metadata.get("ro_tools_version"),
        "table": orch.run_command("cost_table"),
    }))
"""


def test_the_sample_plugin(tmp_path):
    pytest.importorskip("acquirium")
    source = tmp_path / "ro_tools"
    shutil.copytree(EXAMPLES / "plugins" / "ro_tools", source)
    site = tmp_path / "site"
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            "--target",
            str(site),
            str(source),
            "-q",
        ]
    )
    base = {k: v for k, v in os.environ.items() if k != "FLEXORCH_PLUGINS"}
    base["PYTHONPATH"] = os.pathsep.join(
        [str(site), *filter(None, [os.environ.get("PYTHONPATH")])]
    )
    enabled = {**base, "FLEXORCH_PLUGINS": "ro_tools"}
    data = str(tmp_path / "store")
    populate = [sys.executable, "-c", POPULATE, data]
    with shared_acquirium(tmp_path / "store"):
        # Installed but not in the allowlist: not loaded.
        assert json.loads(run(populate, env=base)) == {"loaded": False}

        found = json.loads(run(populate, env=enabled))
        assert found == {
            "loaded": True,
            "class": "ROOrchestrator",
            "stamp": "0.1.0",
            "table": {"a": 1.25, "b": 2.5},
        }
        cli = [sys.executable, "-m", "flexorchestrator.cli", "--data-dir", data]
        from_cli = run(
            [*cli, "cmd", "cost_table", "--study", "plugin_demo"], env=enabled
        )
        assert json.loads(from_cli) == found["table"]
        exported = json.loads(
            run([*cli, "export", "plugin_demo", str(tmp_path / "export")], env=enabled)
        )
        assert exported["counts"]["results"] == 2
        imported = json.loads(
            run(
                [*cli, "import", str(tmp_path / "export"), "--name", "copy"],
                env=enabled,
            )
        )
        assert imported["counts"]["results"] == 2

    # The plugin's own tests, from the installed copy.
    run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(source / "tests"),
        ],
        env=base,
        cwd=Path(__file__).parents[3],
    )
