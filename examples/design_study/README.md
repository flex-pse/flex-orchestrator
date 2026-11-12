# Design study

Sizes a battery and solar for one plant across two modeled years and two
representative months, at three solar prices, and stores the results on disk.

    python examples/design_study/example.py

- `flowsheet.json` is an ordinary flowsheet. Its battery is cyclic and starts
  from a free charge (`cyclic: true`, `fix_initial_state: false`): a design
  study refuses fixed initial states.
- `study.yaml` adds a `design:` section. Without it, the same config runs as an
  ordinary study.
- The pump's demand comes from stored data (`model_inputs`). `example.py`
  projects a year of meter data (2025) onto each modeled year, 10% more every
  five years, and stores one dataset per year: `projection_2030` and
  `projection_2035`. In `study.yaml`, the path `horizon[2030,*].pump...` sets
  the demand in every 2030 horizon from `projection_2030`, each from its own
  month, and `horizon[2035,*].pump...` does the same for 2035. Each result
  records which dataset and window each horizon read.
- The other ways to name horizons and supply data, such as reusing the 2025
  data as it is with `data_years`, are in the docs: Studies from a config >
  Model inputs in a design study.
- `sweep_scenarios.py` sizes the plant under three demand scenarios:
  `growth` (the projection above), and `noisy_1` and `noisy_2`, the projection
  with random hourly and daily swings, a draw each. Each scenario is a dataset
  per modeled year; `scenarios.yaml` sweeps over them with a `model_inputs`
  sweep, one task per scenario. It then rebuilds the `growth` model from its
  stored result (`orch.build(result.spec_tag, **result.overrides)`) and
  solves it again, to read any variable of the Pyomo model.

      python examples/design_study/sweep_scenarios.py

- By default a design study stores only its summary, which
  `orch.design_results(tag)` reads. Export patterns like
  `horizon[2030,7].battery.charge` add per-horizon series.
