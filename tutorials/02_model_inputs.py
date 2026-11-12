import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium", app_title="Model inputs")


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    # Model inputs

    A model often needs measured data: a demand, a price, or the schedule a
    plant actually ran. A **model input** sets a model variable from data
    the orchestrator has stored, lined up with the model's time steps, and
    every result records where that data came from.

    The plant here is a feed, a reverse osmosis (RO) unit, a tank and a
    product that must get a steady 90 m³/hr. Electricity costs twice as
    much on summer weekday afternoons, so the tank lets the RO run harder
    off-peak. The model runs 8 to 10 July 2025, hour by hour; the stored
    operating data is June 2025's.

    This tutorial:

    1. shows what a model input is;
    2. looks at the stored data and the model's horizon;
    3. previews which data a model input reads (`offset`, `timezone`);
    4. shows how data is lined up with the time steps (`resample`, `gaps`,
       `reduce`);
    5. costs the schedule the plant actually ran against the one the model
       picks, in a study;
    6. lists the rules worth knowing.
    """)
    return


@app.cell
def _():
    import tempfile

    import altair as alt
    import marimo as mo
    import numpy as np
    import pandas as pd
    import pyomo.environ as pyo
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
    from flexorchestrator.backend import FileBackend
    from flexorchestrator.errors import ModelInputError

    return (
        ASSETS,
        FileBackend,
        ModelInputError,
        Orchestrator,
        RO_UNITS,
        alt,
        energy_price,
        mo,
        naive,
        np,
        operating_data,
        pd,
        pyo,
        ro_spec,
        tempfile,
        yaml,
    )


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 1. What a model input is

    An entry in `model_inputs` has three parts:

    - **A selector**: which stored data to read. Here a `dataset` and its
      `columns`; it can also be a stored `result`, or Acquirium `streams`,
      chosen the same way as a surrogate fit's data.
    - **`rename`**: the model variable each column sets, by its path.
    - **How to line it up** with the model's time steps: which window to
      read (`offset`, `timezone`, `data_years`), how samples become one
      value per step (`resample`), and how missing steps are filled
      (`gaps`, `limit`). Or `reduce`, for one number.

    This entry fixes the RO's permeate flow to what the plant recorded 30
    days before the model's horizon:
    """)
    return


@app.cell
def _(mo, yaml):
    entry_yaml = """
    dataset: ro_operation               # the selector
    columns: [permeate]
    rename: {permeate: plant.ro.permeate}  # the variable it sets
    offset: -30D                        # read 8-10 June for 8-10 July
    """
    as_operated = yaml.safe_load(entry_yaml)
    mo.md(f"```yaml\nmodel_inputs:\n  -{entry_yaml.rstrip()}\n```")
    return (as_operated,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 2. The stored data and the model's horizon

    The orchestrator stores in a folder here (`FileBackend`); with Acquirium,
    only the `Orchestrator(...)` line changes. We store a month of the RO's
    hourly operating data, its permeate flow and power, and the plant's
    model spec, emitted from the flex-pse model.
    """)
    return


@app.cell
def _(
    ASSETS,
    FileBackend,
    Orchestrator,
    RO_UNITS,
    operating_data,
    ro_spec,
    tempfile,
):
    orch = Orchestrator(
        FileBackend(tempfile.mkdtemp(prefix="tutorial-")), frame="pandas"
    )
    orch.create_study("inputs")
    june = operating_data()
    orch.store_data("ro_operation", june, units=RO_UNITS)
    _spec = orch.store_model_spec("base", ro_spec(), base_dir=ASSETS)
    return june, orch


@app.cell
def _(alt, june, naive, pd):
    _frame = pd.DataFrame(
        {
            "time": naive(june.index),
            "permeate (m³/hr)": june["permeate"],
            "power (kW)": june["power_electrical"],
        }
    ).melt("time", var_name="column", value_name="value")
    alt.Chart(_frame).mark_line(strokeWidth=1, color="#4c78a8").encode(
        x=alt.X("time:T", title="June 2025 (UTC)"),
        y=alt.Y("value:Q", title=None),
        row=alt.Row("column:N", title=None),
        tooltip=["time:T", "column", alt.Tooltip("value:Q", format=".1f")],
    ).resolve_scale(y="independent").properties(
        width=620, height=120, title="The stored dataset ro_operation, every hour"
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    The model's own time steps come from its spec: 48 hours from
    8 July 2025 00:00. Data for July doesn't exist yet, so the entry reads
    June's with `offset`.

    ## 3. Which data a model input reads

    `orch.resolve_model_inputs(spec, entries)` resolves entries against a
    stored spec without building or solving: it returns the values that
    would be fixed, and the provenance a result would record. Move the
    offset, or put the plant in another time zone, and see which window is
    read.

    - **`offset`** shifts the window: `-30D` reads 30 days earlier.
    - **`timezone`** is the plant's wall clock. The model's time steps are
      naive local times, and stored data is UTC, so a plant in Los Angeles
      reads 00:00 local as 07:00 UTC.
    """)
    return


@app.cell
def _(mo):
    days_back = mo.ui.slider(1, 30, value=30, label="offset (days back)")
    zone = mo.ui.dropdown(["UTC", "America/Los_Angeles"], value="UTC", label="timezone")
    mo.hstack([days_back, zone], justify="start", gap=2)
    return days_back, zone


@app.cell
def _(as_operated, days_back, orch, zone):
    preview_entry = {
        **as_operated,
        "offset": f"-{days_back.value}D",
        "timezone": zone.value,
    }
    preview = orch.resolve_model_inputs("base", [preview_entry])
    [preview_record] = preview.provenance
    return preview, preview_entry, preview_record


@app.cell
def _(alt, june, mo, naive, pd, preview, preview_entry, preview_record):
    _window = preview_record["window"]
    # The window is in the plant's wall clock; the chart is in UTC.
    _zone = preview_entry["timezone"]
    _start, _end = (
        pd.Timestamp(_window[k]).tz_localize(_zone).tz_convert("UTC").tz_localize(None)
        for k in ("start", "end")
    )
    _data = pd.DataFrame({"time": naive(june.index), "permeate": june["permeate"]})
    _band = (
        alt.Chart(pd.DataFrame({"start": [_start], "end": [_end]}))
        .mark_rect(color="#f58518", opacity=0.3)
        .encode(x="start:T", x2="end:T")
    )
    _line = (
        alt.Chart(_data)
        .mark_line(strokeWidth=1, color="#4c78a8")
        .encode(
            x=alt.X("time:T", title="June 2025 (UTC)"),
            y=alt.Y("permeate:Q", title="Permeate (m³/hr)"),
        )
    )
    _month = (_band + _line).properties(
        width=620, height=150, title="The window read (orange), in the month of data"
    )
    _steps = pd.date_range("2025-07-08", periods=48, freq="h")
    _fixed = pd.DataFrame(
        {
            "time": _steps,
            "permeate": preview.values["plant.ro.permeate"]["value"],
        }
    )
    _model = (
        alt.Chart(_fixed)
        .mark_line(interpolate="step-after", color="#f58518", strokeWidth=2)
        .encode(
            x=alt.X("time:T", title="The model's time steps, 8 to 10 July 2025"),
            y=alt.Y("permeate:Q", title="Permeate (m³/hr)"),
            tooltip=["time:T", alt.Tooltip("permeate:Q", format=".1f")],
        )
        .properties(
            width=620, height=150, title="What plant.ro.permeate would be fixed to"
        )
    )
    mo.vstack(
        [
            mo.md(
                f"`offset: {preview_entry['offset']}`, `timezone: {_zone}` reads "
                f"**{_window['start']} to {_window['end']}** ({_zone} wall clock)."
            ),
            _month,
            _model,
        ]
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    `orch.apply_model_inputs(model, entries)` does the same on a built
    model, and fixes the variables. A model from `orch.build` remembers what
    was applied, so a result stored from it records the data's provenance:

    ```python
    model = orch.build("base")
    orch.apply_model_inputs(model, as_operated)
    orch.solve_and_store("as_operated", model)
    ```

    Section 5 does this in a study instead.
    """)
    return


@app.cell
def _(as_operated, mo, orch, pyo):
    _model = orch.build("base")
    orch.apply_model_inputs(_model, as_operated)
    _permeate = _model.plant.ro.permeate
    _fixed = sum(v.fixed for v in _permeate.values())
    mo.md(
        f"After `apply_model_inputs`, **{_fixed} of the {len(_permeate)}** "
        f"`plant.ro.permeate` values are fixed; the first is "
        f"{pyo.value(_permeate[0]):.1f} m³/hr."
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 4. Lining data up with the time steps

    Stored data is rarely at the model's time steps. To show the rules, here
    is the plant's permeate every 15 minutes on 8 June, with a three-hour
    outage, 10:00 to 13:00, where the meter recorded nothing. The model's
    hourly steps are fed from it:

    - **`resample.agg`** combines the samples in one step. Pick it by what
      the column measures:

      | `agg` | Use it for |
      | --- | --- |
      | `mean` (the default) | rates and levels: flow, power, temperature, price |
      | `sum` | amounts per interval: kWh or m³ per 15 minutes, as the step's total |
      | `max`, `min` | peaks and troughs, to size for the worst case |
      | `first`, `last`, `median` | spot readings, or data with outliers |
    - **`resample.window`** says which interval a step takes. `forward` (the
      default): the step at 10:00 takes 10:00 up to 11:00, which is what
      flex-pse means by a time step (it costs a rate as `rate[t] * dt`).
      `backward`: it takes after 09:00 up to 10:00, the hour ending at it.
    - **`gaps`** fills a step with no sample: `interpolate` in time (the
      default), `ffill` (the last value carried on), `zero`, or `error` to
      refuse. `limit` caps how many steps in a row it fills. Data coarser
      than the steps (hourly data on 15-minute steps) leaves steps without a
      sample too, and `gaps` fills them; a `sum` is split evenly over the
      steps its interval covers instead, so its total is kept.

    The result records how many steps were filled (`filled`).
    """)
    return


@app.cell
def _(RO_UNITS, june, np, orch, pd):
    # Every 15 minutes, 7 to 10 June: the hourly data interpolated, plus noise,
    # with nothing recorded from 10:00 to 13:00 on 8 June.
    _quarters = pd.date_range("2025-06-07", "2025-06-10 23:45", freq="15min", tz="UTC")
    _smooth = june["permeate"].reindex(june.index.union(_quarters)).interpolate("time")
    _rng = np.random.default_rng(1)
    _quarterly = _smooth.reindex(_quarters) + _rng.normal(0, 4, len(_quarters))
    _outage = (_quarters >= "2025-06-08 10:00") & (_quarters < "2025-06-08 13:00")
    meter = pd.DataFrame({"permeate": _quarterly[~_outage]})
    _stored = orch.store_data(
        "ro_meter_15min", meter, units={"permeate": RO_UNITS["permeate"]}
    )
    return (meter,)


@app.cell
def _(mo):
    agg = mo.ui.dropdown(
        ["mean", "max", "min", "first", "last", "median"],
        value="mean",
        label="resample.agg",
    )
    window = mo.ui.radio(
        ["forward", "backward"], value="forward", label="resample.window", inline=True
    )
    gaps = mo.ui.dropdown(
        ["interpolate", "ffill", "zero", "error"], value="interpolate", label="gaps"
    )
    mo.hstack([agg, window, gaps], justify="start", gap=2)
    return agg, gaps, window


@app.cell
def _(ModelInputError, agg, gaps, mo, orch, window):
    meter_entry = {
        "dataset": "ro_meter_15min",
        "columns": ["permeate"],
        "rename": {"permeate": "plant.ro.permeate"},
        "offset": "-30D",
        "resample": {"agg": agg.value, "window": window.value},
        "gaps": gaps.value,
    }
    try:
        aligned = orch.resolve_model_inputs("base", [meter_entry])
        refused = None
    except ModelInputError as _exc:
        aligned, refused = None, str(_exc)
    (
        mo.callout(mo.md(f"**`gaps: error` refuses:** {refused}"), kind="danger")
        if (refused)
        else None
    )
    return aligned, meter_entry


@app.cell
def _(aligned, alt, meter, meter_entry, mo, naive, pd):
    mo.stop(aligned is None)
    _shift = pd.Timedelta(days=30)
    _steps = pd.date_range("2025-07-08", periods=48, freq="h")
    _backward = meter_entry["resample"]["window"] == "backward"
    _values = aligned.values["plant.ro.permeate"]["value"]
    # A step is filled when its interval holds no sample.
    _samples = naive(meter.index) + _shift
    _has = []
    for _t in _steps:
        if _backward:
            _inside = (_samples > _t - pd.Timedelta(hours=1)) & (_samples <= _t)
        else:
            _inside = (_samples >= _t) & (_samples < _t + pd.Timedelta(hours=1))
        _has.append(bool(_inside.any()))
    # Each step's value is drawn over the interval it took; a filled step's
    # marker sits in the middle of it.
    _half = pd.Timedelta(minutes=-30 if _backward else 30)
    _fixed = pd.DataFrame(
        {
            "time": _steps,
            "middle": _steps + _half,
            "permeate": _values,
            "step": ["from samples" if h else "filled" for h in _has],
        }
    )
    _points = pd.DataFrame({"time": _samples, "permeate": meter["permeate"].to_numpy()})
    _day = (pd.Timestamp("2025-07-08 05:00"), pd.Timestamp("2025-07-08 18:00"))
    _fixed = _fixed[(_fixed.time >= _day[0]) & (_fixed.time <= _day[1])]
    _points = _points[(_points.time >= _day[0]) & (_points.time <= _day[1])]
    _color = alt.Color(
        "kind:N",
        title=None,
        scale=alt.Scale(
            domain=["15-minute samples", "fixed on the model", "filled step"],
            range=["#9d9d9d", "#4c78a8", "#e45756"],
        ),
    )
    _x = alt.X(
        "time:T",
        title="8 July 2025, the model's time (the samples are 8 June's, +30 days)",
        axis=alt.Axis(format="%H:%M"),
    )
    _y = alt.Y("permeate:Q", title="Permeate (m³/hr)", scale=alt.Scale(zero=False))
    _dots = (
        alt.Chart(_points.assign(kind="15-minute samples"))
        .mark_point(filled=True, size=40)
        .encode(x=_x, y=_y, color=_color)
    )
    _line = (
        alt.Chart(_fixed.assign(kind="fixed on the model"))
        .mark_line(
            interpolate="step-before" if _backward else "step-after", strokeWidth=2.5
        )
        .encode(x=_x, y=_y, color=_color)
    )
    _filled = (
        alt.Chart(_fixed[_fixed.step == "filled"].assign(kind="filled step"))
        .mark_point(shape="diamond", size=140, filled=True)
        .encode(
            x=alt.X("middle:T"),
            y=_y,
            color=_color,
            tooltip=[alt.Tooltip("time:T", title="step"), "permeate:Q"],
        )
    )
    [_record] = aligned.provenance
    mo.vstack(
        [
            (_dots + _line + _filled).properties(
                width=620,
                height=260,
                title=f"agg: {meter_entry['resample']['agg']}, window: "
                f"{meter_entry['resample']['window']}, gaps: {meter_entry['gaps']}",
            ),
            mo.md(
                f"The result would record **filled: "
                f"{_record['filled']['plant.ro.permeate']}** of the 48 steps. "
                "Each line is drawn over the interval its step took."
            ),
        ]
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    With `window: forward` the outage empties the 10:00, 11:00 and 12:00
    steps; with `backward`, only 11:00 and 12:00, since the 10:00 step takes
    the samples up to 10:00. `interpolate` draws a straight line across the
    gap, `ffill` holds the last value, and `zero` drops the flow to nothing.

    **`reduce`** sets a variable to one number instead: a statistic of the
    data over the model's horizon. It suits a parameter that doesn't vary
    over time, such as a membrane's measured specific energy.
    """)
    return


@app.cell
def _(as_operated, mo, orch):
    _reduced = {
        statistic: orch.resolve_model_inputs(
            "base", [{**as_operated, "reduce": statistic}]
        ).values["plant.ro.permeate"]["value"]
        for statistic in ("mean", "min", "max")
    }
    mo.md(
        "`reduce` over 8 to 10 June: "
        + ", ".join(f"**{k}** {v:.1f} m³/hr" for k, v in _reduced.items())
        + ". One number each, not 48."
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 5. Costing the schedule the plant ran

    A study config sets model inputs the same way, for every task
    (`model_inputs`) or per case. Two cases: `optimized` lets the model
    choose the RO's schedule, `as_operated` fixes it to what the plant ran
    in June. Both must still deliver 90 m³/hr from the tank, and end the
    two days with the tank at least as full as it started (it's cyclic).
    June's permeate averages a little over 90 m³/hr, so the fixed schedule
    can: its tank ends fuller than it started.
    """)
    return


@app.cell
def _(as_operated, mo, orch, yaml):
    keep = [
        "plant.ro.permeate",
        "plant.ro.power_electrical",
        "plant.tank.volume",
        "costing.aggregate_operating_cost",
    ]
    study_config = {
        "base_model_spec": "base",
        "sweeps": [
            {
                "type": "cases",
                "cases": [
                    {"name": "optimized"},
                    {"name": "as_operated", "model_inputs": [as_operated]},
                ],
            }
        ],
        "export": {"include": keep},
        "output_name": "{case}",
    }
    study_run = orch.execute_study(study_config)
    mo.vstack(
        [
            mo.md(
                "```yaml\n"
                + yaml.safe_dump(study_config, sort_keys=False, width=100)
                + "```"
            ),
            mo.md(f"**{study_run.counts()}**"),
        ]
    )
    return (study_run,)


@app.cell
def _(alt, energy_price, mo, naive, orch, pd, study_run):
    _frames, _costs = [], []
    for _task in study_run.tasks:
        _result = orch.result(_task.tag)
        _data = _result.data()
        _costs.append(
            {
                "case": _task.tag,
                "operating cost (USD)": _result.scalars()[
                    "costing.aggregate_operating_cost"
                ],
            }
        )
        for _key, _label in [
            ("plant.ro.permeate", "RO permeate (m³/hr)"),
            ("plant.tank.volume", "Tank volume (m³)"),
        ]:
            _s = _data[_key]
            _frames.append(
                pd.DataFrame(
                    {
                        "time": naive(pd.DatetimeIndex(_s.timestamps)),
                        "value": _s.values,
                        "series": _label,
                        "case": _task.tag,
                    }
                )
            )
    _series = pd.concat(_frames)
    _steps = pd.date_range("2025-07-08", periods=48, freq="h")
    _price = energy_price(_steps)
    _peak = pd.DataFrame(
        {
            "start": _steps[_price > 0.1],
            "end": _steps[_price > 0.1] + pd.Timedelta("1h"),
        }
    )
    _band = (
        alt.Chart(_peak)
        .mark_rect(color="#f58518", opacity=0.15)
        .encode(x="start:T", x2="end:T")
    )
    _panels = [
        alt.layer(
            _band,
            alt.Chart(_series[_series.series == _label])
            .mark_line(interpolate="step-after", strokeWidth=2)
            .encode(
                x=alt.X("time:T", title="8 to 10 July 2025 (peak price shaded)"),
                y=alt.Y("value:Q", title=_label),
                color=alt.Color(
                    "case:N",
                    title=None,
                    scale=alt.Scale(
                        domain=["optimized", "as_operated"],
                        range=["#4c78a8", "#e45756"],
                    ),
                ),
            ),
        ).properties(width=620, height=170)
        for _label in ("RO permeate (m³/hr)", "Tank volume (m³)")
    ]
    _cost = (
        alt.Chart(pd.DataFrame(_costs))
        .mark_bar()
        .encode(
            x=alt.X("operating cost (USD):Q"),
            y=alt.Y("case:N", title=None, sort=["optimized", "as_operated"]),
            color=alt.Color(
                "case:N",
                legend=None,
                scale=alt.Scale(
                    domain=["optimized", "as_operated"], range=["#4c78a8", "#e45756"]
                ),
            ),
            tooltip=["case", alt.Tooltip("operating cost (USD):Q", format=",.2f")],
        )
        .properties(width=620, height=80, title="Operating cost over the two days")
    )
    _by_case = {c["case"]: c["operating cost (USD)"] for c in _costs}
    mo.vstack(
        [
            alt.vconcat(*_panels),
            _cost,
            mo.md(
                "Running June's schedule costs "
                f"**{_by_case['as_operated']:,.2f} USD**; "
                f"the optimized one **{_by_case['optimized']:,.2f} USD**. The "
                "optimized RO runs at about 114 m³/hr off-peak, filling the tank "
                "by about 24 m³ an hour, then stops through the afternoon peak "
                "while the tank supplies the product, and ends at the 750 m³ it "
                "started with. June's schedule ran through the peak, and made "
                "more water than the plant needed: its tank ends at about 869 m³."
            ),
        ]
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    The `as_operated` result records where its permeate came from
    (`result.model_inputs`): the dataset and its run, the window read, the
    steps filled and a hash of the values. The values themselves are in
    `result.overrides`, so the model can be rebuilt exactly
    (`orch.build(result.spec_tag, **result.overrides)`).
    """)
    return


@app.cell
def _(mo, orch, pd):
    _result = orch.result("as_operated")
    _provenance = pd.DataFrame(
        [
            {
                "paths": ", ".join(_source["paths"]),
                "dataset": _source["resolved"]["dataset"],
                "window start": _source["window"]["start"],
                "window end": _source["window"]["end"],
                "timezone": _source["timezone"],
                "steps filled": sum(_source["filled"].values()),
                "data_hash": _source["data_hash"][:19] + "...",
            }
            for _source in _result.model_inputs
        ]
    )
    _override = _result.overrides["plant.ro.permeate"]
    mo.vstack(
        [
            mo.ui.table(_provenance, selection=None, show_column_summaries=False),
            mo.md(
                f"`result.overrides['plant.ro.permeate']`: "
                f"{len(_override['value'])} values in {_override['units']}, "
                f"starting {', '.join(f'{v:.1f}' for v in _override['value'][:3])}, ..."
            ),
        ]
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 6. Rules worth knowing

    - **Each variable is set one way.** A path can't be set both by a model
      input and by a build override, a `variable` sweep or a case's
      `variables`; the orchestrator refuses rather than pick one. Two entries
      in one list can't set the same path either.
    - **A swept entry replaces a base one.** An entry in a `model_inputs`
      sweep, or in a case, replaces the config's `model_inputs` entry for
      the same path, so a scenario names only the streams it changes.
    - **Models with several time blocks**, such as a design study's
      horizons, line each path up with the time block above it, and a `*`
      such as `horizon[2030,*]` picks horizons: see tutorial 5.

    The first rule, shown:
    """)
    return


@app.cell
def _(ModelInputError, as_operated, mo, orch):
    _model = orch.build("base", **{"plant.ro.permeate": 90.0})
    try:
        orch.apply_model_inputs(_model, as_operated)
        _message = "no error"
    except ModelInputError as _exc:
        _message = str(_exc)
    mo.vstack(
        [
            mo.md(
                "```python\n"
                'model = orch.build("base", **{"plant.ro.permeate": 90.0})\n'
                "orch.apply_model_inputs(model, as_operated)\n```"
            ),
            mo.callout(mo.md(f"`ModelInputError`: {_message}"), kind="warn"),
        ]
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## Where next

    - The docs' **Studies from a config** page has every model input option.
    - Tutorial 5 sets a design study's horizons from a projection per
      modeled year.
    """)
    return


if __name__ == "__main__":
    app.run()
