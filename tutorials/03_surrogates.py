import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium", app_title="Surrogates")


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    # Surrogates

    A process model needs a relation for each unit it can optimise over. An
    RO's power depends on how hard it runs, but the true curve may be
    unknown, or too awkward for the solver. A **surrogate** is a simpler
    relation fitted from operating data, which flex-pse swaps into the unit.

    The plant here: feed, RO, tank, product. It must deliver 90 m³/hr
    while buying electricity on a summer time-of-use tariff, and the tank
    lets the RO run harder off-peak. This tutorial:

    1. stores a month of RO operating data and looks at the true curve
       behind it;
    2. fits two surrogates in Python, and stores them;
    3. solves the plant with each, and compares the schedules;
    4. checks each model's energy against the true curve;
    5. refits from a narrower operating range (the slider), to see how the
       data's range limits a fit;
    6. does the same in a study config, with `fits` and a `surrogate` sweep.
    """)
    return


@app.cell
def _():
    import json
    import pprint
    import tempfile

    import altair as alt
    import marimo as mo
    import numpy as np
    import pandas as pd
    from flexparameterize import ConstantIntensityRegressor, LinearRegressor
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
        ConstantIntensityRegressor,
        FileBackend,
        LinearRegressor,
        Orchestrator,
        RO_UNITS,
        alt,
        energy_price,
        json,
        mo,
        naive,
        np,
        operating_data,
        pd,
        pprint,
        ro_spec,
        sec,
        tempfile,
    )


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 1. The operating data and the true curve

    The RO's specific energy (kWh per m³ of permeate) is lowest at its
    design point, 90 m³/hr, and rises either side:

    $$\text{SEC}(q) = 2.5 + 0.0004\,(q - 90)^2 \quad \text{kWh/m³}$$

    so its power is $\text{SEC}(q)\cdot q$. A month of hourly data, June
    2025, with the permeate spread over 30 to 150 m³/hr and 2% noise on the
    power, is stored as the dataset `ro_operation`, and the plant's model
    spec as `base`.
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
    orch.create_study("surrogates")
    orch.store_data("ro_operation", operating_data(), units=RO_UNITS)
    orch.store_model_spec("base", ro_spec(), base_dir=ASSETS)
    # Every fit below reads the data back from the store.
    data = orch.get_data("ro_operation")
    return data, orch


@app.cell
def _(alt, data, np, pd, sec):
    def curve_frame(fits, low=30.0, high=150.0):
        """``{name: power(q)}`` sampled over ``low`` to ``high`` m³/hr."""
        q = np.linspace(low, high, 61)
        return pd.concat(
            [
                pd.DataFrame({"permeate": q, "power": fn(q), "relation": name})
                for name, fn in fits.items()
            ]
        )

    def relation_chart(points, fits, title, span=None):
        """The data as dots, each relation as a line, one legend for all."""
        names = ["operating data", *fits]
        colors = ["#9d9d9d", "#54a24b", "#f58518", "#4c78a8"][: len(names)]
        color = alt.Color(
            "relation:N", title=None, scale=alt.Scale(domain=names, range=colors)
        )
        x = alt.X("permeate:Q", title="RO permeate (m³/hr)")
        y = alt.Y("power:Q", title="RO power (kW)")
        dots = (
            alt.Chart(points.assign(relation="operating data"))
            .mark_point(filled=True, size=12, opacity=0.35)
            .encode(x=x, y=y, color=color)
        )
        lines = (
            alt.Chart(curve_frame(fits))
            .mark_line(strokeWidth=3)
            .encode(
                x=x,
                y=y,
                color=color,
                # The true curve solid; each fit dashed.
                strokeDash=alt.StrokeDash(
                    "relation:N",
                    legend=None,
                    scale=alt.Scale(
                        domain=list(fits),
                        range=[[1, 0], [8, 4], [3, 3]][: len(fits)],
                    ),
                ),
            )
        )
        layers = [dots, lines]
        if span is not None:
            band = (
                alt.Chart(pd.DataFrame({"low": [span[0]], "high": [span[1]]}))
                .mark_rect(color="#4c78a8", opacity=0.08)
                .encode(x="low:Q", x2="high:Q")
            )
            layers.insert(0, band)
        return alt.layer(*layers).properties(width=560, height=300, title=title)

    _points = pd.DataFrame(
        {"permeate": data["permeate"], "power": data["power_electrical"]}
    )
    relation_chart(
        _points,
        {"true curve: SEC(q)·q": lambda q: sec(q) * q},
        "June's hourly operating data, and the curve it came from",
    )
    return (relation_chart,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 2. Fit two surrogates

    flex-pse's regressors (`flexparameterize`) fit a relation from a frame
    of inputs and outputs, here read back from the store:

    - **`ConstantIntensityRegressor`**: one energy intensity (kWh/m³), so
      power = intensity · permeate. Linear through zero.
    - **`LinearRegressor`**: power = slope · permeate + intercept.

    `to_surrogate_spec()` turns a fit into a surrogate spec, plain data
    that the orchestrator stores like a model spec.
    """)
    return


@app.cell
def _(ConstantIntensityRegressor, LinearRegressor, data, json, mo, orch):
    def fit_both(frame):
        """Both fits from ``frame``'s permeate and power columns."""
        X, y = frame[["permeate"]], frame[["power_electrical"]]
        intensity = ConstantIntensityRegressor().fit(X, y)
        line = LinearRegressor().fit(
            X, y, input_units={"permeate": "m^3/hr"}, output_units="kW"
        )
        return intensity, line

    average, linear = fit_both(data)
    orch.store_surrogate_spec("ro_average", average.to_surrogate_spec())
    orch.store_surrogate_spec("ro_linear", linear.to_surrogate_spec())
    _stored = orch.surrogate_spec("ro_linear").spec
    mo.vstack(
        [
            mo.md(
                f"- average intensity: **{average.coefficient:.3f} kWh/m³**\n"
                f"- linear: power = **{linear.coefficients['permeate']:.3f}** · "
                f"permeate **{linear.coefficients['intercept']:+.1f}** kW"
            ),
            mo.md("The stored `ro_linear` surrogate spec:"),
            mo.md(f"```json\n{json.dumps(_stored, indent=2, default=str)}\n```"),
        ]
    )
    return average, fit_both, linear


@app.cell
def _(average, data, linear, pd, relation_chart, sec):
    def fit_relations(intensity, line):
        """Each fit as a function of permeate, with the true curve."""
        slope = line.coefficients["permeate"]
        intercept = line.coefficients["intercept"]
        return {
            "true curve: SEC(q)·q": lambda q: sec(q) * q,
            f"average: {intensity.coefficient:.2f}·q": (
                lambda q: intensity.coefficient * q
            ),
            f"linear: {slope:.2f}·q {intercept:+.0f}": lambda q: slope * q + intercept,
        }

    relation_chart(
        pd.DataFrame({"permeate": data["permeate"], "power": data["power_electrical"]}),
        fit_relations(average, linear),
        "Both fits over the data",
    )
    return (fit_relations,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    Both are straight lines, and the true curve bends: both lie above it
    between about 55 and 125 m³/hr, around the design point where the RO is
    most efficient, and below it at either end.

    ## 3. Solve the plant with each

    A surrogate reaches the model two ways:

    - **A build override** fixes a variable when the model is built: the
      average fixes `plant.ro.energy_intensity`, so nothing in the spec
      changes.
    - **`orch.apply_surrogate`** returns a copy of a stored model spec with
      the surrogate swapped into a unit's power relation. Stored with
      `parent="base"`, it reuses the base spec's files (the tariff) and
      records where it came from.

    ```python
    fixed = {"plant.ro.energy_intensity": {"value": average.coefficient,
                                           "units": "kWh/m^3"}}
    orch.solve_and_store("fixed", orch.build("base", **fixed), include=KEEP)

    spec = orch.apply_surrogate("base", unit="plant.ro", surrogate="ro_linear")
    orch.store_model_spec("ro_linear", spec, parent="base")
    orch.solve_and_store("linear", orch.build("ro_linear"), include=KEEP)
    ```
    """)
    return


@app.cell
def _(alt, energy_price, naive, orch, pd, sec):
    KEEP = [
        "plant.ro.permeate",
        "plant.ro.power_electrical",
        "plant.tank.volume",
        "costing.aggregate_operating_cost",
    ]

    def solve_pair(intensity, line, prefix, replace=False):
        """Solve with the average (an override) and the linear surrogate (a spec).

        Returns ``{label: result tag}``.
        """
        fixed = {
            "plant.ro.energy_intensity": {
                "value": intensity.coefficient,
                "units": "kWh/m^3",
            }
        }
        tags = {"average": f"{prefix}_average", "linear": f"{prefix}_linear"}
        orch.solve_and_store(
            tags["average"], orch.build("base", **fixed), include=KEEP, replace=replace
        )
        orch.store_surrogate_spec(
            f"{prefix}_ro_linear", line.to_surrogate_spec(), replace=replace
        )
        spec = orch.apply_surrogate(
            "base", unit="plant.ro", surrogate=f"{prefix}_ro_linear"
        )
        orch.store_model_spec(
            f"{prefix}_ro_linear", spec, parent="base", replace=replace
        )
        orch.solve_and_store(
            tags["linear"],
            orch.build(f"{prefix}_ro_linear"),
            include=KEEP,
            replace=replace,
        )
        return tags

    def series_of(tags):
        """Each result's schedule, one row per hour, with true and model power."""
        frames = []
        for label, tag in tags.items():
            frame = orch.result(tag).to_frame()
            permeate = frame["plant.ro.permeate"]
            frames.append(
                pd.DataFrame(
                    {
                        "time": naive(frame.index),
                        "permeate": permeate.to_numpy(),
                        "tank": frame["plant.tank.volume"].to_numpy(),
                        "model kW": frame["plant.ro.power_electrical"].to_numpy(),
                        "true kW": (sec(permeate) * permeate).to_numpy(),
                        "surrogate": label,
                    }
                )
            )
        return pd.concat(frames)

    def schedule_chart(tags, title):
        """RO permeate and tank volume per surrogate, peak-price hours shaded."""
        frame = series_of(tags)
        times = pd.DatetimeIndex(frame["time"].unique())
        price = energy_price(times)
        peaks = pd.DataFrame(
            {
                "start": times[price.to_numpy() > 0.1],
                "end": times[price.to_numpy() > 0.1] + pd.Timedelta(hours=1),
                "hours": "peak price, 0.18 $/kWh",
            }
        )
        shade = (
            alt.Chart(peaks)
            .mark_rect(opacity=0.18)
            .encode(
                x="start:T",
                x2="end:T",
                color=alt.Color(
                    "hours:N",
                    title=None,
                    scale=alt.Scale(range=["#e45756"]),
                ),
            )
        )
        color = alt.Color(
            "surrogate:N",
            title="surrogate",
            scale=alt.Scale(domain=["average", "linear"], range=["#f58518", "#4c78a8"]),
        )
        x = alt.X("time:T", title="July 2025", axis=alt.Axis(format="%d %H:%M"))

        def panel(field, label):
            line = (
                alt.Chart(frame)
                .mark_line(interpolate="step-after", strokeWidth=2.5)
                .encode(x=x, y=alt.Y(f"{field}:Q", title=label), color=color)
            )
            return (
                (shade + line)
                .resolve_scale(color="independent")
                .properties(width=600, height=170)
            )

        return alt.vconcat(
            panel("permeate", "RO permeate (m³/hr)"),
            panel("tank", "Tank volume (m³)"),
        ).properties(title=title)

    def energy_check(tags):
        """Per surrogate: the RO energy the model counted, and the true energy."""
        frame = series_of(tags)
        rows = []
        for label, group in frame.groupby("surrogate", sort=False):
            cost = orch.result(tags[label]).scalars()[
                "costing.aggregate_operating_cost"
            ]
            rows.append(
                {
                    "surrogate": label,
                    "cost ($)": round(cost, 2),
                    "model kWh": round(group["model kW"].sum()),
                    "true kWh": round(group["true kW"].sum()),
                }
            )
        table = pd.DataFrame(rows)
        table["error"] = (table["model kWh"] / table["true kWh"] - 1).map(
            lambda v: f"{v:+.1%}"
        )
        bars = (
            alt.Chart(
                table.melt("surrogate", ["model kWh", "true kWh"], "energy", "kWh")
            )
            .mark_bar()
            .encode(
                x=alt.X("energy:N", title=None, axis=alt.Axis(labels=False)),
                y=alt.Y("kWh:Q", title="RO energy over the two days (kWh)"),
                color=alt.Color(
                    "energy:N",
                    title=None,
                    scale=alt.Scale(
                        domain=["model kWh", "true kWh"], range=["#4c78a8", "#54a24b"]
                    ),
                ),
                column=alt.Column("surrogate:N", title=None),
                tooltip=["surrogate", "energy", "kWh"],
            )
            .properties(width=120, height=220)
        )
        return table, bars

    return energy_check, schedule_chart, solve_pair


@app.cell
def _(average, linear, mo, orch, schedule_chart, solve_pair):
    python_tags = solve_pair(average, linear, "python")
    _costs = {
        label: orch.result(tag).scalars()["costing.aggregate_operating_cost"]
        for label, tag in python_tags.items()
    }
    mo.vstack(
        [
            mo.md(
                "Operating cost over 8 to 10 July: "
                f"**average ${_costs['average']:,.2f}**, "
                f"**linear ${_costs['linear']:,.2f}**."
            ),
            schedule_chart(python_tags, "The schedule each model picked"),
        ]
    )
    return (python_tags,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    Off-peak, both run the RO at about 111 to 114 m³/hr, above the
    90 m³/hr the product takes, filling the tank, and draw the tank down
    through the 16:00 to 21:00 peak. The tank is cyclic, so each ends the two
    days back at the 750 m³ it started with. The average stops the RO in the
    peak. The linear one keeps it at about 12 m³/hr: with its intercept of
    −40 kW, that flow costs nothing in the model (3.42 · 12 − 40 ≈ 0 kW),
    where the true RO would draw about 58 kW.

    ## 4. Check against the truth

    Each model counts the RO's energy with its own surrogate. The data came
    from a known curve, so we can also ask what the RO would **really** use
    running each schedule: $\sum_t \text{SEC}(q_t)\, q_t$ over the hours.
    """)
    return


@app.cell
def _(energy_check, mo, python_tags):
    _table, _bars = energy_check(python_tags)
    mo.vstack([_bars, mo.ui.table(_table, selection=None)])
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    Both models overcount, by about 8 to 9%. Their schedules run at about
    111 to 114 m³/hr, where both lines lie above the curve: the true RO
    needs about 2.7 kWh/m³ there, the average says 2.98. The linear fit's
    count is a little closer, partly because it counts its peak-hour
    trickle as free. The fits were made over the whole range of the data,
    30 to 150 m³/hr, but the model only runs in a narrow band of it.

    ## 5. Fit from a narrower operating range

    A plant rarely runs over its whole range. Suppose the data only covers
    the range below: the fits see only that part of the curve, and the
    model uses them everywhere.
    """)
    return


@app.cell
def _(mo):
    fit_range = mo.ui.range_slider(
        30,
        150,
        step=10,
        value=[60, 120],
        label="Operating range in the data (m³/hr)",
        full_width=False,
    )
    fit_range
    return (fit_range,)


@app.cell
def _(
    data,
    energy_check,
    fit_both,
    fit_range,
    fit_relations,
    mo,
    pd,
    relation_chart,
    solve_pair,
):
    _low, _high = fit_range.value
    _part = data[(data["permeate"] >= _low) & (data["permeate"] <= _high)]
    _average, _linear = fit_both(_part)
    # The same tags on each move of the slider, replaced.
    _tags = solve_pair(_average, _linear, "window", replace=True)
    _table, _bars = energy_check(_tags)
    mo.vstack(
        [
            mo.md(
                f"**{len(_part)}** of the **{len(data)}** hours ran between "
                f"{_low} and {_high} m³/hr."
            ),
            relation_chart(
                pd.DataFrame(
                    {"permeate": _part["permeate"], "power": _part["power_electrical"]}
                ),
                fit_relations(_average, _linear),
                f"Fitted on {_low} to {_high} m³/hr only (shaded)",
                span=(_low, _high),
            ),
            _bars,
            mo.ui.table(_table, selection=None),
        ]
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    With the starting range, 60 to 120 m³/hr, both fits move down, and now
    undercount a little: −4.5% and −4.9%, from +8.7% and +8.1%. The plant
    runs at about 111 to 114 m³/hr, near the top of that range, where the
    true curve is above the fits. Try 80 to 100, around the design point:
    both undercount by about 8%, since the plant runs above what they saw.
    Try 110 to 120, around where the plant runs: the average counts within
    1% of the truth, but the linear fit is steep, with an intercept of about
    −285 kW, and gives no power below about 54 m³/hr, so it undercounts by
    about 23%. Try 120 to 150: the intercept is about −640 kW, and the fit
    gives no power below about 79 m³/hr. The model runs the RO at about
    79 m³/hr through the peak as if it were free, and at the first time step
    counts −637 kW of power: it counts 66% less energy than the RO would
    really use. A surrogate holds only over the data it was fitted on, and
    the model is free to run anywhere, so fit over the range the plant will
    run in, and check the schedule against it.

    ## 6. The same in a study config

    A study config does all of this as data. `fits` fits surrogates from
    stored data (with the same regressors, by name) and stores them; a
    `surrogate` sweep swaps each into a unit, one task per surrogate.
    `orch.fit_surrogate(...)` runs one fit the same way, outside a study.
    """)
    return


@app.cell
def _(mo, orch, pd, pprint, python_tags):
    study = {
        "base_model_spec": "base",
        "fits": [
            {
                "tag": "{name}",
                "X": {"dataset": "ro_operation", "columns": ["permeate"]},
                "y": {"dataset": "ro_operation", "columns": ["power_electrical"]},
                "variants": [
                    {"name": "fixed_intensity", "regressor": "constant_intensity"},
                    {"name": "linear_surrogate", "regressor": "multilinear"},
                ],
            }
        ],
        "sweeps": [
            {
                "type": "surrogate",
                "unit": "plant.ro",
                "surrogates": ["fixed_intensity", "linear_surrogate"],
            }
        ],
        "export": {"include": ["costing.aggregate_operating_cost"]},
        "output_name": "study_{surrogate}",
    }
    study_run = orch.execute_study(study)
    _python = {
        "fixed_intensity": python_tags["average"],
        "linear_surrogate": python_tags["linear"],
    }
    _cost = "costing.aggregate_operating_cost"
    _rows = [
        {
            "surrogate": name,
            "study cost ($)": round(orch.result(f"study_{name}").scalars()[_cost], 2),
            "by hand, section 3 ($)": round(orch.result(tag).scalars()[_cost], 2),
        }
        for name, tag in _python.items()
    ]
    mo.vstack(
        [
            mo.md(f"**{study_run.counts()}**"),
            mo.md(
                "```python\n"
                + "study = "
                + pprint.pformat(study, sort_dicts=False, width=84)
                + "\n```"
            ),
            mo.ui.table(pd.DataFrame(_rows), selection=None),
        ]
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    The study's costs match the ones solved by hand: the fits are the same
    regressors on the same stored data. The study applies the constant
    intensity as a surrogate rather than an override, and gets the same
    cost.

    ## Where next

    - **Studies and sweeps** (tutorial 4) combines a surrogate sweep with
      other sweeps, and plans them with a dry run.
    - A fit's `variants`, `window`, `prep` and `validation` are in the docs'
      reference for `FitParams`.
    """)
    return


if __name__ == "__main__":
    app.run()
