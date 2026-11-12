# Studies from a config

A study config holds a whole study as data: the base model spec, the
surrogate fits, the sweeps, and what to store. It can be a dict, a YAML file
or a JSON file. Its model is {class}`~flexorchestrator.study.StudyConfig`, and
its JSON Schema is `docs/schemas/study_config.schema.json` (see
{doc}`../reference/schemas`).

```yaml
base_model_spec: base
fits:
  - tag: "ro_{name}"
    X: {dataset: ro_operation, columns: [permeate]}
    y: {dataset: ro_operation, columns: [power_electrical]}
    variants:
      - {name: average, regressor: constant_intensity}
      - {name: linear, regressor: multilinear}
sweeps:
  - type: surrogate
    unit: plant.ro
    surrogates: [ro_average, ro_linear]
export: {include: ["plant.ro.*", "costing.aggregate_operating_cost"]}
output_name: "{surrogate}"
```

Run it from Python, or from the command line:

```python
orch.execute_study("study.yaml")
```

```
flex-orch --data-dir .flexorch run study.yaml --study my_study [--dry-run]
```

## How a study runs

1. **Expand.** The sweeps' values are combined, as a `product` (the default)
   or with `zip`, and each combination's tag is rendered from `output_name`.
   A repeated or invalid tag fails here, before anything runs. `--dry-run`
   stops here and returns the plan.
2. **Check.** With `on_existing: error` (the default), a task whose tag is
   already stored raises before anything runs. `skip` leaves stored tasks
   alone, and `replace` solves them again.
3. **Fit.** The `fits` run first, so the sweeps can use the surrogates they
   store.
4. **Solve.** The tasks run one after another in this process, or on Ray when
   `execution.max_workers` is above 1 (see {doc}`parallel`). The stored
   results are the same either way.

Each call is recorded in the `flexorch::commands` ledger, with its parameters
and summary.

## The sweep types

A sweep varies one thing:

| Type | Varies |
| --- | --- |
| `surrogate` | The surrogate a unit uses. |
| `variable` | A model variable, fixed to each value. Use it for anything that is a Pyomo `Var`. |
| `configuration_option` | An option the model is built with, such as a unit's construction option or a price. The path is any element's name followed by the option. |
| `spec_list` | The model spec itself. |
| `model_inputs` | The stored data that model variables are set from. |
| `cases` | Named, hand-picked combinations of the above. |

```python
{"type": "variable", "path": "plant.ro.energy_intensity",
 "values": [{"value": 2.5, "units": "kWh/m^3"}, {"value": 3.0, "units": "kWh/m^3"}]}

{"type": "configuration_option", "path": "plant.feed.max_withdrawal",
 "values": [{"value": 160, "units": "m^3/hr"}, {"value": 200, "units": "m^3/hr"}]}

{"type": "cases", "cases": [
    {"name": "linear_tight_feed",
     "surrogates": {"plant.ro": "linear_surrogate"},
     "configuration_options": {"plant.feed.max_withdrawal.value": 160}},
    {"name": "aged_membranes",
     "variables": {"plant.ro.energy_intensity": {"value": 3.5, "units": "kWh/m^3"}}},
]}
```

The [`examples/ro_study/`](https://github.com/flex-pse/flex-orchestrator/tree/main/examples/ro_study)
folder has one script per sweep type.

## Model inputs

`model_inputs`, outside the sweeps, sets model variables from stored data in
every task. The data can be a dataset, a stored result, or Acquirium streams,
selected the same way as a fit's `X` and `y`. For example, a model can be
costed against the schedule a plant actually ran.

```yaml
model_inputs:
  - dataset: ro_operation
    columns: [permeate]
    rename: {permeate: plant.ro.permeate}  # the path it sets
    timezone: America/Los_Angeles          # the plant's wall clock
    offset: -30D                           # read the month before
```

Each column's data is read over the model's horizon, converted to the plant's
time zone, resampled onto the model's time steps, and its gaps filled
(`gaps`, `limit`). With `reduce`, it becomes one number instead. Every result
records where its inputs came from: the data read, the window, the steps
filled, and a hash of the values.

### Lining data up with the time steps

A time step at `t`, `dt` long, takes the samples of one interval and combines
them with `resample.agg` (`mean` by default; `sum`, `min`, `max`, `median`,
`first` or `last`). `resample.window` says which interval:

| `window` | Step `t` takes | Use it for |
| --- | --- | --- |
| `forward` (default) | `[t, t + dt)`, the interval starting at `t` | rates and costs: flow, power, price |
| `backward` | `(t - dt, t]`, the interval ending at `t` | a value that sums up what happened up to `t` |

`forward` is what flex-pse means by a time step: its time points are interval
starts, and it costs a rate as `rate[t] * dt`. So for most inputs, keep the
default and pick `agg` by what the column measures:

```yaml
model_inputs:
  - dataset: meter
    columns: [flow]
    rename: {flow: "pump.inlet_state.flow_vol_phase[*,Liq]"}
    resample: {agg: mean}                    # a rate: average it
  - dataset: meter
    columns: [energy]
    rename: {energy: plant.energy}
    resample: {agg: sum, window: backward}   # kWh per interval, up to t
```

- **The data's own stamps.** `forward` reads a sample stamped `s` as the
  interval starting at `s`, and `backward` as the interval ending at `s`. Data
  stamped the other way, such as a meter that stamps 10:00 to 10:15 as 10:15,
  read `forward`, is moved onto its interval's start with `offset: 15min`.
- **Data coarser than the steps.** A step with no sample is filled by `gaps`
  (`interpolate` by default). A `sum` is different: each sample's amount is
  split evenly over the steps its interval covers, so hourly kWh on
  15-minute steps puts a quarter on each, and the total is kept. That needs
  the data's spacing to be a whole number of steps.
- **The first backward step** reads data from before the horizon; the
  recorded window shows it.
- **The recorded window** is in the plant's wall clock (the entry's
  `timezone`), like the model's time steps, not in UTC.

### Models with several time blocks

A model can have more than one time axis. A design study's merged model has a
`time_block` in each horizon, `horizon[2030,7]`. A hand-built model can give
two plants a time block each:

```python
m.plant_a.time_block = TimeBlock(...)
m.plant_b.time_block = TimeBlock(...)
```

Each path is lined up with the **nearest time block above it**, and read over
that block's own dates:

- `plant_a.var_a` is read over `plant_a`'s horizon, and `plant_b.var_b` over
  `plant_b`'s.
- `horizon[2030,7].pump.flow` sets July 2030 only, and `horizon[2030,*].pump.flow`
  every 2030 horizon (see [Model inputs in a design study](#model-inputs-in-a-design-study)).
- A path under no time block is set in **every** one, relative to each. In a
  design study, the base flowsheet's `pump.flow` sets every horizon, each from
  its own year and month.

The records are kept per time block, each with its own window. The
`time_grid` hook gives the time axes: one per time block, named by the path
of the block that holds it.

## Design studies

A `design:` section runs each task as a design study. The model spec is run
over every planning year and representative month, and it sizes shared
capacities such as a battery or solar. Leave the section out for an ordinary
study. See
[`examples/design_study/`](https://github.com/flex-pse/flex-orchestrator/tree/main/examples/design_study).

### Model inputs in a design study

A design study runs the base flowsheet once per **horizon**, a modeled year
and representative month, named `horizon[<year>,<month>]`. Each horizon has
its own dates: `horizon[2035,7]` runs in July 2035, and a prorated month
(`days`) only on its modeled days. A model input answers two questions:
**which horizons** it sets, and **which data** each of them reads.

**Which horizons: the path.** An entry's `rename` gives the path it sets.

| Path | Sets |
| --- | --- |
| `pump.flow` (a base flowsheet path) | every horizon |
| `horizon[2030,7].pump.flow` | July 2030 only |
| `horizon[2030,*].pump.flow` | every 2030 horizon |
| `horizon[*,7].pump.flow` | every July |

A `*` in the horizon's index matches any year or month. A `*` in the
variable's own index, as in `flow_vol_phase[*,Liq]`, is part of the variable
path and has nothing to do with horizons. A `*` that matches no horizon,
such as `horizon[2040,*]` in a study that doesn't model 2040, is an error.
Two entries can't set the same variable in the same horizon: if a bare path
and `horizon[2030,*]` both name it, the study refuses.

**Which data: each horizon reads its own dates.** Whatever the path, each
horizon it sets reads the data at that horizon's dates, and resamples it onto
that horizon's time steps. So the data has to exist at those dates, or be
moved there. Modeled years are often in the future, so there are three ways
to supply them:

1. **A projection per year (recommended for forecasts).** Store one dataset
   per modeled year, at that year's dates, and set each in its own year's
   horizons with a wildcard:

   ```yaml
   model_inputs:
     - dataset: projection_2030
       columns: [flow]
       rename: {flow: "horizon[2030,*].pump.inlet_state.flow_vol_phase[*,Liq]"}
     - dataset: projection_2035
       columns: [flow]
       rename: {flow: "horizon[2035,*].pump.inlet_state.flow_vol_phase[*,Liq]"}
   ```

   Each 2030 horizon reads its own month of `projection_2030`. The
   [`design_study` example](https://github.com/flex-pse/flex-orchestrator/tree/main/examples/design_study)
   does this, with projections made from a year of meter data.

2. **One dataset that covers every modeled year.** When a single dataset
   already holds data at every horizon's dates, a base flowsheet path is
   enough, and each horizon picks out its own dates:

   ```yaml
   model_inputs:
     - dataset: forecast_2030_2040
       columns: [flow]
       rename: {flow: "pump.inlet_state.flow_vol_phase[*,Liq]"}
   ```

3. **Past data as it is.** `data_years` maps a modeled year to the year whose
   dates it reads, so every horizon reuses the same measured year:

   ```yaml
   model_inputs:
     - dataset: metered_demand
       columns: [flow]
       rename: {flow: "pump.inlet_state.flow_vol_phase[*,Liq]"}
       data_years: {2030: 2025, 2035: 2025}  # both read 2025's dates
   ```

   `offset` still shifts the window within the data year.

These combine: `data_years` works with any path, so a projection stored at
2025's dates can be read into `horizon[2035,*]` with
`data_years: {2035: 2025}`.

Every result records, per horizon, the dataset it read, the window, the steps
it filled, and a hash of the values (`orch.result(tag).model_inputs`).

**Sweeping over scenarios.** To size the plant under several demand futures,
such as a projection and noisy draws of it, store each scenario's datasets
and sweep over them with a `model_inputs` sweep. Each value is a labelled set
of entries, written exactly as `model_inputs` entries are:

```yaml
sweeps:
  - type: model_inputs
    name: scenario
    values:
      - label: growth
        inputs:
          - {dataset: projection_2030, columns: [flow],
             rename: {flow: "horizon[2030,*].pump.inlet_state.flow_vol_phase[*,Liq]"}}
          - {dataset: projection_2035, columns: [flow],
             rename: {flow: "horizon[2035,*].pump.inlet_state.flow_vol_phase[*,Liq]"}}
      - label: noisy_1
        inputs:
          - {dataset: noisy_1_2030, columns: [flow],
             rename: {flow: "horizon[2030,*].pump.inlet_state.flow_vol_phase[*,Liq]"}}
          - {dataset: noisy_1_2035, columns: [flow],
             rename: {flow: "horizon[2035,*].pump.inlet_state.flow_vol_phase[*,Liq]"}}
output_name: "{scenario}"
```

Each task sizes the plant for one scenario. A swept entry replaces the
config's `model_inputs` entry for the same variable, so a scenario can name
only the streams it changes and take the rest from `model_inputs`. Combined
with another sweep, such as a solar price, every scenario runs at every
price. The `design_study` example's `sweep_scenarios.py` runs this with three
scenarios.

## Hooks are plain data

A config names its hooks as import strings (`"package.module:function"`) or
registered names, never as callables. That keeps a config plain data, so a Ray
worker or a host application's API can carry it. See {doc}`extending`.
