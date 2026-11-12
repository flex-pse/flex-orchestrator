import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium", app_title="Studies and sweeps")


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    # Studies and sweeps

    A **study config** describes a whole set of solves as data: the model
    spec they start from, what changes between them (the **sweeps**), what
    each result stores, and how each is named. `orch.execute_study(config)`
    expands it into tasks, then builds, solves and stores each one.

    The plant here is the RO plant of the earlier tutorials: feed, RO, tank
    and product, delivering a constant 90 m³/hr on a time-of-use tariff, over
    8 to 10 July 2025. This tutorial:

    1. runs a first config, a sweep of the RO's energy intensity;
    2. lists the sweep types;
    3. plans a study with a dry run, to see its tasks before solving any,
       and compares `combine: product` with `zip`;
    4. runs a nested sweep, surrogates fitted from data at three feed limits;
    5. runs hand-picked **cases**;
    6. looks at what each result records, and what happens on a re-run;
    7. exports the study to a folder of JSON files and reads a result back.
    """)
    return


@app.cell
def _():
    import tempfile
    from pathlib import Path

    import altair as alt
    import marimo as mo
    import pandas as pd
    import yaml
    from tutorial_data import (
        ASSETS,
        RO_UNITS,
        energy_price,
        naive,
        operating_data,
        ro_spec,
    )

    from flexorchestrator import Orchestrator
    from flexorchestrator.archive import load_result
    from flexorchestrator.backend import FileBackend

    return (
        ASSETS,
        FileBackend,
        Orchestrator,
        Path,
        RO_UNITS,
        alt,
        energy_price,
        load_result,
        mo,
        naive,
        operating_data,
        pd,
        ro_spec,
        tempfile,
        yaml,
    )


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## Setting up

    The orchestrator stores in a folder here (`FileBackend`). A study holds
    the plant's model spec, `base`, and a month of the RO's operating data,
    which section 4 fits surrogates to.
    """)
    return


@app.cell
def _(ASSETS, FileBackend, Orchestrator, RO_UNITS, operating_data, ro_spec, tempfile):
    orch = Orchestrator(
        FileBackend(tempfile.mkdtemp(prefix="tutorial-")), frame="pandas"
    )
    orch.create_study("sweeps")
    orch.store_data("ro_operation", operating_data(), units=RO_UNITS)
    orch.store_model_spec("base", ro_spec(), base_dir=ASSETS)
    # What every result here stores: the RO's schedule, the tank and the cost.
    KEEP = [
        "plant.ro.permeate",
        "plant.ro.power_electrical",
        "plant.tank.volume",
        "costing.aggregate_operating_cost",
    ]
    return KEEP, orch


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 1. A study config

    A config is a dict, or a YAML or JSON file. This one sweeps a model
    **variable**: the RO's specific energy, which rises as membranes age.
    Each value is fixed on the built model before it's solved.

    - `base_model_spec`: the stored spec every task builds from;
    - `sweeps`: what changes between tasks;
    - `export`: what each result stores (here a few variables; without it,
      the whole model);
    - `output_name`: each task's tag, from the sweep values (`{intensity}` is
      the sweep's `name`).
    """)
    return


@app.cell
def _(KEEP, mo, yaml):
    intensity_yaml = """
    base_model_spec: base
    sweeps:
      - type: variable
        name: intensity
        path: plant.ro.energy_intensity
        values:
          - {value: 2.5, units: kWh/m^3}
          - {value: 3.0, units: kWh/m^3}
          - {value: 3.5, units: kWh/m^3}
    output_name: "intensity_{intensity}"
    """
    intensity_config = {**yaml.safe_load(intensity_yaml), "export": {"include": KEEP}}
    mo.md(f"```yaml{intensity_yaml.rstrip()}\n    export:\n      include: {KEEP}\n```")
    return (intensity_config,)


@app.cell
def _(intensity_config, mo, orch, pd):
    intensity_run = orch.execute_study(intensity_config)

    def costs(run):
        """Each task of ``run``: its tag, the values it swept, and its cost."""
        return pd.DataFrame(
            [
                {
                    "tag": task.tag,
                    **{name: str(value) for name, value in task.labels.items()},
                    "operating cost (USD)": round(
                        orch.result(task.tag).scalars()[
                            "costing.aggregate_operating_cost"
                        ],
                        2,
                    ),
                }
                for task in run.tasks
            ]
        )

    mo.vstack(
        [
            mo.md(f"**{intensity_run.counts()}**"),
            mo.ui.table(costs(intensity_run), selection=None),
        ]
    )
    return costs, intensity_run


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 2. The sweep types

    | `type` | Each task | Set by |
    | --- | --- | --- |
    | `variable` | fixes a variable of the built model | `path`, `values` |
    | `configuration_option` | builds a spec with an option changed | `path`, `values` |
    | `surrogate` | swaps a stored surrogate into a unit | `unit`, `surrogates` |
    | `spec_list` | builds from another stored spec | `specs` |
    | `model_inputs` | sets variables from stored data | `values`: labelled sets |
    | `cases` | runs one named, hand-picked combination of the above | `cases` |

    A config can list several sweeps. `combine: product` (the default) runs
    every combination; `combine: zip` steps them together, the first value
    of each, then the second, so they must have the same length. A config's
    `fits` fit surrogates from stored data before any task runs, so a
    `surrogate` sweep can use them.
    """)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 3. Plan before running: a dry run

    `execute_study(config, dry_run=True)` expands the config into its tasks
    and returns them without fitting, storing or solving anything. Here two
    surrogates, fitted from the operating data, meet a sweep of the feed's
    `max_withdrawal`. Pick the feed limits and how the sweeps combine; the
    plan updates at once.
    """)
    return


@app.cell
def _(mo):
    combine_choice = mo.ui.radio(
        ["product", "zip"], value="product", label="combine", inline=True
    )
    withdrawal_choice = mo.ui.multiselect(
        ["160", "180", "200", "220", "240"],
        value=["180", "200", "220"],
        label="max_withdrawal (m³/hr)",
    )
    mo.hstack([combine_choice, withdrawal_choice], justify="start", gap=2)
    return combine_choice, withdrawal_choice


@app.cell
def _(KEEP):
    # Fits two surrogates of the RO's power from its stored operating data:
    # a constant energy intensity, and power linear in permeate flow.
    FITS = [
        {
            "tag": "{name}",
            "X": {"dataset": "ro_operation", "columns": ["permeate"]},
            "y": {"dataset": "ro_operation", "columns": ["power_electrical"]},
            "variants": [
                {"name": "fixed_intensity", "regressor": "constant_intensity"},
                {"name": "linear_surrogate", "regressor": "multilinear"},
            ],
        }
    ]

    def nested_config(withdrawals, combine):
        """Both surrogates, swept with ``withdrawals``, combined by ``combine``."""
        return {
            "base_model_spec": "base",
            "fits": FITS,
            "sweeps": [
                {
                    "type": "surrogate",
                    "unit": "plant.ro",
                    "surrogates": ["fixed_intensity", "linear_surrogate"],
                },
                {
                    "type": "configuration_option",
                    "name": "withdrawal",
                    # The option's number; its units stay as the spec has them.
                    "path": "plant.feed.max_withdrawal.value",
                    "values": withdrawals,
                },
            ],
            "combine": combine,
            "export": {"include": KEEP},
            "output_name": "{surrogate}_{withdrawal}",
        }

    return (nested_config,)


@app.cell
def _(combine_choice, mo, nested_config, orch, pd, withdrawal_choice, yaml):
    _withdrawals = sorted(int(v) for v in withdrawal_choice.value)
    _config = nested_config(_withdrawals, combine_choice.value)
    _shown = mo.md(
        "```yaml\n"
        + yaml.safe_dump(
            {k: _config[k] for k in ("sweeps", "combine", "output_name")},
            sort_keys=False,
            default_flow_style=None,
        )
        + "```"
    )
    try:
        _plan = orch.execute_study(_config, dry_run=True)
    except Exception as _error:  # a StudyConfigError: show it, as the study would
        _planned = mo.callout(
            mo.md(f"**{type(_error).__name__}**: {_error}"), kind="danger"
        )
    else:
        _planned = mo.vstack(
            [
                mo.md(
                    f"**{len(_plan.tasks)} tasks**, after fitting "
                    f"{', '.join(_plan.planned_fits)}:"
                ),
                mo.ui.table(
                    pd.DataFrame(
                        [
                            {
                                "tag": task.tag,
                                "surrogate": ", ".join(
                                    f"{s.unit} <- {s.surrogate}"
                                    for s in task.surrogates
                                ),
                                "configuration": ", ".join(
                                    f"{path} = {value}"
                                    for path, value in task.configuration.items()
                                ),
                            }
                            for task in _plan.tasks
                        ]
                    ),
                    selection=None,
                ),
            ]
        )
    mo.hstack([_shown, _planned], widths=[1, 2], gap=2)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    With `zip`, the two sweeps must have the same length: pick two feed
    limits and the plan pairs `fixed_intensity` with the first and
    `linear_surrogate` with the second.

    ## 4. Running the nested sweep

    Now the `product` of both surrogates with 180, 200 and 220 m³/hr: the
    study fits the surrogates, stores them, then runs six tasks. A
    configuration option changes how the model is built, so each task stores
    its own spec, derived from `base`.

    Why not 160? Half the feed becomes permeate, so 160 m³/hr makes at most
    80 m³/hr, short of the 90 the plant delivers. The tank is cyclic (it must
    end as full as it started), so it can't make up the difference: the
    solve is infeasible, and the task is stored as `failed`, with only its
    outcome. The dry run above can plan it; nothing checks feasibility
    before solving.
    """)
    return


@app.cell
def _(mo, nested_config, orch, pd):
    nested_run = orch.execute_study(nested_config([180, 200, 220], "product"))
    _fits = pd.DataFrame(
        [
            {
                "surrogate": fit.tag,
                "coefficients": ", ".join(
                    f"{name} = {value:.3f}" for name, value in fit.coefficients.items()
                ),
                "r²": round(fit.metrics["r2"], 3),
            }
            for fit in nested_run.fits[0].fits
        ]
    )
    mo.vstack(
        [
            mo.md(f"**{nested_run.counts()}**. The fits:"),
            mo.ui.table(_fits, selection=None),
        ]
    )
    return (nested_run,)


@app.cell
def _(alt, costs, nested_run):
    nested_costs = costs(nested_run)
    nested_costs["withdrawal"] = nested_costs["withdrawal"].astype(int)
    alt.Chart(nested_costs).mark_line(point=alt.OverlayMarkDef(size=70)).encode(
        x=alt.X(
            "withdrawal:Q",
            title="Feed max_withdrawal (m³/hr)",
            scale=alt.Scale(domain=[175, 225]),
            axis=alt.Axis(values=[180, 200, 220]),
        ),
        y=alt.Y(
            "operating cost (USD):Q",
            title="Operating cost, 8 to 10 July (USD)",
            scale=alt.Scale(zero=False),
        ),
        color=alt.Color("surrogate:N", title="RO surrogate"),
        tooltip=["tag", "operating cost (USD)"],
    ).properties(
        width=480, height=260, title="Each task's cost: a looser feed limit costs less"
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    The tank lets the RO run less on the afternoon peak (shaded) and the
    tank cover the demand. A looser feed limit lets the RO make more water
    the rest of the day, so it can idle further on the peak: with 220 m³/hr
    it drops to about 16 m³/hr there, with 200 only to about 53. With 180
    it can't idle at all: 180 m³/hr of feed makes exactly the 90 m³/hr the
    plant delivers, so the RO runs flat out all day. Below, every task's
    schedule side by side: one column per surrogate, one colour per feed
    limit. Untick tasks to compare fewer. The two surrogates pick the same
    schedule at each limit: what they change is the cost they count for it
    (the chart above).
    """)
    return


@app.cell
def _(mo, nested_run):
    task_choice = mo.ui.multiselect(
        [task.tag for task in nested_run.tasks],
        value=[task.tag for task in nested_run.tasks],
        label="Tasks to compare",
    )
    task_choice
    return (task_choice,)


@app.cell
def _(alt, energy_price, naive, orch, pd, task_choice):
    # One long table: every chosen task's permeate and tank volume over time.
    _rows = []
    for _tag in task_choice.value:
        _result = orch.result(_tag)
        _frame = _result.to_frame("pandas")
        _surrogate, _limit = _tag.rsplit("_", 1)
        for _path, _name in (
            ("plant.ro.permeate", "RO permeate (m³/hr)"),
            ("plant.tank.volume", "Tank volume (m³)"),
        ):
            _rows.append(
                pd.DataFrame(
                    {
                        "time": naive(_frame.index),
                        "value": _frame[_path].to_numpy(),
                        "series": _name,
                        "surrogate": _surrogate,
                        "feed limit": f"{_limit} m³/hr",
                        "task": _tag,
                    }
                )
            )
    if _rows:
        _schedules = pd.concat(_rows)
        _time = pd.DatetimeIndex(_schedules["time"].unique())
        _peak = pd.DataFrame({"start": _time[energy_price(_time).to_numpy() > 0.1]})
        _peak["end"] = _peak["start"] + pd.Timedelta(hours=1)
        _x = alt.X("time:T", title=None, axis=alt.Axis(format="%a %H:%M", tickCount=6))
        _shade = (
            alt.Chart(_peak)
            .mark_rect(color="#f58518", opacity=0.18)
            .encode(x="start:T", x2="end:T")
        )
        _lines = (
            alt.Chart()
            .mark_line(interpolate="step-after", strokeWidth=2)
            .encode(
                x=_x,
                y=alt.Y("value:Q", title=None),
                color=alt.Color(
                    "feed limit:N",
                    title="Feed limit",
                    scale=alt.Scale(range=["#4c78a8", "#e45756", "#54a24b"]),
                ),
                tooltip=["task", "time:T", alt.Tooltip("value:Q", format=".1f")],
            )
        )
        _panels = []
        for _name in ("RO permeate (m³/hr)", "Tank volume (m³)"):
            _panels.append(
                alt.layer(_shade, _lines, data=_schedules[_schedules.series == _name])
                .properties(width=300, height=150)
                .facet(column=alt.Column("surrogate:N", title=None))
                .properties(title=_name)
            )
        _output = alt.vconcat(*_panels).properties(
            title="The schedules each task found (shaded: peak price)"
        )
    else:
        _output = None
    _output
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 5. Cases: hand-picked combinations

    A product grows fast. When only a few combinations matter, a `cases`
    sweep names each one and sets any of `surrogates`, `configuration_options`,
    `variables`, `model_inputs` or `spec`. The surrogates fitted in section 4
    are stored, so these cases use them without fitting again.
    """)
    return


@app.cell
def _(KEEP, mo, yaml):
    cases_yaml = """
    base_model_spec: base
    sweeps:
      - type: cases
        cases:
          - name: linear_tight_feed
            surrogates: {plant.ro: linear_surrogate}
            configuration_options: {plant.feed.max_withdrawal.value: 180}
          - name: fixed_loose_feed
            surrogates: {plant.ro: fixed_intensity}
            configuration_options: {plant.feed.max_withdrawal.value: 220}
          - name: aged_membranes
            variables:
              plant.ro.energy_intensity: {value: 3.5, units: kWh/m^3}
    output_name: "{case}"
    """
    cases_config = {**yaml.safe_load(cases_yaml), "export": {"include": KEEP}}
    mo.md(f"```yaml{cases_yaml.rstrip()}\n```")
    return (cases_config,)


@app.cell
def _(alt, cases_config, costs, mo, nested_run, orch):
    cases_run = orch.execute_study(cases_config)
    _costs = costs(cases_run)
    _chart = (
        alt.Chart(_costs)
        .mark_bar()
        .encode(
            x=alt.X("tag:N", title=None, sort=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("operating cost (USD):Q", title="Operating cost (USD)"),
            color=alt.Color("tag:N", legend=None),
            tooltip=["tag", "operating cost (USD)"],
        )
        .properties(width=420, height=240, title="Each case's cost")
    )
    mo.vstack([mo.md(f"**{cases_run.counts()}**"), _chart])
    return (cases_run,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    `linear_tight_feed` is the same combination as section 4's
    `linear_surrogate_180`, so it costs the same. `aged_membranes` builds
    from `base`, whose RO has a fixed 3.0 kWh/m³ intensity, and fixes it at
    3.5: the same as section 1's `intensity_3.5`.

    ## 6. What a result records

    Every result knows what made it: the spec it was built from (with its
    hash), the values a sweep fixed (`overrides`), and the versions and hooks
    that ran it (`metadata`). A configuration option or surrogate is in the
    spec itself, a derived one whose `parent_tag` is `base`.
    """)
    return


@app.cell
def _(mo, orch, pd):
    _rows = []
    for _tag in ("intensity_3.5", "linear_surrogate_180", "aged_membranes"):
        _result = orch.result(_tag)
        _spec = _result.spec()
        _rows.append(
            {
                "result": _tag,
                "spec": _result.spec_tag,
                "spec's parent": _spec.parent_tag or "",
                "overrides": ", ".join(
                    f"{path} = {value['value']} {value['units']}"
                    for path, value in _result.overrides.items()
                ),
                "flex-orchestrator": _result.metadata["flexorch_version"],
                "spec hash": _result.metadata["spec_hash"][:12],
            }
        )
    mo.ui.table(pd.DataFrame(_rows), selection=None)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    `orch.stored_model_results()` lists every result in the study:
    """)
    return


@app.cell
def _(cases_run, mo, orch, pd):
    _ = cases_run  # listed once every section has run
    all_results = pd.DataFrame(
        [
            {
                "tag": result.tag,
                "spec": result.spec_tag,
                "operating cost (USD)": round(
                    result.scalars()["costing.aggregate_operating_cost"], 2
                ),
                "stored at": result.started_at,
            }
            for result in orch.stored_model_results()
        ]
    )
    mo.ui.table(all_results, selection=None)
    return (all_results,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    **Running a config again.** Each task's tag is taken. With the default
    `on_existing: error`, the study checks every tag before it solves any, and
    refuses. `skip` keeps the stored results and runs only the missing tasks,
    so an interrupted study can simply be run again; `replace` solves them
    all again and replaces the results.
    """)
    return


@app.cell
def _(intensity_config, intensity_run, mo, orch):
    _ = intensity_run  # after the first run
    try:
        orch.execute_study(intensity_config)
    except Exception as _error:
        _refused = f"**{type(_error).__name__}**: {_error}"
    _skipped = orch.execute_study({**intensity_config, "on_existing": "skip"})
    mo.vstack(
        [
            mo.callout(mo.md(f"`on_existing: error`: {_refused}"), kind="warn"),
            mo.md(f"`on_existing: skip`: **{_skipped.counts()}**, nothing solved."),
        ]
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 7. Export and share

    `orch.export_study(folder)` writes the whole study, its specs (with their
    files), data, fits and results, to a folder of JSON files. Anyone can read
    a result back from it with `load_result`, with no orchestrator and no
    database, or import the whole study into another store with
    `orch.import_study(folder)`.
    """)
    return


@app.cell
def _(Path, all_results, load_result, mo, orch, tempfile):
    _ = all_results  # export once every result is stored
    folder = Path(tempfile.mkdtemp(prefix="tutorial-export-")) / "sweeps"
    exported = orch.export_study(folder)
    _loaded = load_result(folder / "results" / "linear_surrogate_180.json")
    _stored = orch.result("linear_surrogate_180").scalars()
    _key = "costing.aggregate_operating_cost"
    _files = "\n".join(sorted(exported.files)[:8] + ["..."])
    mo.vstack(
        [
            mo.md(f"Exported **{exported.counts}** to a folder:"),
            mo.md(f"```\n{_files}\n```"),
            mo.md(
                f"Read back from `results/linear_surrogate_180.json`: "
                f"{_loaded.scalars()[_key]:,.2f} USD, as stored "
                f"({_stored[_key]:,.2f} USD)."
            ),
        ]
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ### Running tasks in parallel

    Every task here ran one after another, in this process. With
    `execution: {max_workers: 4}` in the config (or
    `execute_study(config, max_workers=4)`), the tasks run on Ray, four at a
    time; the results are stored the same way. The docs' **Running in
    parallel** page covers it.

    ## Where next

    - **Model inputs** sets variables from stored data, and a `model_inputs`
      sweep varies them (tutorial 2).
    - **Design studies** size equipment over many years (tutorial 5).
    """)
    return


if __name__ == "__main__":
    app.run()
