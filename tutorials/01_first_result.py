import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium", app_title="A first result")


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    # A first result

    flex-orchestrator keeps everything a process-model study needs and makes
    in one store: the **model specs** it builds from, the **data** it reads,
    and every **result** it solves, each recorded with what it came from.

    This tutorial takes one model from start to finish:

    1. meets the plant and its electricity tariff;
    2. stores a month of the plant's operating data;
    3. stores the model as a spec, plain JSON;
    4. builds it, solves it and stores the result;
    5. reads the result back and charts the schedule the model chose;
    6. looks at what the result records about where it came from;
    7. stores a variant of the spec, with a bigger or smaller tank, and
       compares the two;
    8. takes results out of the store: a CSV, one result as JSON, and the
       whole study as a folder another store can import, then opens its
       files: results, model specs, assets and data.

    Move the slider in section 7 and that section re-runs.
    """)
    return


@app.cell
def _():
    import tempfile

    import altair as alt
    import marimo as mo
    import pandas as pd
    from tutorial_data import (
        ASSETS,
        RO_UNITS,
        energy_price,
        naive,
        operating_data,
        ro_spec,
        sec,
    )

    from flexorchestrator import Orchestrator
    from flexorchestrator.backend import FileBackend

    return (
        ASSETS,
        FileBackend,
        Orchestrator,
        RO_UNITS,
        alt,
        energy_price,
        mo,
        naive,
        operating_data,
        pd,
        ro_spec,
        sec,
        tempfile,
    )


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 1. The plant

    A reverse osmosis (RO) plant draws feed water, sends half of it through
    the RO as permeate (`recovery: 0.5`) and the rest to brine, and stores
    the permeate in a tank. The tank must deliver a constant **90 m³/hr** of
    product. The RO's power is its permeate flow times its energy intensity,
    3 kWh/m³.

    Electricity comes on a summer time-of-use tariff: energy costs twice as
    much on weekday afternoons, 16:00 to 21:00, and the month's peak power is
    charged too. The tank lets the RO make water off-peak and stop when
    energy is dear. The model covers 8 to 10 July 2025, in hourly steps.

    The tank is **cyclic** (`cyclic=True`): it starts half full, at 750 m³,
    and must end the two days at least as full. Without that, the cheapest
    schedule would run the tank down and count the water it started with as
    free; with it, every cubic metre delivered is one the RO made.
    """)
    return


@app.cell
def _(alt, energy_price, pd):
    hours = pd.date_range("2025-07-08", "2025-07-10", freq="h", inclusive="left")
    prices = energy_price(hours)
    _frame = pd.DataFrame({"time": hours, "price": prices.to_numpy()})
    alt.Chart(_frame).mark_line(interpolate="step-after", color="#4c78a8").encode(
        x=alt.X("time:T", title=None, axis=alt.Axis(format="%a %d %b %H:%M")),
        y=alt.Y(
            "price:Q", title="Energy price ($/kWh)", scale=alt.Scale(domain=[0, 0.2])
        ),
        tooltip=[alt.Tooltip("time:T", format="%a %H:%M"), "price"],
    ).properties(width=600, height=160, title="The tariff's energy price, 8 to 10 July")
    return (hours,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 2. A store, a study and some data

    An `Orchestrator` works on a storage backend. Here it's a folder
    (`FileBackend`), so this runs anywhere; with Acquirium, only that line
    changes. Everything is kept in a **study**, a named group of specs, data
    and results.

    The plant's operating data is a month of hourly readings: the RO's
    permeate flow and the power it drew. `store_data` keeps it as a dataset,
    with its units.
    """)
    return


@app.cell
def _(FileBackend, Orchestrator, RO_UNITS, operating_data, tempfile):
    orch = Orchestrator(
        FileBackend(tempfile.mkdtemp(prefix="tutorial-")), frame="pandas"
    )
    orch.create_study("first_result")
    operation = operating_data()
    orch.store_data("ro_operation", operation, units=RO_UNITS)
    return (orch,)


@app.cell
def _(alt, naive, orch, pd, sec):
    _data = orch.get_data("ro_operation")  # read back: a UTC-indexed frame
    _frame = pd.DataFrame(
        {
            "time": naive(_data.index),
            "permeate": _data["permeate"],
            "power": _data["power_electrical"],
        }
    )
    _curve = pd.DataFrame({"permeate": range(30, 151, 5)})
    _curve["power"] = sec(_curve["permeate"]) * _curve["permeate"]
    _scatter = (
        alt.Chart(_frame.assign(series="readings"))
        .mark_circle(size=14, opacity=0.5)
        .encode(
            x=alt.X("permeate:Q", title="Permeate (m³/hr)"),
            y=alt.Y("power:Q", title="RO power (kW)"),
            color=alt.Color(
                "series:N",
                title=None,
                scale=alt.Scale(
                    domain=["readings", "the true curve"], range=["#4c78a8", "#e45756"]
                ),
            ),
        )
    )
    _line = (
        alt.Chart(_curve.assign(series="the true curve"))
        .mark_line(strokeWidth=2.5)
        .encode(x="permeate:Q", y="power:Q", color="series:N")
    )
    _week = (
        alt.Chart(_frame[_frame.time < "2025-06-04"])
        .mark_line(color="#4c78a8")
        .encode(
            x=alt.X("time:T", title=None, axis=alt.Axis(format="%d %b %H:%M")),
            y=alt.Y("permeate:Q", title="Permeate (m³/hr)"),
        )
        .properties(width=330, height=220, title="The first three days")
    )
    alt.hconcat(
        (_scatter + _line).properties(
            width=280, height=220, title="Power against flow, June"
        ),
        _week,
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    The readings follow a curve: the RO is most efficient near 90 m³/hr. The
    model here ignores that and takes a fixed 3 kWh/m³; the
    [surrogates tutorial](03_surrogates.py) fits the data instead.

    ## 3. The model as a spec

    The model is built in Python with flex-pse (`ro_model` in
    `tutorial_data.py`), then **emitted** as a spec: JSON that says which
    elements to build and how. The orchestrator stores the spec, not the
    model, so it can rebuild the same model anywhere, in another process or
    on another machine. The tariff file is stored with it.
    """)
    return


@app.cell
def _(ASSETS, mo, orch, pd, ro_spec):
    record = orch.store_model_spec("base", ro_spec(), base_dir=ASSETS)
    _elements = pd.DataFrame(
        [
            {
                "kind": element["kind"],
                "name": element.get("name", ""),
                "unit model": element.get("unit_model_class") or "",
            }
            for element in record.spec["elements"]
        ]
    )
    mo.vstack(
        [
            mo.md(
                f"Stored spec **`{record.tag}`**, hash `{record.spec_hash[:12]}...`, "
                f"with {len(_elements)} elements:"
            ),
            mo.ui.table(_elements, selection=None, show_column_summaries=False),
        ]
    )
    return (record,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 4. Build, solve, store

    `orch.build("base")` builds a Pyomo model from the stored spec.
    `solve_and_store` solves it and stores the result under a tag. A model
    has hundreds of values, so `include` picks the ones to keep: glob
    patterns or exact paths. Without `include`, every value of the model is
    stored: its variables, parameters and expressions.
    """)
    return


@app.cell
def _(mo, orch, pd):
    keep = [
        "plant.ro.permeate",
        "plant.ro.power_electrical",
        "plant.tank.volume",
        "costing.aggregate_operating_cost",
    ]
    model = orch.build("base")
    stored = orch.solve_and_store("base_run", model, include=keep)
    _fields = [
        ("tag", stored.tag),
        ("status", stored.status),
        ("outcome", f"{stored.outcome.status}, {stored.outcome.termination}"),
        ("solver", stored.outcome.solver),
        ("keys", ", ".join(stored.keys)),
        ("run_id", stored.run_id),
    ]
    mo.vstack(
        [
            mo.md("`solve_and_store` returns what it stored, a `StoredResult`:"),
            mo.ui.table(
                pd.DataFrame(_fields, columns=["field", "value"]),
                selection=None,
                show_column_summaries=False,
            ),
        ]
    )
    return keep, stored


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    `status` is `succeeded`: the solve was optimal and its values are stored.
    A solve that fails is stored too, as `failed`, with only its outcome, so
    a study keeps a record of it.

    ### Choosing the solver

    By default flex-pse picks the solver: it classifies the model (LP, MILP,
    NLP, ...) and takes the first installed solver that can solve that
    class, trying `gurobi`, `scip`, `highs`, `cbc`, then `ipopt`. This plant
    is an LP, so it went to `highs`. To ask for one, name it:

    | Where | How | Applies to |
    | --- | --- | --- |
    | the spec | `"solver": "highs"` in the spec JSON | models built from it |
    | one solve | `orch.solve_and_store(..., prefer="cbc")` | that solve |
    | a study config | `solve_options: {prefer: cbc}` | every task |

    `prefer` on a solve or in `solve_options` wins over the spec's `solver`.
    The same place takes the solver's options: `time_limit` (seconds),
    `mip_gap` (for MILPs), `tee` (print the solver's log), `warmstart`, and
    `options`, a dict passed straight to the solver
    (`options: {threads: 4}`).

    `prefer` is a preference, not a demand: a solver that isn't installed,
    or can't solve the model's class, is skipped with a warning, and the
    default order picks instead. So check which one ran:
    `stored.outcome.solver`, also recorded with every result. Pick one below
    to see; this solves a fresh build with `flexorchestrator.flexpse.solve`,
    the same solve `solve_and_store` calls, and stores nothing.
    """)
    return


@app.cell
def _(mo):
    solver_choice = mo.ui.dropdown(
        ["automatic", "highs", "cbc", "scip", "ipopt", "gurobi"],
        value="cbc",
        label="prefer",
    )
    time_limit = mo.ui.number(start=1, stop=600, value=60, label="time_limit (s)")
    mo.hstack([solver_choice, time_limit], justify="start", gap=2)
    return solver_choice, time_limit


@app.cell
def _(mo, orch, pd, solver_choice, time_limit):
    import logging

    import pyomo.environ as pyo

    from flexorchestrator.flexpse import solve

    _prefer = None if solver_choice.value == "automatic" else solver_choice.value

    class _Warnings(logging.Handler):
        """Keeps the solver facade's warnings, to show them here."""

        def __init__(self):
            super().__init__(logging.WARNING)
            self.messages = []

        def emit(self, record):
            self.messages.append(record.getMessage())

    _caught = _Warnings()
    _log = logging.getLogger("flexcore.solvers.facade")
    _log.addHandler(_caught)
    try:
        _model = orch.build("base")
        _outcome = solve(_model, prefer=_prefer, time_limit=time_limit.value)
    finally:
        _log.removeHandler(_caught)
    _rows = [
        ("asked for", solver_choice.value),
        ("solved by", _outcome.solver),
        ("termination", _outcome.termination),
        ("solve time", f"{_outcome.wall_time:.2f} s"),
        ("cost", f"{pyo.value(_model.costing.aggregate_operating_cost):,.2f} USD"),
    ]
    _parts = [
        mo.ui.table(
            pd.DataFrame(_rows, columns=["", "value"]),
            selection=None,
            show_column_summaries=False,
        )
    ]
    if _caught.messages:
        _parts.append(mo.callout(mo.md("\n\n".join(_caught.messages)), kind="warn"))
    mo.vstack(_parts)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 5. Reading the result back

    `orch.result(tag)` reads a stored result. Its `scalars()` are the single
    numbers, and `to_frame()` the time series, one column per path.
    """)
    return


@app.cell
def _(mo, orch, stored):
    result = orch.result(stored.tag)
    schedule = result.to_frame()
    cost = result.scalars()["costing.aggregate_operating_cost"]
    mo.vstack(
        [
            mo.md(
                f"`result.scalars()`: the two days cost **${cost:,.2f}** to operate. "
                "`result.to_frame()`:"
            ),
            mo.ui.table(schedule.round(1).reset_index(), selection=None, page_size=6),
            mo.md(
                "Every result in the study: "
                + ", ".join(f"`{r.tag}`" for r in orch.stored_model_results())
            ),
        ]
    )
    return result, schedule


@app.cell
def _(alt, energy_price, hours, naive, pd, schedule):
    def peak_bands(index):
        """One band per peak-price hour, for shading a chart."""
        _peak = index[energy_price(index).to_numpy() > 0.1]
        return pd.DataFrame(
            {
                "start": _peak,
                "end": _peak + pd.Timedelta(hours=1),
                "price": "peak (0.18 $/kWh)",
            }
        )

    def schedule_chart(frame, title):
        """The RO's permeate and the tank's volume, over the peak hours."""
        _time = naive(frame.index)
        _flows = pd.DataFrame(
            {"time": _time, "permeate": frame["plant.ro.permeate"].to_numpy()}
        )
        _tank = pd.DataFrame(
            {"time": _time, "volume": frame["plant.tank.volume"].to_numpy()}
        )
        _x = alt.X("time:T", title=None, axis=alt.Axis(format="%a %H:%M"))
        _bands = (
            alt.Chart(peak_bands(hours))
            .mark_rect(opacity=0.18)
            .encode(
                x="start:T",
                x2="end:T",
                color=alt.Color(
                    "price:N",
                    title=None,
                    scale=alt.Scale(range=["#f58518"]),
                    legend=alt.Legend(orient="top"),
                ),
            )
        )
        _permeate = (
            alt.Chart(_flows)
            .mark_line(interpolate="step-after", color="#4c78a8", strokeWidth=2)
            .encode(x=_x, y=alt.Y("permeate:Q", title="RO permeate (m³/hr)"))
        )
        _demand = (
            alt.Chart(pd.DataFrame({"y": [90]}))
            .mark_rule(strokeDash=[4, 3], color="#888")
            .encode(y="y:Q")
        )
        _volume = (
            alt.Chart(_tank)
            .mark_line(color="#54a24b", strokeWidth=2)
            .encode(x=_x, y=alt.Y("volume:Q", title="Tank volume (m³)"))
        )
        return alt.vconcat(
            (_bands + _permeate + _demand).properties(width=600, height=160),
            (_bands + _volume).properties(width=600, height=130),
            title=title,
        )

    schedule_chart(
        schedule,
        "The RO runs off-peak; the tank covers the peak (dashed: 90 m³/hr demand)",
    )
    return (schedule_chart,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    The RO makes about 114 m³/hr while energy is cheap, more than the
    90 m³/hr the plant delivers, so the tank fills by about 24 m³ an hour.
    It stops through the five peak hours, when the tank supplies the 450 m³
    the plant needs, and refills after. It runs at a steady rate rather than
    flat out, because the tariff also charges the month's peak power. The
    tank swings between about 665 and 1,127 m³ and ends at 750 m³, where it
    started, as the cyclic constraint asks. (The 0 at 00:00 on the first day
    is the model's first time step, which flex-pse's tank keeps outside its
    balance: its volume there is the starting 750 m³.)

    ## 6. What the result records

    A result records how it was made: the spec it was built from (tag and
    hash), the solve's outcome, the versions of flex-orchestrator and
    flex-pse, and the hooks that built and solved it. So any result can be
    traced back to its spec, and rebuilt from it.
    """)
    return


@app.cell
def _(mo, pd, record, result):
    _meta = result.metadata
    _rows = [
        ("spec", f"{result.spec_tag} ({_meta['spec_hash'][:12]}...)"),
        ("same spec as stored", str(_meta["spec_hash"] == record.spec_hash)),
        ("solver", f"{result.outcome.solver}: {result.outcome.termination}"),
        ("solve time", f"{result.outcome.wall_time:.1f} s"),
        ("flex-orchestrator", _meta["flexorch_version"]),
        ("flex-pse", _meta["flexpse_version"]),
        ("built by", _meta["flexorch_hooks"]["build"]),
        ("stored", str(result.started_at)),
    ]
    mo.vstack(
        [
            mo.ui.table(
                pd.DataFrame(_rows, columns=["", "value"]),
                selection=None,
                show_column_summaries=False,
            ),
            mo.md(
                f"`result.spec()` returns the spec itself: `{result.spec().tag}`, "
                f"{len(result.spec().spec['elements'])} elements."
            ),
        ]
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 7. A variant of the spec

    A spec is data, so a variant is a new spec: here the same plant with
    another tank size, stored with `parent="base"` so the study records
    where it came from. Solve it and compare. The five peak hours need
    450 m³ from the tank, which starts half full and must end at least that
    full. A small tank can't both carry the plant through the peaks and be
    back at its start by the end. At 600 m³ the RO still stops through the
    first peak, but keeps making about 16 m³/hr through the second, since
    only three hours are left after it to refill the tank; at 300 m³ it
    makes about 40 m³/hr through the peaks. Running at peak prices costs:
    about 1,657 USD at 600 m³ and 1,860 USD at 300 m³, against 1,572 USD at
    1,500 m³. From 900 m³ up the bill stays at 1,572 USD: the tank already
    holds what the peaks need, so more room buys nothing.
    """)
    return


@app.cell
def _(mo):
    tank = mo.ui.slider(300, 3000, step=300, value=600, label="Tank size (m³)")
    tank
    return (tank,)


@app.cell
def _(ASSETS, keep, orch, ro_spec, tank):
    variant = f"tank_{tank.value}"
    orch.store_model_spec(
        variant, ro_spec(tank.value), parent="base", base_dir=ASSETS, replace=True
    )
    variant_run = orch.solve_and_store(
        variant, orch.build(variant), include=keep, replace=True
    )
    return variant, variant_run


@app.cell
def _(alt, mo, orch, pd, result, schedule_chart, tank, variant, variant_run):
    _variant = orch.result(variant_run.tag)
    _costs = pd.DataFrame(
        [
            {
                "spec": "base (1500 m³)",
                "cost": result.scalars()["costing.aggregate_operating_cost"],
            },
            {
                "spec": f"{variant} ({tank.value} m³)",
                "cost": _variant.scalars()["costing.aggregate_operating_cost"],
            },
        ]
    )
    _bars = (
        alt.Chart(_costs)
        .mark_bar()
        .encode(
            x=alt.X("cost:Q", title="Operating cost, 8 to 10 July ($)"),
            y=alt.Y("spec:N", title=None),
            color=alt.Color("spec:N", legend=None),
            tooltip=["spec", alt.Tooltip("cost:Q", format="$,.2f")],
        )
        .properties(width=520, height=70)
    )
    mo.vstack(
        [
            mo.md(
                f"Spec `{variant}` (parent `{orch.model_spec(variant).parent_tag}`): "
                f"**{variant_run.status}**."
            ),
            _bars,
            schedule_chart(
                _variant.to_frame(), f"The schedule with a {tank.value} m³ tank"
            ),
        ]
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 8. Taking results out of the store

    Three ways, from a quick table to a whole study:

    1. **A table file.** `result.to_frame()` is a pandas (or polars) frame,
       so any format pandas writes: CSV here, or Parquet or Excel.
       `result.scalars()` is a dict.
    2. **One result, with its provenance.** `orch.export_result(tag, path)`
       writes it as JSON: its values with units, its outcome, the spec it
       came from and its metadata. `flexorchestrator.archive.load_result`
       reads it back with no store at all.
    3. **A whole study.** `orch.export_study(folder)` writes its specs, data,
       fits and results as a folder of JSON files, with a checksum file.
       `orch.import_study(folder)`, or `flex-orch import <folder>` from the
       command line, re-creates it in another store.
    """)
    return


@app.cell
def _(mo, result, tempfile):
    from pathlib import Path

    exports = Path(tempfile.mkdtemp(prefix="tutorial-export-"))
    _csv = exports / "base_run.csv"
    result.to_frame().to_csv(_csv)
    _scalars = result.scalars()
    mo.md(
        f"Wrote `{_csv.name}`: {len(result.to_frame())} rows, columns "
        + ", ".join(f"`{c}`" for c in result.to_frame().columns)
        + ". `result.scalars()`: "
        + ", ".join(f"`{k}` = {v:,.2f}" for k, v in _scalars.items())
    )
    return (exports,)


@app.cell
def _(exports, mo, orch, result):
    from flexorchestrator.archive import load_result

    _file = orch.export_result(result.tag, exports / f"{result.tag}.json")
    loaded = load_result(_file)
    _same = {s.key: s for s in loaded.data()} == {s.key: s for s in result.data()}
    mo.md(
        f"`orch.export_result` wrote `{_file.name}` "
        f"({_file.stat().st_size / 1024:,.0f} kB). `load_result` reads back "
        f"`{loaded.tag}`, built from spec `{loaded.spec_tag}`, solved by "
        f"{loaded.outcome.solver}; every value is the same as stored: "
        f"**{_same}**."
    )
    return


@app.cell
def _(FileBackend, Orchestrator, exports, mo, orch, result, tempfile):
    _report = orch.export_study(exports / "first_result")
    _shown = [f for f in _report.files if not f.startswith("data/")][:8]
    # Another store, as a colleague's would be: import the folder there.
    _elsewhere = Orchestrator(
        FileBackend(tempfile.mkdtemp(prefix="tutorial-elsewhere-")), frame="pandas"
    )
    _imported = _elsewhere.import_study(exports / "first_result")
    _key = "costing.aggregate_operating_cost"
    _there = _elsewhere.result(result.tag).scalars()[_key]
    mo.md(
        f"`orch.export_study` wrote **{_report.counts}** to "
        f"`{_report.path.name}/`, {len(_report.files)} files, among them:\n\n"
        + "\n".join(f"- `{f}`" for f in _shown)
        + f"\n\n`import_study` into a new store gave **{_imported.counts}**, in "
        f"study `{_imported.study}`, and `{result.tag}` there costs "
        f"{_there:,.2f} USD, as here ({result.scalars()[_key]:,.2f} USD). "
        "[Tutorial 4](04_studies_and_sweeps.py) exports a study of sweeps."
    )
    study_folder = exports / "first_result"
    return (study_folder,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ### What the export holds

    The exported folder is plain files, readable without flex-orchestrator:

    | Folder | Holds |
    | --- | --- |
    | `results/` | a JSON per result: its values, outcome and provenance |
    | `specs/models/` | a JSON per model spec: the flowsheet |
    | `specs/assets/` | files the specs refer to (the tariff), named by hash |
    | `data/` | a JSON per dataset: columns, units, timestamps |
    | `study.json` | what the folder holds |
    | `checksums.sha256` | a hash of every file |

    Pick a file to see it. Long lists of numbers are shortened unless you
    switch that off.
    """)
    return


@app.cell
def _(mo, study_folder):
    _groups = [
        ("result", "results"),
        ("model spec", "specs/models"),
        ("asset", "specs/assets"),
        ("dataset", "data"),
    ]
    _options = {}
    for _label, _folder in _groups:
        for _file in sorted((study_folder / _folder).iterdir()):
            _options[f"{_label}: {_file.name}"] = _file
    for _name in ("study.json", "checksums.sha256"):
        _options[f"study: {_name}"] = study_folder / _name
    file_choice = mo.ui.dropdown(_options, value="result: base_run.json", label="File")
    shorten = mo.ui.switch(value=True, label="Shorten long lists")
    mo.hstack([file_choice, shorten], justify="start", gap=2)
    return file_choice, shorten


@app.cell
def _(file_choice, mo, pd, shorten, study_folder):
    import json

    def shortened(value, keep=4):
        """``value`` with each list longer than ``2 * keep`` cut to its ends."""
        if isinstance(value, dict):
            return {k: shortened(v, keep) for k, v in value.items()}
        if isinstance(value, list):
            if len(value) > 2 * keep:
                hidden = f"... {len(value) - 2 * keep} more ..."
                return [
                    *(shortened(v, keep) for v in value[:keep]),
                    hidden,
                    *(shortened(v, keep) for v in value[-keep:]),
                ]
            return [shortened(v, keep) for v in value]
        return value

    _path = file_choice.value
    _where = _path.relative_to(study_folder).as_posix()
    _size = f"`{_where}`, {_path.stat().st_size:,} bytes"
    if _path.suffix == ".json":
        _content = json.loads(_path.read_text())
        _text = json.dumps(shortened(_content) if shorten.value else _content, indent=2)
        _view = mo.md("```json\n" + _text + "\n```")
    elif _path.suffix == ".csv":
        _view = mo.ui.table(pd.read_csv(_path), selection=None, page_size=12)
    else:
        _view = mo.md("```text\n" + _path.read_text() + "\n```")
    mo.vstack([mo.md(_size), _view])
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## Where next

    - **Acquirium.** To keep everything in Acquirium instead of a folder,
      only the first line changes: `Orchestrator()` connects to the store
      that `FLEXORCH_ACQUIRIUM_URL` names, or `Orchestrator(data_dir=...)`
      starts one locally. The rest of this notebook runs as it is.
    - [2. Model inputs](02_model_inputs.py): set model variables from stored
      data, and see how it's lined up with the model's time steps.
    - [3. Surrogates](03_surrogates.py): fit the RO's energy from the
      operating data, and solve with the fit.
    - [4. Studies and sweeps](04_studies_and_sweeps.py): many solves from one
      config.
    - [5. Design studies](05_design_studies.py): size equipment across years.
    """)
    return


if __name__ == "__main__":
    app.run()
