# Changelog

## Unreleased

### Added
- Model inputs for models with several time blocks (#5). Each path is lined
  up with the nearest time block above it and read over that block's dates;
  a path under none is set in every one. A design study's `model_inputs`
  therefore set every horizon from its own year and month;
  `horizon[<year>,<month>].<path>` sets one, and a `*` matches any year or
  month (`horizon[2030,*].<path>`: every 2030 horizon), so each modeled year
  can take its own projection. `data_years`
  (`{2035: 2025}`) reads another year's dates, for modeled years with no data
  yet. Provenance is recorded per time block. The `time_grid` hook may return
  `{scope: grid}`, and `apply_values` fixes each time block's values on its
  own block. The `design_study` example sets the pump's demand from a projection
  per modeled year, and its `sweep_scenarios.py` sizes the plant under
  several demand scenarios (the projection, and two noisy draws of it) with a
  `model_inputs` sweep.
- flex-pse 0.3.1: a design study's `design:` takes `capex_budget`, a cap on
  the total capital cost of what it sizes, and storage units take
  `cyclic` (end the horizon at least as full as it starts). A design
  study's storage also needs `fix_initial_state: false` now; the
  `design_study` example's battery sets it.
- `resample.window` on a model input: `forward` (the default) gives step `t`
  the samples of `[t, t + dt)`, the interval flex-pse means by a step;
  `backward` gives it `(t - dt, t]`, the interval ending at `t`.
- Tutorials (`tutorials/`): marimo notebooks with charts of the data and
  of how it reaches the model, run as scripts by the tests and published, with
  their outputs, in the docs: a first result end to end, model inputs,
  surrogates, studies and sweeps, and design studies.
  The `tutorials` extra installs marimo and Altair.
- Documentation, built with Sphinx (`docs/`) and published to GitHub Pages
  from main: installation, a quick start, explanations of connecting,
  studies, storage, Ray, embedding and extending, and the API reference.
  The build fails on any warning, broken cross-references included.
- Ready to embed in a host application:
  - A long-lived local Acquirium: `flex-orch [--data-dir D] acquirium start
    [--config acquirium.toml] [--port P] | status | stop`
    (`flexorchestrator.service`). It runs `acquirium server` detached on
    loopback, optionally with your own config and its drivers, so it outlives
    every script; while it runs, `Orchestrator(data_dir=)` attaches to it and
    never stops it. `FLEXORCH_START_ACQUIRIUM=0` makes the orchestrator fail
    rather than start a short-lived managed server.
  - `FLEXORCH_ACQUIRIUM_URL` connects `Orchestrator()` and `connect()` to that
    Acquirium (mode 3). While it's set, passing `data_dir`, `db` or another
    `acquirium_url` raises, so the store is chosen in one place.
  - Study handles: `select_study`, `create_study` and `get_or_create_study`
    still select the study, and now return a handle fixed to it that shares the
    connection, so callers in several threads never see each other's selection.
    `orch.user` (or a handle's) sets who runs and stores are recorded as.
  - Spec files as bytes: `store_model_spec(tag, spec, files={path: bytes})`
    stores a spec's files without reading the disk, exactly as the same files
    read from disk (the same `spec_hash`), and `model_spec(tag,
    with_files=True)` returns them (`SpecRecordWithFiles`).
  - `flex-orch verify [--study S ...] [--samples N] [--against FILE]`
    (`flexorchestrator.verify`): per study and kind, run counts, tags and
    hashes of a sample of values, and problems (a tag stored twice, runs left
    running, unreadable values, a study that doesn't exist). `--against` an
    earlier report exits 1 on any difference in succeeded runs, tags or
    values; a copy (an import, a move) matches its original.
  - `python -m flexorchestrator` runs the CLI.
- Design studies, built on flex-pse's `flexdesign`: size equipment once across
  every modeled year and representative month. Add a `design:` section to a study
  config, or store a whole `DesignSpec` as a model spec (format `flex-pse.design`,
  with the files in its `base` and `changes` stored too). Sweeps, Ray, retries and
  resume work as usual, and a study without `design` is unchanged.
  - A design study stores a summary by default, read with
    `orch.design_results(tag)`; export patterns add per-horizon series.
  - The exporter takes the time Sets of several blocks, each with its own
    timestamps.
  - `model_inputs` can't be combined with a design yet (#5).

### Changed
- A design study no longer refuses `model_inputs`.
- The import package is renamed `flexorchestrator`, matching flex-pse's
  `flexcore`, `flexdesign` and the rest: `from flexorchestrator import
  Orchestrator`. The distribution is still `flex-orchestrator`, the CLI still
  `flex-orch`, and the plugin entry-point group is now
  `flexorchestrator.plugins`.
- `select_study`, `create_study` and `get_or_create_study` return a study
  handle instead of the study's name; `.study` on it is the name.
- The memory backend locks every method, and the file backend locks its reads
  too, so both are safe to share between threads.
- The Acquirium backend's stream lookups (`stream_info`, `find_streams`,
  `stream_rows`) use Acquirium's query API instead of SPARQL, so they don't
  depend on its graph layout. Stream values now come in the unit `stream_info`
  reports: the point's unit, converted by Acquirium if the stream is stored in
  another. Only the `sparql` selector still runs SPARQL, the query its user
  writes.
- Acquirium is pinned to `main` at `b7f6fc8` (the `v0.4.0a9` release commit).
- flex-pse is pinned to v0.3.0. Flowsheet specs are now schema `0.1.1`; `0.1.0`
  specs still load, and BatteryModel's `capacity` migrates to `energy_capacity`.
  - Validating normalises a `0.1.0` spec to different JSON, so storing it again
    under an existing tag, or resuming a study from before this bump, can raise
    `TagExistsError` or re-run the task. Use `on_existing: replace` or a new tag.
  - `solve_options` for the flex-pse hook are now `prefer`, `tee`, `warmstart`,
    `mip_gap`, `time_limit` and `options`. Others, such as `load_solutions`,
    give an `error` outcome.
  - An infeasible HiGHS solve is now stored as `failed` rather than `error`, so
    `retries` no longer applies to it.
  - A model with inline `energy_prices` now solves with HiGHS instead of IPOPT.
  - Run metadata still records `flexpse_version` as `0.1.0`, because the v0.3.0
    tag's package metadata says so.

### Fixed
- A model input's `sum` of data coarser than the model's steps (hourly kWh
  on 15-minute steps) splits each sample's amount evenly over the steps it
  covers. It used to put the whole amount on one step and interpolate the
  steps between, overstating the total.
- `[` in an export or result pattern is literal, as in a Pyomo path, so
  `horizon[2030,*].pump.*` matches. Patterns can no longer use `[...]` character
  classes.
- The flex-pse spec validators dump with `by_alias=True`, so a `$source`
  reference is stored as `{"$source": ...}`, not `{"source": ...}`.
- A study run in-process (`max_workers: 1`) whose config names registered
  hooks no longer fails every task with "No hooks are registered".
- Cancelling `execute_study` now also cancels the fits it runs first.
- `create_orchestrator` (and so `flex-orch run`) honours
  `FLEXORCH_RAY_ADDRESS`.
- With `on_error: stop`, two tasks timing out at once no longer raise
  `KeyError`.
- A model key named `time` is refused wherever it appears, not only when it
  is the first key on its time axis.
- A numeric pandas `time_column` raises `StorageError` instead of being read
  as nanoseconds since 1970.
- Deleting the same run id twice in one call deletes it once (both backends).
- Acquirium `has_study` turns connection errors into `StorageError`; `runs()`
  sorts a run with no start time last.
- A `constant_intensity` surrogate with the wrong number of coefficients
  raises `FitError`.
- An export's final folder rename retries briefly on a Windows
  `PermissionError` (a just-written file held by antivirus or the indexer).

### Changed
- `retries` now covers only the model work (build, solve, export, or a dead
  Ray worker) on both paths. A task whose spec can't be prepared or whose
  result can't be stored is an error result at once; in-process this used
  to be retried.
- `fit_surrogate` on Ray finds Ray the same way studies do: the call's
  address, then the fit's, then `Orchestrator(ray_address=)` /
  `FLEXORCH_RAY_ADDRESS`, then local Ray.
- An unknown registered hook name in a study config raises `HookError`
  before the study runs, on both paths.
- Code review: shared helpers replace duplicated code
  (`backend.recording`, `SolveOutcome.from_stored`, `results.ResultReads`,
  `results.layout`, `backend.base.align` / `qudt_key`, task error results),
  and docstrings were filled in or corrected across the package.
- The package root also exports `ConfigureFn`, `StudyConfigError`,
  `FitError`, `CommandError`, `CommandCancelled`, `PluginError` and
  `ArchiveError`.
- A root `README.md`.

### Added
- GitHub Actions CI (`.github/workflows/ci.yml`): `lint`, then `unit` and
  `component` on Linux and Windows and `integration` on Linux, each built
  from `environment-dev.yml`; also weekly, to catch upstream drift.
- Model inputs from stored data: a study's `model_inputs`, a
  case's `model_inputs`, a `model_inputs` sweep, and
  `orch.apply_model_inputs(model, inputs)` on a built model (new
  `apply_values` hook) set model variables from a dataset, a result or
  general-store streams, selected as fit data is. The data is lined up with
  the model's time steps (the new `time_grid` hook) in the facility's
  `timezone`, resampled, gap-filled (`interpolate` by default, `ffill`,
  `zero` or `error`) or reduced to one number, and shifted by `offset`. Each
  result records where its inputs came from (`ResultHandle.model_inputs`,
  kept through export and import). `orch.resolve_model_inputs` returns the
  values without building. Example: `examples/ro_study/model_inputs.py`.
- A `streams` fit selector names general-store streams by `source_id` and
  `ref_name`, as their driver registered them, instead of by reference URI.
- A file backend: `FileBackend(path)` keeps every study in a
  folder on disk, with no Acquirium. It passes the same contract tests as
  the memory and Acquirium backends, and every orchestrator feature works on
  it (fits, sweeps, Ray, export, import). Each value is written as it's
  recorded, with an atomic rename, so a crash loses only the run in
  progress; deletion is real. Storing a result takes about 45 ms against
  about 580 ms on a local Acquirium. `flex-orch --files DIR` selects it, and
  `examples/ro_study/store_on_disk.py` shows it.
- `examples/ro_study/compare_ray.py` times a month-long nested sweep
  in-process and on Ray with half the machine's cores.
- Parallel execution on Ray. With `max_workers > 1`,
  `execute_study` runs its tasks on Ray (`flexorchestrator.parallel`):
  - Workers build, solve and export; the main process stores each result as
    it arrives (a `ray.wait` loop), so workers hold no credentials. Results
    are the same as with `max_workers=1`.
  - A task's spec and files go to Ray once per spec (`ray.put`).
  - The loop enforces `task_timeout_s`, two hours by default (`null` for no
    limit): `ray.cancel(force=True)`, and the task is stored as a failed
    result, `termination: timeout`. It also retries a dead
    worker or a raising build/solve within `retries`, and cancels running
    tasks on `on_error: stop` or a cancelled command.
  - Workers get `OMP/MKL/OPENBLAS_NUM_THREADS=1`; `num_cpus`, `memory` and
    `max_calls` are configurable.
  - Hooks must be importable by name, checked before anything runs.
  - `fit_surrogate` variants also fit on Ray: data is prepared once in the
    main process and shared with `ray.put`.
  - New config fields: `execution.ray_address`, `execution.resources`
    (`num_cpus`, `memory`) and `execution.max_calls`. Also new:
    `FitParams.ray_address`, `Orchestrator(ray_address=)` (default
    `FLEXORCH_RAY_ADDRESS`), `execute_study(ray_address=)` and
    `flex-orch run --ray-address`.
  - Local Ray starts with `namespace="flexorch"` and no dashboard.
  - `TaskResult` reports `seconds` (build, solve, export, store) and the
    worker's `rss_mb`.
  - The task is now three steps, `prepare` / `execute` / `finish`, in
    `study.task`.
  - `deploy/ray/` has per-tenant Ray cluster templates (compose and KubeRay)
    and the opt-in benchmark `study/tests/test_benchmark_ray.py`.
  - New optional dependency extra `ray`.
- Result runs record the hooks a task actually used (`flexorch_hooks`),
  including a study's build hook.
- Study export and import: `flexorchestrator.archive` with
  `export_study` / `export_result` (a `flexorch-export/1` folder of JSON
  files, written to a temporary folder and renamed into place, with
  `checksums.sha256` and optional gzip), `load_result` (no database) and
  `import_study` (checks schemas and checksums, re-creates the study through
  the store paths, keeps tags, spec hashes and hooks, records
  `flexorch_imported_from`). `Orchestrator.export_study`, `.export_result`,
  `.import_study`. JSON Schemas in `docs/schemas/`
  (`python -m flexorchestrator.schemas`). New `ArchiveError`.
- Commands, plugins and events: `flexorchestrator.ext` with
  `@command`, `Command`, `CommandRegistry`, `Progress`, `Plugin`
  (`PLUGIN_API_VERSION` 1.0.0, the `flexorchestrator.plugins` entry-point
  group, the `FLEXORCH_PLUGINS` allowlist, collision checks),
  `create_orchestrator`, and lifecycle events. `Orchestrator(plugins=...)`,
  `.run_command`, `.register_command`, `.on`, and the stable methods
  `build_hooks`, `default_export_selection`, `before_store`, `after_store`,
  `on_task_result`, `on_study_created`. Every command call is recorded in
  `flexorch::commands`. Testing helpers `memory_orchestrator` and
  `run_command_in_test`. New `CommandError`, `CommandCancelled`,
  `PluginError`.
- Regression: the built-in `fit_surrogate` command
  (`Orchestrator.fit_surrogate`, `flexorchestrator.fitting`): dataset,
  other-study, result, `ref_uris`, `resolve` and read-only `sparql`
  selectors; alignment, resampling and gap handling; variants; holdout
  validation; flex-pse's `multilinear` and `constant_intensity` regressors or
  any class by import string. Each fit stores its surrogate spec and a fit run
  in `<s>::fits` with the training data's provenance and `data_hash`. The
  Acquirium backend reads general-store streams (`stream_rows`,
  `sparql_select`, `stream_info`, `find_streams`). flex-pse hooks
  `get_regressor` and `predict_surrogate`; `export.units.qudt_to_pyomo` and
  `pyomo_unit_of`. New `FitError`.
- Declarative studies: `flexorchestrator.study` with
  `StudyConfig` (fits; `surrogate`, `variable`, `configuration_option`,
  `spec_list` and `cases` sweeps, combined as a product or zipped;
  `output_name`, `on_existing`, `export`, `execution`), `expand`,
  `TaskSpec` / `TaskResult` / `run_task`, and the built-in `execute_study`
  command (`Orchestrator.execute_study`, with `dry_run`). New `StudyConfigError`.
  `output_name` fields take Python format specs (`{price:.2f}`); values
  are otherwise written exactly, with `e+` written as `e`.
- A `configure` hook slot and its flex-pse default
  `flexorchestrator.flexpse.configure(spec, path, value)`: sets a unit's
  construction option or another element's field, named by the element's
  path and the option (dict options whole or one key at a time). The
  flex-pse build hook also edits `costing` element fields named by an
  override.
- The `flex-orch` CLI: `run`, `cmd` (options from the command's params
  model), `commands`, `export` and `import`. New dependency `pyyaml`.
- `examples/ro_study/`: the `ro_surrogates` model as study configs, one
  script per sweep (surrogates, variables, configuration options, nested
  with a full-model export,
  cases), and the sample plugin `examples/plugins/ro_tools/`.
- User-replaceable build and solve hooks: `BuildFn`, `SolveFn`,
  `ApplySurrogateFn` and `SwapSurrogateFn` protocols, `SolveOutcome`, `HookSet`,
  `HookRegistry` (callables, registered names and import strings), and
  `run_solve`.
- Default flex-pse hooks in `flexorchestrator.flexpse`, targeting flex-pse
  v0.2.0 (flowsheet spec schema 0.1.0): `build` (overrides become `fix`
  elements), `solve` (honours the spec's `solver`), `apply_surrogate` and
  `swap_surrogate` (`surrogate` elements, optional `relation`).
- Specs as data: `flexorchestrator.specs` with `to_json_spec`,
  canonical `spec_hash` / `run_hash`, `detect_format`, `validate_spec`
  (normalises flex-pse specs through their validators), `check_tag` and
  `SpecRecord`; flex-pse validators in `flexorchestrator.flexpse`.
- Model data exporter: `flexorchestrator.export` with
  `export_model_data` (identity-based time detection, time / time x custom /
  custom / scalar classification, `ModelData` / `ExportedSeries`), and
  `UnitMapper`, which generates QUDT keys from pint's unit parts
  (`L/day` -> `L-PER-DAY`) and verifies them through an optional resolver,
  plus `conversion_factor`. New `ExportError` and `UnitError`; new
  `pyomo` extra.
- A `time_sets` hook kind (`TimeSetsFn`); the flex-pse default
  (`flexorchestrator.flexpse.time_sets`) registers the TimeBlock's time
  set plus the `startup` / `shutdown` index sets, so those export as
  timeseries.
- Explicit results API: `Orchestrator` with `create_study` /
  `select_study` / `get_or_create_study` (with guards),
  `store_model_results(tag, model, outcome, keys, include, exclude)`,
  `solve_and_store(tag, model, ...)` and `model_results(tag)`, returning
  `StoredResult`. A tag is stored once; a result whose solve wasn't `ok` is a
  `failed` run holding only its outcome, so the tag can be retried.
- Storage backends: the `StorageBackend` contract,
  `MemoryBackend` and `AcquiriumBackend`, and the results writer
  (`flexorchestrator.results`): only verified QUDT units are declared, the
  Pyomo unit, component and index go in variable metadata, and later runs are
  converted to a label's declared unit. New `StorageError`,
  `StudyExistsError`, `StudyNotFoundError`, `TagExistsError` and
  `TagNotFoundError`; new `acquirium` extra.
- Data API: `Orchestrator.store_data(name, df, units=,
  time_column=)` and `get_data(name, columns=, start=, end=, column=,
  frame=)`; one dataset is one run of `<s>::data`, one timeseries per column.
  Units given as text (`m3/day`) or QUDT keys. New `AmbiguousColumnError`,
  `StoredData`, the `frame=` default on `Orchestrator`, and
  `flexorchestrator.frames` (polars/pandas in and out). polars and pandas
  are now dependencies.
- `Orchestrator.store_model_data(tag, data, outcome)` stores values that are
  already exported.
- Results are stored as a snapshot: a `model_values` table
  (Parquet, through Acquirium's `table` kind) and a `model_manifest` JSON.
  `streams=` on `store_model_results` / `store_model_data` /
  `solve_and_store` also stores chosen keys as native variables, and
  `model_results(tag, units=)` converts with pint. `StoredResult.streams`.
- Storage contract: the `table` kind; a timeseries sample must have a value
  (missing values are left out). The Acquirium backend declares units as QUDT
  URIs.
- Acquirium pin moved to PR #78 `9a70a1f`.
- `Orchestrator.acquirium`: the raw Acquirium client; there
  are no query wrappers.
- Result handles: `Orchestrator.result(tag)` and
  `stored_model_results(tags=)` return `ResultHandle`s with `to_frame()`,
  `to_polars()`, `to_pandas()`, `data(units=)`, `scalars()`, `units` and
  `outcome`, read lazily from the snapshot.
- Deletion and uniqueness: `Orchestrator.delete(kind, tag)`,
  `delete_study(name)`, `replace=True` on `store_model_results`,
  `store_model_data`, `solve_and_store` and `store_data` (writes first, then
  deletes the old item), `allow_delete=`, a per-study lock, and a post-write
  check that removes a racing duplicate. Storage contract: `delete_runs` and
  `delete_study`, using Acquirium PR #78's deletion.
- Storage contract: `variables(study)` and `records(..., labels=)`.
- Spec storage: `store_model_spec` (with `parent=`,
  `base_dir=`, `assets=`), `store_surrogate_spec`, `model_spec`,
  `surrogate_spec`, `stored_specs`, `apply_surrogate`, `materialize_spec` and
  `build(tag, **overrides)`, in `<s>::specs`. Files a spec references (flex-pse
  tariff and events files, `$source` data, or explicit JSON pointers) are
  stored with it (`flexorchestrator.assets`: `AssetRef`, `find_assets`,
  `flexpse_assets`, `collect_assets`, `materialize_spec`), and
  `SpecRecord.assets` / `record_hash` make the hash cover them. Results record
  `model_spec_ref` and `overrides` inputs and `spec_tag` / `spec_hash` /
  `run_hash` metadata; `ResultHandle.spec_tag`, `.overrides` and `.spec()`.
- Storage contract: the `file` kind (`FileValue`) and `studies()`;
  `Orchestrator.studies()` and `register_hooks()`.
- `examples/ro_surrogates/`: one short script that stores synthetic RO data,
  emits and stores a tariff-priced flex-pse model's spec, fits an average
  energy intensity and a linear power surrogate, and compares the two; run as
  an integration test (`test_examples.py`). An opt-in real-Postgres preflight
  test (`FLEXORCH_TEST_PG`).
- Connection modes: `flexorchestrator.connection` with
  `connect()`, `Connection`, `DBCredentials`, `credentials_from_env`,
  `postgres_dsn`, `redact` and `preflight`. `Orchestrator()` connects by itself
  (local DuckDB in `.flexorch`, `db=` + `data_dir=` for Postgres, or
  `acquirium_url=`), with `orch.connection`, `close()` and `with` support.
  Postgres logins come from `FLEXORCH_DB_PASSWORD_FILE`,
  `FLEXORCH_DB_USER` / `FLEXORCH_DB_PASSWORD` or libpq, never from code; the
  password reaches the server only as `PGPASSWORD` in its environment. New
  `DBAuthError`.
- Run provenance: run metadata records the flex-pse and
  Acquirium versions, the user, the hooks (results) and the redacted
  connection.
- An opt-in `benchmark` tier (`pytest -m benchmark -s`) with the storage
  benchmark.
- The flex-pse `time_sets` hook returns `[]` for a model without exactly one
  TimeBlock, so user models store through the default hooks.
- `flexorchestrator.testing`: fake hooks for tests without a solver, and the
  shared flex-pse specs tests build models from: `DEMO_SPEC`, plus
  `LOGIC_SPEC`, `SURROGATE_DISPATCH_SPEC` and `NETWORK_SPEC` emitted from
  flex-pse's round-trip corpus (repo-wide `demo_spec` / `demo_model`
  fixtures).
- Project scaffold: `pyproject.toml`, black/ruff/import-linter config and
  pre-commit hooks matching flex-pse, pytest tier markers.
