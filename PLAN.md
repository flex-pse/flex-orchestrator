# flex-orchestrator — Development Plan

Goal: one Python tool that connects **flex-pse** (or a user's own model code) with **Acquirium**, which stores studies,
data, specs, model results and units. It should support four ways of working:

1. **Interactive scripts.** Store data, specs and results while you work.
2. **Coming back later.** Reopen a study, query results as polars or pandas, rebuild a stored model and re-solve it.
3. **Config-driven studies.** One JSON/YAML file describes a sweep, and `execute_study` runs all of it, in parallel
   with Ray.
4. **HTTP service.** A FastAPI server lets a UI and other tools submit studies and fetch results as JSON.

**Development is split into two phases:**
- **Phase 1, the core library (M0–M6).** Use cases 1–3 as a Python library, plus a deployable single-tenant stack with
  Postgres backups. There is no HTTP in this phase.
- **Phase 2, HTTP and integrations (M7–M8).** HTTP routes, token auth, and integration with the multi-user control
  plane and UI.

The core library must never import the server code, so Phase 2 is a thin layer added on top.

**Deployment model (D14):** fewer than 100 users, organised into **tenants (groups/projects)**. **Each tenant gets its
own isolated stack**:
- an orchestrator and an Acquirium;
- its own database on a shared Postgres cluster;
- its own data volume, network and Ray.

Users and groups must never be able to reach another tenant's data, configs or compute.

This document is the plan only. No orchestrator code has been written yet.

---

## 1. Starting point (what exists upstream)

| Piece | Where | Status / notes |
|---|---|---|
| Study / experiment ledger | Acquirium `main` (`227e3f6`, PR #78 merged; was `6335f52` on its branch): `ac.study.define/get/all`, `study.input/output/log(...)`, `.json() .scalar(unit=) .text() .file() .table() .timeseries(unit=, observed=, keys=)`, `study.start()/finish()/fail()`, `study.experiments.where(status=, **metadata)` | No release has it yet, so we pin to the commit. Runs are append-only and can't be changed. Works on DuckDB (local) or Timescale/Postgres. |
| Variable declarations | `ExperimentStore.declare` (`Experiments.py:204`) | Within a study, a label's role, kind and metadata (**including unit**) are fixed. Re-declaring it with a different shape raises an error. |
| Study creation | `ExperimentStore.define` (`Experiments.py:193`) | **Silently idempotent by name**: it returns the existing study instead of failing. The orchestrator has to guard against duplicates itself. |
| Deletion | — | **There is no way to delete a study, run or observation.** `Manager.delete_timeseries` (`manager.py:809`) deliberately raises an error. The only reset is `recreate=True`, which wipes the whole database. |
| Graph metadata | `ac.insert_metadata(uri, values)` | Stored **only in Oxigraph**, not in Postgres (see the state table below). So it is *not* used for anything that must survive a Postgres-only restore. |
| Querying | `ac.query()` / `ac.explore()` builder, `ac.attr.*` expressions, `find_entity`, `find_all_data`, `sparql`, `Q.data()/.dataframe()`, `study.experiments.where(...).frame()` | Returns **polars**. |
| Units | QUDT ontology bundled; `client.resolve_unit("gal/min")` (`client.py:801`), `/resolve_conversion`, `/conversion_factors` | Turns free-form unit strings into QUDT units. |
| FlexPSE ↔ Acquirium example | `acquirium/examples/flexpse/record_experiment.py` | Reference pattern (it reads values by hand with `pyo.value` and `pyo.units.convert`). |
| Model spec | flex-pse 0.2 flowsheet spec: `FlowsheetSpec` (schema `0.1.0`), a flat list of `elements` whose `name` is the component's model path; `flexcore.config.io.load_spec` | The default spec format. Older `ModelConfig` documents (schema `0.0.x`) are not used. |
| Build / solve | `flexops.build_model(spec)` (expands arcs itself); `flexcore.solvers.get_solver(model=m, prefer=spec.solver).solve(m)`; `flexops.apply_spec(m, elements)` applies `fix` / `unfix` / `dispatch` / `surrogate` elements to a live model | These are the **defaults only**. Users can supply their own (D1). |
| Surrogate spec | `SurrogateSpec`, and the `surrogate` element (`unit`, `relation`, `surrogate_type`, `data`) | In 0.2 **two types work end to end**: `multilinear` (`MultilinearSurrogate` replaces the relation) and `constant_intensity` (no class; the build fixes the unit's intensity parameter). `quadratic`, `exponential`, `arima` and `neural_network` are reserved stubs that raise `NotImplementedError`. |
| Regressors | `flexparameterize.regression`: `LinearRegressor().fit(X, y, input_units=, output_units=)` gives `multilinear`; `ConstantIntensityRegressor().fit(X, y).to_surrogate_spec(input_units=, output_units=)` gives `constant_intensity`; `get_regressor(SurrogateType)`; `to_fit_result() -> FitResult` (coefficients, r2/rmse, n_samples, data window) | Exist in 0.2. Neither has `predict`. The ARIMA regressor is only on the `arima_integration` branch (upstream item U8). |
| `emit_model(m)` (built model → spec) | `flexops.emit_model` | Exists in 0.2 (was upstream item U1). Not needed by D2, but usable for provenance. |
| Model data → export | only `FlexCosting.report_cost() -> CostReport` | **Missing.** The orchestrator builds its own exporter (D3). |
| Multi-tenancy / auth | — | **Acquirium has neither.** Its client takes only `server_url`, `server_port` and `use_ssl` (`Client/acquirium.py:49`). There is one server process per data store. Tenancy has to be provided **around** Acquirium (D14). |
| Database credentials | `pg_dsn` in `[server]` of `acquirium.toml`, or the `PG_DSN` env var (`Server/config.py:43`, `manager.py:126`). Passed unchanged to `psycopg.connect(dsn)` (`timescale_store.py:67`). | The credentials belong to the **Acquirium server process**. HTTP clients never see them. |

### Where Acquirium keeps its state (this decides what a backup must cover)

| Data | Where it lives with the Postgres backend | Covered by a Postgres backup? |
|---|---|---|
| Timeseries values, stream table, logs | Postgres | ✅ |
| Study, variable, run and observation tables | Postgres (same tables as on DuckDB) | ✅ |
| Experiment metadata in the graph | Oxigraph in `data_dir` | ⚠️ can be rebuilt from SQL (`sync_metadata_graph`, `Experiments.py:129`) |
| Plant models, `insert_metadata`, stream/point descriptions | **Oxigraph only** | ❌ needs a `data_dir` snapshot or `/export_graph` |
| Experiment file uploads | **`<data_dir>/experiment_artifacts`** (`manager.py:159`) | ❌ needs a `data_dir` snapshot |

**A full backup of a tenant means its Postgres database plus a snapshot of its Acquirium `data_dir`, taken
together** (D14).

### How Acquirium handles database credentials (checked in `a74b3bc`)
- **Two backends.**
  - `timeseries_backend = "duckdb"` (the default) is a local file and needs no credentials.
  - `"timescale"` is PostgreSQL with the TimescaleDB extension. It needs a libpq DSN.
- **Only the server connects to Postgres.** `Manager` reads `pg_dsn` from the config, or else the `PG_DSN` env var.
- **An environment variable overrides the config file.** `LoadedConfig.apply_server_env` uses `setdefault`, so a
  `PG_DSN` in the environment wins over `pg_dsn` in the toml.
  - `aq.init()` removes inherited `ACQUIRIUM_*` env vars but **passes `PG_DSN` through** (`runtime.py:198-205`).
  - libpq also reads `PGPASSWORD` and `~/.pgpass`.
- **Privileges needed by the database role** (`timescale_store.py:72-219`):
  - `CREATE EXTENSION IF NOT EXISTS timescaledb`, unless an admin has already created it;
  - `CREATE TABLE`/`VIEW`, `create_hypertable`, `ALTER TABLE`;
  - `DROP ... CASCADE` when `recreate=true`.
- **One server per database/graph pair.** Oxigraph is single-process. A second server on the same database with a
  different `data_dir` would come out inconsistent.
- **Where credentials could leak today:** the compose files hard-code `acquirium/acquirium`, and
  `acquirium.docker.toml` puts the password in the DSN. The store logs only the host (`Storage/__init__.py:42`).
  `server.log` and error messages still need checking (U5).

### How the pseudocode maps to what exists
- `or` is a reserved word in Python, so the examples use `orch`.
- The pseudocode's `ArimaSurrogate` loop has no 0.2 equivalent yet (U8). Until then the same workflow is planned and
  tested with `multilinear` surrogates. The orchestrator does not wrap flex-pse fitters (`LinearRegressor().fit(...)`).
  Users bring any object that
  produces a spec.
- A surrogate goes in as a `surrogate` element naming the unit and the relation. The default relation is
  `"power_electrical_relation"`; the digester's is `"biogas_relation"`, so it must be named.
- `store_model_results(tag)` has no model argument, so the model is passed explicitly (D4).
- `get_data('biogas_volume')` reads one column, but the data was stored as the dataset `'biog_gas_data'`
  (D6 covers lookup by column).
- `Orchestrator(db, username, password)`: in the library, the database credentials come from the environment or a
  secret file, not from arguments typed into scripts (D11). User login is handled by the control plane and tokens in
  Phase 2 (D15).

---

## 2. Key design decisions

### Phase 1: core library

**D1 — Users can bring their own build and solve functions.** The orchestrator manages storage and runs. It never
requires flex-pse to build or solve. Two small protocols in `flex_orchestrator.hooks`:

```python
class BuildFn(Protocol):
    def __call__(self, spec: dict, **overrides) -> Any: ...            # returns a Pyomo model (or anything ExportFn understands)

class SolveFn(Protocol):
    def __call__(self, model: Any, **options) -> SolveOutcome: ...     # SolveOutcome(ok, status, termination, message, wall_time, log)
```

- Optional hooks: `apply_surrogate(spec, unit, surrogate_spec, relation=None) -> spec` (edits the spec) and
  `swap_surrogate(model, unit, surrogate_spec, relation=None)` (edits a live model). `relation=None` means the hook's
  default.
- Optional hook `time_sets(model) -> [Set, ...]`: the model's time Sets, main one first, for the exporter (D3).
  The flex-pse default adds the partial-horizon Sets flex-pse logic builds.
- Optional hook `configure(spec, path, value) -> spec`: sets one configuration option in a copy of a spec, for
  `configuration_option` sweeps (**built**). The flex-pse default reads `path` as an element's name (the longest
  leading part that names one, so any unit or block works) followed by the option: a unit's
  `construction_options` entry, or another element's own field (`"costing.energy_prices.electrical"`). A dict
  option is set whole by a dict value, or one key at a time by a longer path (`"plant.feed.max_withdrawal.value"`);
  missing dicts on the way are created.
- The defaults live in `flex_orchestrator.flexpse` and target flex-pse 0.2 (**built**):
  - `build` adds each override as a flex-pse `fix` element (`"override:<path>"`), then calls `flexops.build_model`.
    An override path is a variable path from the model root (`"demo.pump.flow_in"`, `"...flow_in[3]"`, `"...[*]"`),
    and its value is a number or `{"value", "units"}`. Price sweeps edit the `costing` element instead: a path whose
    first part names a `costing` element and whose next part is one of its fields
    (`"costing.energy_prices.electrical"`) sets that field (**built**, M4).
  - `solve` calls `get_solver` (preferring the spec's `solver`) and maps the termination to `SolveOutcome`.
  - `apply_surrogate` adds a `surrogate` element, replacing one for the same unit and relation;
    `swap_surrogate` applies the same element to a live model with `flexops.apply_spec`.
- Hooks can be passed in three ways:
  1. As callables to `execute_study(cfg, build=my_build, solve=my_solve)` or `Orchestrator(..., build=, solve=)`.
  2. Registered by name with `orch.register_hooks("my_model", build=..., solve=...)` (**built**).
  3. As **import strings** in a study config, `build: "mypkg.models:build"`. Ray workers need this form (D13), and so
     does the Phase 2 HTTP API.
- A user who writes their own `ExportFn` can store models that aren't Pyomo at all.

**D2 — Specs are stored as data.**
- A model spec is any JSON-serialisable dict. For flex-pse it is a flowsheet spec (`FlowsheetSpec`, schema `0.1.0`)
  as JSON, validated when flex-pse is installed.
- A surrogate spec is any JSON dict. For flex-pse it is a `SurrogateSpec` dump.
- Both are stored as `.json()` input observations. A surrogate swap is a spec transformation
  (`apply_surrogate`) followed by `build`, so stored specs replay without needing `emit_model`.
- **Built** in `flex_orchestrator.specs` (no pyomo or flex-pse imports). Writing to Acquirium comes with the M1
  backend:
  - `to_json_spec` turns a dict or pydantic model into plain JSON and rejects anything JSON can't hold, including
    NaN and infinity.
  - **Normalisation:** a spec whose format has a validator is stored in its validated form, with defaults written
    out. For flex-pse that means `load_spec(...).model_dump(mode="json")` and `SurrogateSpec...model_dump`. So a dict
    and the equivalent pydantic model store, and hash, the same JSON, and a later change to a flex-pse default can't
    change what an old spec means. Without flex-pse installed, specs are stored as given.
  - `spec_hash` is the sha256 of canonical JSON: sorted keys, no whitespace, and integral floats written as ints, so
    `1` and `1.0` hash the same. The canonical form is **frozen** (a pinned-hash test guards it), because stored hashes
    drive resume. `run_hash(spec_hash, overrides)` identifies one run.
  - `spec_format` is detected from the shape: `flex-pse.flowsheet/<schema_version>`, `flex-pse.surrogate`, or `json`.
    Users can name their own format. Validators are registered per format family, and run only when that library is
    installed.
  - `SpecRecord(kind, tag, spec, spec_format, spec_hash, parent_tag)` is what a store call writes. Its `metadata()`
    gives the D10 run metadata, and `verify()` re-checks the hash when a spec is read back (D17 import). A flowsheet
    can't be stored as a surrogate spec, or the other way round.
  - **Tags** are 1–128 characters from `[A-Za-z0-9_.-]`, starting with a letter or digit, because they become
    Acquirium labels and the D17 export's file names.
  - Rendering templates such as `output_name` belongs to `StudyConfig` (M4), not `specs.py`.
- **Referenced files (built, M1).** A spec can point at files it doesn't embed, so the stored spec alone can't
  rebuild the model. In a flex-pse flowsheet these are the costing element's `tariff_source` (a path, a list, or a
  `{name: path}` mapping) and `dr.events_source`, plus any `{"$source": "<path>"}` value: price values, `dispatch`
  values and `fix` values. flex-pse resolves them relative to the spec file's directory, or the working directory
  for a dict spec, which on Ray is the worker's. The plan:
  - **Find them.** A per-format finder returns each reference as a JSON pointer plus the path. Generic `json` specs
    name theirs explicitly, as JSON pointers to path strings (`store_model_spec(..., assets=["/site_file"])`).
  - **Store them with the spec.** `store_model_spec(tag, spec, base_dir=...)` reads each file and records it in
    `SpecRecord.assets` as `{pointer: {name, sha256, size}}`. It stores the bytes once per hash as `.file()` input
    observations (Acquirium artifacts), and stores the spec with each path rewritten to the asset's stable name
    (`assets/<sha256[:12]>-<filename>`). A missing file is an error, never a silent skip.
  - **Hash what the model depends on.** `spec_hash` then covers the spec *and* the asset hashes. Otherwise a run
    with an edited tariff file and an unchanged spec would look identical, and resume (D9) would wrongly skip it.
    For specs without assets the hash stays exactly as now (the pinned-hash test still holds).
  - **Replay.** Before a build, `materialize_spec(record, dir)` writes the assets to a directory, on the worker for
    Ray (D13), and points the spec's paths at them. The build hook never sees stored names.
  - **Export.** D17 study exports include `specs/assets/<name>` files and list them in `checksums.sha256`, so an
    exported study rebuilds offline.
  - This is D2 work, not D3 (the exporter reads built models, not specs). It lands with spec storage in M1, because
    it needs Acquirium's file artifacts; the discovery hook and hashing can be built before that.

**Built** (M1) in `flex_orchestrator.assets`, `spec_store` and `Orchestrator`:
- `store_model_spec(tag, spec, *, parent=None, base_dir=None, assets=(), spec_format=None, replace=False)` and
  `store_surrogate_spec(tag, spec, *, parent=None, ...)` return the `SpecRecord`. `model_spec(tag)`,
  `surrogate_spec(tag)` and `stored_specs(kind)` read records back and check their hashes. `delete("model_spec",
  tag)` and `replace=True` follow D9. A taken tag is refused before any file is read.
- **Finding files:** `flexpse_assets` reads only the JSON, so it needs no flex-pse. It finds `tariff_source`
  entries ending in `.json` / `.csv` (any other string is a historian tag), `dr.events_source`, and every
  `{"$source": path}`.
- **`SpecRecord.assets`** is `{pointer: AssetRef(name, sha256, size)}`. `record_hash` is `spec_hash(spec)` without
  assets (the pinned hash holds) and `spec_hash({"spec", "assets": {pointer: sha256}})` with them.
- **Derived specs.** `orch.apply_surrogate(base, unit=, surrogate=, relation=)` applies a stored surrogate to a
  stored spec through the hook and returns the spec. `store_model_spec(..., parent=base)` reuses the parent's
  stored files when the spec names them by their stored names. Without `parent=`, such a name is an error that
  says so.
- **Building.** `orch.build(tag, **overrides)` materializes the files into a temporary directory, calls the build
  hook, and marks the model with `{tag, spec_hash, overrides}`. `orch.materialize_spec(tag, dir)` does the same for
  a directory of your choice (Ray, D13).
- **Normalisation fix.** The flex-pse validators dump with `by_alias=True`. Without it, `{"$source": ...}` was
  stored as `{"source": ...}`: flex-pse still loads that, but it isn't the form flex-pse writes, and asset discovery
  couldn't see it.
- **Tested** on both backends: a spec with a tariff file, rebuilt from another working directory, gives the same
  model, and editing the tariff changes `spec_hash` and the model; `$source` price files; surrogate specs and
  derived specs; the kinds kept apart; results linked to their specs.

**D3 — The orchestrator's own model data exporter, with units.** `export_model_data(model, keys=None, include=None,
exclude=None, time_sets=None, datetime_index=None)` returns a `ModelData` object: a list of `ExportedSeries(key, kind,
index, values, unit_pyomo, unit_qudt, component, raw_index)`. It works in four steps:

1. **Walk the model.**
   - Start from the requested `keys` (exact component paths, e.g. `"costing.aggregate_electrical_power"`), or match
     `include`/`exclude` glob patterns over component names. The default is every active `Var`, `Expression` and
     mutable `Param`.
   - Use `component_data_objects` with stable `name` paths, relative to the model root.
2. **Classify each component by checking which Set it is indexed by.**
   - **Finding the time set.** In flex-pse, `time_block.time_index` is an ordered integer `pyo.Set` holding `0..N-1`.
     The timestamps live separately in `time_block.datetime_index`, a pandas `DatetimeIndex` (`time_block.py:255-283`).
     Because the time set is just integers, the check **must compare the Set object itself, not its values**: any
     other integer Set of the same length would match by value.
     - The time set is found by locating the model's `TimeBlock`, or given as `time_sets=[...]` together with a
       `datetime_index=` mapping for models that don't come from flex-pse.
     - A component counts as time-indexed only if its `index_set()` **is** a registered time set, or one of the
       factors in `index_set().subsets()` is.
     - Time positions `i` become timestamps through `datetime_index[i]`.
     - A declared subset of time, such as a set of all points but the last, is treated as time only if it was added to
       `time_sets` explicitly. Otherwise it falls under the custom-index rule below.
   - **Classification:**
     - **Indexed by time only** → one **timeseries**, with key = the component path
       (e.g. `plant.digester.biogas_volume`).
     - **Indexed by time × a custom set** (e.g. `(t, unit)`) → one **timeseries per custom index value**, e.g.
       `costing.unit_power[pump1]`. Only the time position is turned into timestamps.
     - **Not indexed** → a **scalar**, with key = the component path.
     - **Indexed by a custom set only** → **one scalar per index value**, keyed by the component path plus the index,
       e.g. `costing.capital_cost[pump1]`, `unit.coeff[1,2]`, `tariff.rate[peak]`.
       - Keys are formatted from Pyomo's own `ComponentData.name`, so they match `model.pprint()`.
       - The parent component and the raw index are stored in the variable metadata
         (`{component: "costing.capital_cost", index: ["pump1"]}`), so Acquirium's `study.variables.where(component=...)` (through `orch.acquirium`) finds all
         elements of one component.
   - **Limits and edge cases:**
     - Index values that aren't strings or numbers are converted with `str()`. If two of them produce the same key,
       the export raises an error.
     - `max_scalars_per_component` (default 10,000): above that limit it warns and skips the component, unless that
       component was named explicitly in `keys`.
     - A component with no value (stale or uninitialised) is recorded as `None` with a `stale` flag, not left out.
3. **Pull out the units.** `pyo.units.get_units(element)` gives the Pyomo unit; it is read from a data element,
   not the component, because a Reference (flex-pse's `flow_in`) reports `dimensionless` for itself. A `UnitMapper`
   translates it to a QUDT key. **Keys are generated, not looked up in a table of combinations**, so any unit
   built from known parts works (`L/day` → `L-PER-DAY` needs no entry):
   - user overrides (`unit_map={...}`), by exact string, so the user always wins;
   - otherwise pint splits the unit into parts `(prefix, base unit, exponent)` (`kWh` → `kilo` + `watt_hour`), and
     the key is composed by QUDT's naming rule: prefix + base symbol, the exponent as a digit, numerator parts
     joined by `-`, then `-PER-` and the denominator (`J/(kg*K)` → `J-PER-KiloGM-K`). Only **base units** need a QUDT
     symbol: `BASE_UNITS` lists about 25 (`liter` → `L`, `hour` → `HR`, …), a currency `XYZ` becomes `CCY_XYZ`, and
     any other base is looked up through the resolver (`psi` → `PSI`, `hp` → `HP`);
   - with a resolver (Acquirium's `resolve_unit`, always passed by the Acquirium backend), every generated key is
     **verified**: it must exist in QUDT with the same SI multiplier as the Pyomo unit, to 1 ppm, because QUDT stores
     some multipliers to about 7 figures. Other orderings of the parts are tried if QUDT spells the unit
     differently. QUDT text matching alone is never trusted: it resolves Pyomo's `a` (a year) to `ARE` (an area);
   - without a resolver, keys are generated but marked unverified (`ExportedSeries.unit_verified`).
   Results are cached for each session. If a unit can't be resolved, strict mode raises an error. Otherwise the value
   is stored with `unit=None` and the raw Pyomo unit is kept in the variable metadata, with a warning.
4. **Store in Acquirium** (D5): the whole export as one snapshot per run, and the keys a user asks for also as
   native variables.
   - snapshot → `model_values` (`.table()`: a UTC `time` column and one Float64 column per timeseries key) plus
     `model_manifest` (`.json()`: every key's kind, component, index, Pyomo and QUDT units, the scalars' values, and
     the rows a subset-of-time series covers);
   - native (`streams=`) → `study.output(key).timeseries(unit=qudt_uri, pyomo_unit=..., component=..., index=...)`
     or `.scalar(...)`;
   - **unit consistency (native only):** a label's unit can't change once declared. If a later run produces the
     same key in a different unit with the same dimensions, the values are converted to the unit already declared.
     If the dimensions don't match, it raises an error. Snapshots are self-describing and keep the exported unit;
     reads convert with pint on request.

**Built** (steps 1–3) in `flex_orchestrator.export`, which depends only on pyomo (an import-linter rule). Step 4
is built with D4/D5 in `flex_orchestrator.results`. What the build settled:
- **Signature:** `export_model_data(model, keys=None, include=None, exclude=None, time_sets=None,
  datetime_index=None, unit_mapper=None, max_scalars_per_component=10_000)`.
- **Selection:** `keys` alone exports only those keys, and `keys` plus `include` gives the union. A key can name
  one element (`"capital_cost[p2]"`). The default walk skips private (`_`-prefixed) names, immutable Params and
  anything in a deactivated block, such as a swapped-out relation. References like `flow_in` are exported under
  their own names, even though they duplicate the underlying state-block variable.
- **Time:** the TimeBlock is found by duck typing (a block with a `time_index` Set and a `datetime_index`), so
  the walker doesn't import flex-pse. Several TimeBlocks are an error unless `time_sets=` is given. Extra
  `time_sets` entries must be subsets of the first; their points take the main set's timestamps. With no
  `datetime_index`, the datetimes come from the block that holds the main time set, or else the raw time values
  are used. **flex-pse's own subsets of time**: `add_startup_shutdown` indexes `startup` / `shutdown` over `t >= 1`
  through an anonymous Set with no link to `time_index`, so by identity they'd export as one scalar per point. The
  default `time_sets` hook (a new D1 hook kind, `flex_orchestrator.flexpse:time_sets`) registers the TimeBlock's
  `time_index` plus those index Sets, found on the units themselves and never by value, so they export as
  timeseries over every point but the first (upstream item U10). If the time position in a product index can't be
  located (a factor of unknown width before it, or time appearing twice), the component is treated as custom.
- **Missing values:** `None` is recorded only when the model has no value, or the value is NaN, and the key is
  listed in `ModelData.stale()`. Pyomo's own `stale` flag is **not** used, because fixed parameters keep valid
  values while Pyomo marks them stale.
- **Checked against real QUDT** (component tests, using Acquirium's bundled vocabulary): every `BASE_UNITS` entry,
  and generated keys for `L/day`, `kg/day`, `mg/L`, `ug/L`, `m/s**2`, `J/kg/K`, `W/m**2`, `kmol/h`, `gal/min`,
  `MWh`, `degC/h` and more, plus `psi` and `hp` found through the resolver. pint turns `degC` inside a compound
  unit into a temperature difference, which maps to `DEG_C` the way QUDT spells rates (`DEG_C-PER-HR`).
- **Units QUDT lacks.** QUDT has no compound currency units (`USD/kWh`, `USD/a`, `USD/kW`) and no `kWh/m^3`. With a
  resolver these export with `unit_qudt=None` and keep their Pyomo unit. Without one they get their generated key
  (`CCY_USD-PER-YR`), marked unverified. So most flex-pse costing values have no verified QUDT unit (upstream
  item U9). Acquirium stores `unit=` as plain metadata without checking it, so nothing breaks.

The exporter has no Acquirium dependency (it produces `ModelData`). The writer is a second step, so the exporter can
be tested without a database.

**D4 — Explicit results API.**
- `orch.store_model_results(tag, model, outcome=None, keys=None, include=None, exclude=None, *, streams=None)`
- `orch.solve_and_store(tag, model)`
- The orchestrator never relies on an implied "current model".
- `keys` / `include` / `exclude` choose what is exported into the snapshot (everything by default); `streams`
  chooses which of those are also stored natively, for queries and fitting (D5).

**Built** in `flex_orchestrator.orchestrator` (the facade), `flex_orchestrator.results` (the D3 step 4 writer) and
`flex_orchestrator.backend` (the contract, memory and Acquirium backends), ahead of the rest of M1. What the build
settled:
- **Signatures:** `Orchestrator(backend, *, study=None, hooks=None, registry=None, unit_mapper=None,
  strict_units=False, frame="polars")`; `store_model_results(tag, model, outcome=None, keys=None, include=None,
  exclude=None, *, streams=None, time_sets=None, datetime_index=None) -> StoredResult(tag, run_id, status, outcome,
  keys, streams)`; `store_model_data(tag, data, outcome=None, *, streams=None)` for values exported another way;
  `solve_and_store(tag, model, keys, include, exclude, *, streams=None, solve=None, time_sets=None,
  datetime_index=None, **options)`; `model_results(tag, units=None) -> ModelData`, read from the snapshot, with
  `units={key or component: pyomo unit}` converting through pint (frames come with D7). The D11 connection
  modes arrive in M1, so for now the backend is passed in.
- **Order of a store:** check the tag → export → build the snapshot and check the native keys against the study's
  declarations (kind, unit conversion, datetimes) → declare what's new → start the run → record the manifest, the
  table, the native values and the outcome → finish. Anything that can't be stored fails *before* a run starts. A
  write that fails part-way marks the run `failed` and re-raises.
- **Failed solves:** an outcome that isn't `ok` is stored as a `failed` run holding only `solve_outcome`, so the
  tag can be retried (D9). `solve_and_store` checks the tag before solving and turns a raising solve into an
  `error` outcome (`run_solve`).
- **Units on Acquirium:** Acquirium rejects a declared unit it can't resolve (`CCY_USD-PER-YR` → 400), so only
  verified QUDT keys are declared, as QUDT URIs (`http://qudt.org/vocab/unit/KiloW`), which Acquirium passes through
  without looking the text up for every stream it registers. `pyomo_unit`, `component` and `index` (a JSON string, since Acquirium flattens
  list metadata into dotted keys) are variable metadata. A label keeps its first declaration: Acquirium compares
  re-declarations literally, so the writer never re-declares; later runs are converted to the declared
  `pyomo_unit` with `conversion_factor`, and a mismatch in dimensions raises `UnitError`.
- **Time:** the default `time_sets` hook is applied, so flex-pse `startup` / `shutdown` store as timeseries. It
  returns `[]` for a model without one TimeBlock, and is skipped if flex-pse isn't installed. A timeseries
  without datetimes (a user model with no `datetime_index=`) is a `StorageError` naming the fix. Timestamps are
  stored and read back as UTC.
- **Acquirium specifics:** one active run per `Study` handle, so each run gets its own handle; run lookup uses
  the SQL `metadata=` filter, not the graph; NaN is rejected by Acquirium's client, which the exporter already
  turns into `None`. Since PR #78 `9a70a1f`, a timeseries sample must have a value, so a native stream leaves
  out points with no value (the snapshot keeps them as nulls).
- **Run inputs (built, M1).** A model from `orch.build(tag, **overrides)` remembers its spec, and `spec=` /
  `overrides=` on `store_model_results`, `store_model_data` and `solve_and_store` name it for a model built another
  way. The result run records the inputs `model_spec_ref` (`{tag, spec_hash}`) and `overrides`, and the metadata
  `spec_tag`, `spec_hash` and `run_hash`. `ResultHandle.spec_tag`, `.overrides` and `.spec()` read them back.

**D5 — Whole results as an Acquirium table snapshot; chosen keys also native.**
- A stored result keeps the whole export as one snapshot: `model_values` (`.table()`, Parquet inside Acquirium)
  and `model_manifest` (`.json()`) (D3 step 4).
- Keys a user names (`streams=`) are also native Acquirium variables (timeseries or scalar), so they can be queried,
  compared across runs and used for fitting (through `orch.acquirium`, and D19).
- Reads come from the snapshot; units are converted with pint (`model_results(tag, units=...)`).
- `store_data` input becomes Acquirium timeseries streams (D6). Specs become `.json()`.
- There is no file store of our own: Parquet is used only through Acquirium's `table` kind.

**Why.** Storing every key natively costs one stream per key, and the per-stream cost is in Acquirium's graph, not
in the data (benchmark below). A snapshot is one table write whatever the number of keys, reads back exactly, and is
what a D17 result file needs anyway.

**Backups.** On both backends Acquirium keeps a `table` (or `file`) artifact's bytes in
`<data_dir>/experiment_artifacts/<sha256>`; the database holds only an index row (digest, name, type, size;
`Experiments.py:556-562`). So a snapshot is covered by a backup of Postgres **plus** the `data_dir` snapshot D14
already takes, not by Postgres alone. Upstream item U12 asks Acquirium to store artifact bytes in the database on
the Timescale backend (a ~30–60 line change in `ExperimentStore` plus a migration); then snapshots are covered by
the Postgres backup with no change here. The manifest, the solve outcome and native values are in Postgres today.

**Benchmark** (`pytest -m benchmark -s`, `tests/test_benchmark_storage.py`). Sized to the models we run: at most a
month at 15-minute steps (2,976 points per key), stored once per model, through `store_model_data` into a local
DuckDB Acquirium (PR #78 `9a70a1f`). Measured 2026-10-21 on a Windows dev machine:

| Keys (× 2,976 points + 10 scalars) | Save (snapshot) | Read back |
|---|---|---|
| 10 | 0.1 s | 0.1 s |
| 100 | 0.1 s | 0.2 s |
| 1,000 | 0.3 s | 0.5 s |
| 100, with 10 also native (`streams=`) | 2.0 s | |

How we got here (one save of 100 keys, 2,976 points each):

| Layout | Save |
|---|---|
| one variable per key, our old pin `a74b3bc` | 56.9 s |
| one variable per key, PR #78 head | 24.7 s |
| one keyed variable (`timeseries(keys=...)`), unit as text | 10.4 s |
| one keyed variable, unit as a QUDT URI | 3.9 s |
| whole model as `.json()` value / `.file()` | 0.7 s / 0.09 s |
| **whole model as `.table()` (chosen)** | **0.03 s** (+ manifest) |

- Per-stream cost is in the graph: `insert_graph` once per variable (and growing with the number of streams already
  registered), plus `resolve_record` once per stream when a unit is given as text. Data inserts run at about
  90,000+ points/s.
- A keyed timeseries records all its streams in one snapshot (one registration, one batch insert, one batch of
  observations); separate variables can't be batched through a public API yet (U11). Native streams therefore stay
  one variable per key for now, for the few keys users ask for.

**D6 — Data API.**
- `store_data(name, df, units={col: unit})` accepts a polars or pandas DataFrame. The index (pandas) or a
  `time_column` (polars) must be datetimes.
- It registers the datasource `flexorch/<study>/<name>`, creates one stream per column with its units, and records a
  run in `<study>::data` with a `.json()` manifest.
- `get_data(name, columns=None, start=None, end=None, frame=...)` reads it back.
- `get_data(column=...)` finds a single column across datasets, and raises an error if the name is ambiguous.

**Built** in `flex_orchestrator.data` and `Orchestrator.store_data` / `get_data`, on the D4 storage contract. What
the build settled:
- **No separate datasource.** A dataset is one run of `<s>::data` (`{flexorch_kind: "data", flexorch_tag: name}`),
  and each column is that run's timeseries input, labelled `<name>/<column>` so two datasets can share a column
  name. Acquirium stores a run's streams under `experiment/<run_id>` itself, so there is no `flexorch/<study>/<name>`
  datasource to register. Column variables carry `dataset`, `column` and `unit_text` metadata; the `manifest` JSON
  lists the columns in order, their units, the row count and the time range.
- **Signatures:** `store_data(name, df, units=None, *, time_column=None) -> StoredData(name, run_id, columns, rows,
  units)`; `get_data(name=None, columns=None, start=None, end=None, *, column=None, frame=None)`. `start` and `end`
  are both inclusive. A dataset name is stored once (`TagExistsError`); datasets and results are separate kinds.
- **Input tables:** pandas uses its `DatetimeIndex` unless `time_column=` is given; polars uses its only datetime
  column unless one is named. Columns must be numeric. NaN and pandas' `NA` become missing values, which are no sample (Acquirium needs a value in every timeseries row), so reading one column back skips the rows it has no value for. Infinite values,
  repeated timestamps, empty tables, and `units=` naming a column the table doesn't have are refused before a run
  starts. Timestamps are stored in UTC (naive means UTC).
- **Units:** as written by the user (`"m3/day"`, `"gal/min"`) or as QUDT keys. They go through the exporter's
  `UnitMapper` (pint's parts, verified), then the resolver's own text lookup, which understands spellings pint
  can't parse (`m3/day`, `GAL_US-PER-MIN`). Only a verified key is declared; the text is always kept. Without a
  resolver (the memory backend) units are text only, with no warning.
- **Reads:** a polars frame with a `time` column first, or a pandas frame with a UTC `DatetimeIndex` named `time`.
  Only the requested columns' data is read (`records(labels=)`). `get_data(column=)` looks through the declared
  columns of succeeded datasets: none → `TagNotFoundError`, several → `AmbiguousColumnError` naming them.
- **Contract additions:** `variables(study)` and `records(..., labels=)`. polars and pandas are core dependencies.

**D7 — You choose polars or pandas.**
- Every read accepts `frame="polars" | "pandas"`.
- The default is set per orchestrator, `Orchestrator(..., frame="pandas")`.
- Results are built in polars and converted with `polars.DataFrame.to_pandas()`, with a DatetimeIndex restored.
- Shorthand methods: `.to_pandas()` / `.to_polars()`.

**Built.** `flex_orchestrator.frames` converts tables in and out (D6), and stored results are read through handles:
- `orch.result(tag)` and `orch.stored_model_results(tags=None)` return `ResultHandle(tag, run_id, metadata,
  started_at)`. A handle reads its snapshot on first use and keeps it.
- `handle.to_frame(frame=None, units=None)` returns the timeseries as one table: a `time` column first (polars) or
  a UTC `DatetimeIndex` named `time` (pandas), and one column per key. A subset of time (`startup`) is null at the
  points it doesn't cover. `.to_polars()` / `.to_pandas()` are the shorthands; `frame=None` uses the orchestrator's
  default.
- `handle.data(units=)` returns `ModelData`; `handle.scalars()`, `handle.units` (`{key: pyomo unit}`) and
  `handle.outcome` (the `SolveOutcome`, or `None`) cover the rest. `units=` converts with pint (D5).
- `orch.model_results(tag, units=)` is `orch.result(tag).data(units)`; `orch.get_data(..., frame=)` (D6).

**D8 — Acquirium is used directly; no query wrappers.** `orch.acquirium` returns the backend's Acquirium client, and
users query it with Acquirium's own API (`ac.study.get("<s>::runs").experiments.where(...)`, `ac.query()`,
`sparql`, ...). The orchestrator doesn't wrap, scope or re-export queries, so there is no second query API to keep in
step with Acquirium's. Isolation comes from the deployment (D14): the client only reaches the tenant's own
Acquirium, so raw access leaks nothing. On a backend with no Acquirium (the memory backend) `orch.acquirium`
raises `StorageError`.

**D9 — No duplicates, no overwrites; deletion is real.** Acquirium allows duplicates, so the orchestrator enforces its
own rules.
- **Studies.**
  - `create_study(name)` checks `ac.study.get(name)` first, and raises `StudyExistsError` if the study exists.
  - `select_study(name)` raises `StudyNotFoundError` if it doesn't exist.
  - `get_or_create_study(name)` is the explicit way to say "either is fine".
- **Tags.**
  - Within a study, every stored item has a unique `(kind, tag)`, saved in run metadata as
    `{flexorch_kind, flexorch_tag}`.
  - Storing an existing tag (status `succeeded` or `running`) raises `TagExistsError`.
  - A `failed` run doesn't count as existing, so you can retry it.
- **Specs.** A content hash (`spec_hash`, sha256 of canonical JSON) is stored with each spec. `execute_study` uses
  the hash plus the overrides to skip runs that already succeeded (resume).
- **Races.** Check-then-write isn't atomic in Acquirium. Mitigations:
  - one writing process per tenant: the main process in D13, and in Phase 2 the tenant's orchestrator service;
  - an in-process lock for each study, held from the tag check to the end of the write;
  - **a check after every write**: if two live runs carry the same tag, the earliest one wins; the later one is
    deleted and its call fails with `TagExistsError`. With `replace=True` the latest wins instead.

**Deletion** uses Acquirium's own (PR #78 `9a70a1f` added `delete_runs`, `delete_study` and `clear`; upstream item
U3). Deleting removes the runs' observations, samples and unshared files for good; a running run is never deleted.
- `orch.delete(kind, tag)` deletes every run with that kind and tag (failed attempts included), so the tag is free
  again. `kind` is `"result"`, `"data"`, `"model_spec"` or `"surrogate_spec"`.
- `orch.delete_study(name)` deletes the study's ledger studies (`<s>::runs`, `<s>::data`, ...) and the study itself,
  and deselects it if selected. A running run anywhere in it stops the delete before anything is removed.
- `store_*(tag, ..., replace=True)` writes the new item first, then deletes the old one, so a write that fails
  (or a solve that isn't `ok`) leaves the old item in place. `replace` defaults to `False`, so overwriting is never
  accidental.
- `Orchestrator(..., allow_delete=False)` turns off `delete`, `delete_study` and `replace` for a deployment that
  must never lose a stored item.
- There is no soft delete, tombstone ledger or `include_deleted`: a deleted item is gone, and backups (D14) are the
  way back.

**Built** in `Orchestrator` and the storage contract (`delete_runs(study, run_ids)`, `delete_study(name)`, on both
backends). The race check is tested by letting a rival write land between the tag check and the write.

**D10 — How an orchestrator study is laid out in Acquirium.**

| Acquirium Study | One run = | Variables |
|---|---|---|
| `<s>` | — (the study itself; created by `create_study`) | none yet |
| `<s>::data` | one `store_data` call | `manifest` (`.json`) plus one `.timeseries(unit=)` per column |
| `<s>::specs` | one `store_model_spec` / `store_surrogate_spec` call | `spec` (`.json`); metadata `{flexorch_kind, flexorch_tag, spec_hash, parent_tag, spec_format}` |
| `<s>::runs` | one model solve | inputs `model_spec_ref` (`.json`), `overrides` (`.json`); outputs `model_values` (`.table`), `model_manifest` (`.json`), the `streams=` keys as native variables (D3, D5), `solve_outcome` (`.json`); log `solver events` |
| `<s>::fits` | one `fit_surrogate` variant (D19) | inputs `fit_params` (the fit as planned, selectors as sent), `training_data` (what they resolved to, prep steps, rows, units, `data_hash`) (`.json`); outputs `fit_result` (coefficients, metrics, diagnostics, holdout) (`.json`), optional `fit_series` (`.table`: `actual`/`fitted`/`residual`); metadata `{flexorch_kind: "fit", flexorch_tag, regressor, data_hash}` |
| `flexorch::commands` | one command call (D18) | input `params`, output `result` (`.json`); metadata `{flexorch_kind: "command", flexorch_tag: <command>, flexorch_study, flexorch_runtime}` |

Every run's metadata also records the orchestrator, flex-pse and Acquirium versions, the user (from the environment
in Phase 1, from the token in Phase 2), and the hook names, so a result can be reproduced.

**Built** for `<s>`, `<s>::runs` and `<s>::data` (the `LEDGERS` map in `orchestrator.py`). Run metadata holds:
- `flexorch_kind`, `flexorch_tag`;
- `flexorch_version`, `flexpse_version`, `acquirium_version` (a package that isn't installed is left out);
- `flexorch_user`: `FLEXORCH_USER`, else the login name;
- `flexorch_hooks` (results only): `{slot: import string or registered name}` for every set hook, including a
  `solve=` given to `solve_and_store`; a callable with no import string is recorded as `<qualname>`;
- `flexorch_connection`: `{mode, target}` with the redacted target (D11), when the orchestrator made the connection.

Tested on both backends, so the nested values round-trip through Acquirium's metadata. `<s>::specs` and the result
inputs `model_spec_ref` / `overrides` are **built** (M1; see D2 and D4); `<s>::fits` and `flexorch::commands` are
**built** (M4; see D18 and D19). A fitted surrogate spec's metadata holds `fit_run_id` and `data_hash`; an imported
item's holds `flexorch_imported_from` (D17). **Not yet:** the `solver events` log. The solve outcome's `log` field
holds the solver output for now.

**D11 — Connection modes and database credentials.** The library connects to exactly one Acquirium, which in
deployment means one tenant. There are three modes:

```python
# 1. local: managed Acquirium + DuckDB in data_dir. No credentials. Laptops, tests, quick work.
orch = Orchestrator()                                         # data_dir=".flexorch"

# 2. postgres: the library starts and owns a local Acquirium backed by Postgres. Developers and single-user servers.
orch = Orchestrator(db="postgresql://db-host:5432/tenant_a",  # no password in the URL
                    data_dir="/srv/flexorch/acquirium")        # graph + artifacts; must persist

# 3. remote: connect to a running Acquirium. This is the tenant stack (D14), where Acquirium owns its DB secret.
orch = Orchestrator(acquirium_url="http://acquirium:8000")
```

- **No username or password in script code.** In mode 2, the database credentials are taken in this order:
  1. `FLEXORCH_DB_PASSWORD_FILE` (a secret file);
  2. `FLEXORCH_DB_USER` / `FLEXORCH_DB_PASSWORD`;
  3. libpq's `PGUSER` / `PGPASSWORD` / `~/.pgpass`.
  An explicit `db_credentials=DBCredentials(user, SecretStr(pw))` argument exists only for developers and tests.
- **Mode 2 mechanics:**
  - build the DSN with `psycopg.conninfo.make_conninfo`;
  - write a toml with `timeseries_backend = "timescale"` and **no `pg_dsn`**;
  - pass the DSN **only through the child process's environment** as `PG_DSN`, and the password separately as
    `PGPASSWORD` (so the DSN itself never holds it), then call `aq.init(config=...)`;
  - never write the password to disk, never log it, and never record it in metadata (only a redacted
    `user@host/db`).
- **Checks before starting (mode 2):**
  - authentication, with a clear `DBAuthError`;
  - the timescaledb extension is present;
  - the role can create tables;
  - a lock record in the database refuses a second managed server that uses a different `data_dir`.
- **Never `recreate`.** The orchestrator never sets it, and rejects configs that do.
- **Mode 3** needs no database credentials at all. In Phase 1 the network around the tenant stack is what protects
  Acquirium (D14). Phase 2 adds token auth at the orchestrator (D15).

**Built** in `flex_orchestrator.connection`; `Orchestrator(backend=None, *, data_dir, db, acquirium_url,
db_credentials)` calls `connect()`, and `orch.connection` / `orch.close()` / `with Orchestrator(...)` manage it.
- **Modes.** Passing a backend together with connection arguments, `acquirium_url` with `db` / `data_dir`, or `db`
  without `data_dir` raises `ValueError`. Mode 1 refuses a `data_dir` set up for Postgres, and mode 2 one that holds
  a DuckDB store, so one directory never serves two different databases.
- **Credentials** come from `credentials_from_env()` in the D11 order; `DBCredentials(user, password)` keeps the
  password as a `SecretStr`. A password inside `db=` is refused, and the error doesn't repeat `db`.
- **The password reaches the server as `PGPASSWORD`, not inside `PG_DSN`.** Acquirium logs
  `dsn.split("@")[-1]` at debug level, which would print part of a password containing `@` (U14). Both variables
  are set only while `aq.init` starts the child process, under a lock, and restored afterwards.
- **Preflight** connects with psycopg (10 s timeout). A refused login is a `DBAuthError`, other connection failures
  a `StorageError`, both with the password scrubbed and the original exception suppressed. Then it checks that
  `timescaledb` is available (Acquirium runs `CREATE EXTENSION` itself) and that the role can create tables in its
  schema.
- **The server lock** is a one-row `flexorch_server` table that binds the database to `<hostname>:<data_dir>` the
  first time. Another managed server on a different directory is refused, since the matching graph and files live in
  the first one's. The binding is permanent; moving `data_dir` means updating that row by hand.
- **The config** `flexorch-acquirium.toml` is written into `data_dir`, holding `timeseries_backend = "timescale"`,
  `recreate = false`, `exact_only` and no `pg_dsn`. `aq.init` itself refuses `recreate` and strips inherited
  `ACQUIRIUM_*` variables.
- `settings.py` is folded into `connection.py`: plain environment reads and pydantic's `SecretStr`, with no
  pydantic-settings dependency.
- **Tested** in the unit tier with Postgres and `aq.init` faked, including a redaction test over the config,
  exceptions, metadata and the process environment. A component test attaches mode 1 to the session's Acquirium.
  Against a **real Postgres 18** (no TimescaleDB, a password with `@` and `:`): the login from
  `FLEXORCH_DB_PASSWORD_FILE` reaches the extension check, and a wrong password gives `DBAuthError` without the
  password (`test_connection_postgres.py`, opt-in with `FLEXORCH_TEST_PG`). **Not yet:** a full mode 2 run, which
  needs TimescaleDB; there is no Windows build on conda-forge, so it waits for a Postgres in CI.

**D17 — Study export to a folder (and import back).** `orch.export_study(path)` writes a self-contained folder that
**mirrors the study's structure**: specs, data and model results, all as **JSON**. Index structure, units and
provenance are explicit, so the folder can be read without Acquirium or the orchestrator. It also works as a portable
copy of one study for sharing, archiving or moving between tenants, on top of the D14 database backups.

```
example_a/                                  # one folder per study
  study.json                                # manifest: schema version, study name, exported_at, exported_by, tenant,
                                            #   orchestrator/flex-pse/acquirium versions, counts, options used
  data/
    biogas_data.json                        # one file per dataset (D6)
  specs/
    models/base_model.json                  # one file per model spec tag (D2)
    models/model_with_surrogate_biogas_h1.json
    surrogates/biogas_h1.json               # one file per surrogate spec tag
  fits/
    biogas_h1.json                          # one file per fit run (D19): regressor, params, diagnostics, data provenance
  results/
    model_with_surrogate_biogas_h1.json       # one file per result tag (D3/D4)
  checksums.sha256                          # sha256 of every file, checked on import
```

*File formats* are versioned (`"schema": "flexorch-export/1"`). Each is defined as a pydantic model, and the JSON
Schemas are published in `docs/schemas/`.
- **Spec file:**
  `{tag, kind, spec_format, spec_hash, parent_tag, created_at, created_by, run_id, versions, spec: {...}}`.
- **Dataset file:**
  `{name, time_index: [ISO-8601 with timezone, ...], columns: {col: {unit, values: [...]}}, manifest}`.
- **Result file:** each value keeps the index structure and units that D3 extracted.

```json
{
  "schema": "flexorch-export/1",
  "tag": "model_with_surrogate_biogas_h1",
  "run_id": "…", "status": "succeeded", "started_at": "…", "finished_at": "…",
  "model_spec_ref": {"tag": "model_with_surrogate_biogas_h1", "spec_hash": "…"},
  "overrides": {"costing.electricity_price": 0.1},
  "hooks": {"build": "flex_orchestrator.flexpse:build", "solve": "flex_orchestrator.flexpse:solve"},
  "solve_outcome": {"ok": true, "status": "ok", "termination": "optimal", "wall_time": 12.3},
  "time_index": {"set": "time_block.time_index", "timestamps": ["2026-01-01T00:00:00+00:00", "…"]},
  "timeseries": {
    "plant.digester.biogas_volume": {"unit": "M3-PER-DAY", "pyomo_unit": "m**3/day",
                                     "component": "plant.digester.biogas_volume", "index": [], "values": [ … ]},
    "costing.unit_power[pump1]":    {"unit": "KiloW", "pyomo_unit": "kW",
                                     "component": "costing.unit_power", "index": ["pump1"], "values": [ … ]}
  },
  "scalars": {
    "costing.total_capital_cost":   {"unit": "USD", "component": "costing.total_capital_cost", "index": [], "value": 1.2e6},
    "costing.capital_cost[pump1]":  {"unit": "USD", "component": "costing.capital_cost", "index": ["pump1"], "value": 3.4e5}
  }
}
```

- Each result stores its timestamps **once** (`time_index`), and every series lines up with them. `component` plus
  `index` rebuild the original Pyomo indexing, and `unit` (QUDT) plus `pyomo_unit` keep both unit systems.
- Floats are written so they read back exactly. Missing or stale values are written as `null`, and stale values are
  listed in `stale: [...]`. Raw indices that aren't strings are kept as JSON arrays of their original types.

*API:*

```python
report = orch.export_study("exports/example_a", parts=("specs", "data", "results"),
                           tags=None, compress=False)                          # compress → *.json.gz
orch.export_result("model_with_surrogate_biogas_h1", "exports/r1.json")          # a single file
res = flex_orchestrator.archive.load_result("exports/example_a/results/…json")  # no DB needed → ResultHandle
res.to_frame("pandas"); res.units

orch.import_study("exports/example_a", name=None, on_existing="error")          # checks schema + checksums, re-creates
```

- **Export** reads from the active study, streams one file at a time (memory use stays bounded for large studies),
  and writes into a temporary folder that is renamed into place at the end, so a crash never leaves a half-written
  export.
  - It refuses to write into a folder that isn't empty, unless `overwrite=True`.
  - It never includes credentials, DSNs or secrets. A test checks this.
- **Import** checks the schema version and every checksum first. It then re-creates the study through the normal
  store APIs, so the D9 uniqueness rules and the unit declarations apply. Specs come first, then data, then results,
  keeping the original tags, `spec_hash` and run metadata. The original `run_id` and `exported_by` are recorded as
  `imported_from` provenance.
- **Isolation (D14):** export and import run inside one tenant's stack. Moving a study to another tenant is an
  explicit two-step operation by someone with access to both: export from A, then import into B. No route reaches
  across tenants.
- CLI: `flex-orch export <study> <path>` and `flex-orch import <path>`. Phase 2 routes are in §4.

**Built** (M3) in `flex_orchestrator.archive`, as specified, with these details settled:
- **Layout.** A spec's referenced files are written under `specs/` by their stored names
  (`specs/assets/<sha256[:12]>-<name>`), raw, so the spec file's paths point at them. `compress=True` gzips every
  JSON file except `study.json` (`mtime=0`, so it's reproducible). `study.json` lists every other file;
  `checksums.sha256` covers all of them.
- **Result files** carry `keys` (walk order) and, for a series covering part of time (`startup`), `rows`: its
  positions in `time_index`. Each value is aligned to `time_index`, `null` where missing. `time_index` has no `set`
  name (the exporter doesn't keep it), and runs have no finish time in the storage contract, so there's no
  `finished_at`. Only succeeded results are exported.
- **Fit files** (`fits/<tag>.json`) hold the fit run's records; `series` when it stored one.
- **Import** checks the schema of every file and every checksum (and that the checksum list matches the manifest)
  before writing, then imports fits, specs (parents first), datasets and results. Specs are written as stored
  (their hash is verified, not recomputed from a re-normalised spec), a surrogate spec's `fit_run_id` is remapped to
  the new fit run, and a result keeps its original `flexorch_hooks`. Each item's metadata records
  `flexorch_imported_from: {study, exported_at, exported_by, run_id, metadata}`. `on_existing="skip"` imports into an
  existing study, skipping the items it has. A result whose spec is stored with another hash is refused; one whose
  spec isn't in the export is imported unlinked.
- `tenant` is `None` in Phase 1. The JSON Schemas are in `docs/schemas/` (`python -m flex_orchestrator.schemas`
  writes them; a test checks they're current).

**D18 — Extension API: commands, plugins, subclassing, custom routes.** Users can extend the orchestrator without
forking it. **Commands are the main extension point.** They live in the core library (Phase 1, no FastAPI), and
Phase 2 exposes them over HTTP automatically. Custom FastAPI routes come in Phase 2 and inherit auth and tenant checks
by default.

*Commands* (`flex_orchestrator.ext`):

```python
from pydantic import BaseModel
from flex_orchestrator.ext import command, Progress

class PriceSweepParams(BaseModel):
    prices: list[float]
    base_tag: str

@command(name="price_sweep", role="editor", long_running=True)
def price_sweep(orch: Orchestrator, params: PriceSweepParams, progress: Progress) -> dict:
    ...                                                   # any orchestrator API, may submit Ray tasks
    return {"stored": [...]}
```

- One definition is callable from everywhere:
  - Python: `orch.run_command("price_sweep", prices=[0.1, 0.2], base_tag="base")`;
  - CLI: `flex-orch cmd price_sweep --prices 0.1 0.2 --base-tag base` (options generated from the params model);
  - HTTP (Phase 2): `POST /commands/price_sweep` returns `202 {job_id}`. The body is checked against the params
    model, and the OpenAPI schema is generated from it.
- What commands get for free:
  - checking parameters against the pydantic model;
  - a `Progress` object that reports progress and handles cancellation;
  - recording each call as a run (inputs, outcome, user) in `<tenant>::commands`, so every call has a record;
  - `role` enforcement in Phase 2;
  - `long_running=True` sends the call through the job queue.
- **`execute_study` is itself a built-in command.** There is one execution and job path for built-in and custom
  work, not two.
- Command names are unique. A collision with a built-in or another plugin fails at startup.

*Plugins* bundle extensions:

```python
plugin = Plugin(
    name="digester_tools",
    api_version=">=1,<2",                    # PLUGIN_API_VERSION range it supports
    commands=[price_sweep],
    hooks={"digester_build": "digester_tools.models:build"},   # named D1 hooks
    orchestrator_cls=DigesterOrchestrator,   # optional subclass (below)
    routers=[router],                        # Phase 2 only: fastapi.APIRouter instances
    on_startup=..., on_shutdown=...,
)
```

- **How plugins are found:**
  - the `flex_orchestrator.plugins` entry-point group in the plugin package's `pyproject.toml`
    (`[project.entry-points."flex_orchestrator.plugins"] digester_tools = "digester_tools:plugin"`);
  - or passed directly, `Orchestrator(plugins=[...])` / `create_app(plugins=[...])`.
- **Only listed plugins load.** A plugin loads only if it appears in `FLEXORCH_PLUGINS=digester_tools,...`, or is
  passed explicitly. Being installed isn't enough, so each tenant can enable a different set.
- **Version check:** if a plugin's `api_version` range doesn't include `PLUGIN_API_VERSION`, it fails at startup with
  a clear error.
- `routers` are accepted only when `server/` is loaded, so the core library still never imports FastAPI.

*Subclassing the orchestrator* is supported, with a deliberately small stable surface:
- These methods are stable to override:
  - `build_hooks()` returns the default hooks;
  - `default_export_selection()`;
  - `before_store(item) / after_store(item)`, which can reject an item or add metadata to it;
  - `on_task_result(result)`;
  - `on_study_created(study)`.
- **Lifecycle events** (`study_created`, `spec_stored`, `result_stored`, `job_finished`, `command_finished`) are a
  lighter alternative: `@orch.on("result_stored")` or `Plugin(on_events={...})`.
- Everything not documented as stable is internal (prefixed with `_`, and not part of the semver promise).
- The server builds its orchestrator from `Plugin.orchestrator_cls`, falling back to `Orchestrator`. If two enabled
  plugins provide subclasses, startup fails.

*Custom FastAPI routes (Phase 2):*
- Build apps only through **`create_app(settings, plugins)`**. There is no module-level `app` created at import time.
  `flex-orch serve` and uvicorn use `--factory`.
- Plugin routers are mounted at **`/ext/<plugin>/…`**, with the auth, tenant and audit dependencies applied **to the
  whole router** (`app.include_router(router, prefix=..., dependencies=[Depends(authenticated_tenant)])`). **A plugin
  route can't be unauthenticated by accident.** Core prefixes (`/studies`, `/jobs`, `/commands`, `/health`,
  `/ready`, `/ext`) are reserved, and a collision fails at startup.
- **Request dependencies** in `flex_orchestrator.server.deps`, the same ones the core routes use:
  - `get_orchestrator` (the tenant's orchestrator, or the subclass);
  - `current_principal` (user, tenant, role);
  - `require_role("viewer" | "editor" | "owner")`;
  - `get_jobs` (submit or track commands from a custom route);
  - `get_settings`.
- Plugin routes appear in the OpenAPI schema under a tag per plugin, so the generated UI client (D16) picks them up.

*Isolation (D14):*
- Plugins are code **installed in the tenant's image**. They run only inside that tenant's stack and Ray, like user
  hooks.
- Tenants that need custom plugins get an image variant, or a pip-install layer at provisioning. Which plugins are
  enabled is set per tenant through `FLEXORCH_PLUGINS`.
- The isolation suite also runs with a deliberately hostile sample plugin enabled.

*Supporting plugin authors:*
- `flex_orchestrator.testing` provides:
  - `memory_orchestrator()`;
  - `run_command_in_test(...)`;
  - (Phase 2) `make_test_app(plugins=[...])` and `token_for(role, tenant)`.
- A complete sample plugin lives in `examples/plugins/digester_tools/`, with its own pyproject and tests. It runs in
  CI.
- `PLUGIN_API_VERSION` follows semver, separately from the package version, and changes are recorded in the
  changelog.

**Built** (M4, the core part) in `flex_orchestrator.ext`, with these details settled:
- **Commands.** `fn(orch, params, progress, **runtime)`; a command needing less may take `(orch)` or
  `(orch, params)`. `runtime` carries what JSON can't (a `build=` callable from `orch.execute_study`) and is recorded
  only as import strings. Calls are recorded in `flexorch::commands` (one ledger per store, the Phase 1 tenant);
  `role` and `long_running` are recorded for Phase 2. `orch.register_command()` adds one without a plugin.
- **Plugins.** `Orchestrator(plugins=[...])` loads the ones passed (objects or installed names) plus those in
  `FLEXORCH_PLUGINS`; `ext.create_orchestrator(...)` also builds a plugin's subclass (a plain `Orchestrator` with a
  subclass-providing plugin warns). A bare import string in `Plugin.hooks` is registered for every hook slot.
  `routers` are kept, unmounted, until the Phase 2 server. `PLUGIN_API_VERSION` is `1.0.0`; ranges are
  comma-separated `>=`, `>`, `<=`, `<`, `==`, `!=` clauses.
- **Events** add `data_stored` and `fit_stored` to the list above; a listener that raises propagates (the item is
  already stored). `before_store(item)` gets a `StoreItem(kind, tag, metadata)`: it raises to refuse, or adds
  metadata, which can't replace the orchestrator's own keys.
- **Testing helpers:** `memory_orchestrator()` (fake hooks, no allowlist) and `run_command_in_test()`.
- **Sample plugin:** `examples/plugins/ro_tools/` (setuptools, so it builds offline), with a command, named hooks
  and a subclass overriding `before_store` and `default_export_selection`.

**D19 — Regression as a command (`fit_surrogate`).** Fitting surrogates from data stored in Acquirium is a
**built-in D18 command**. So it is available in Python (`orch.fit_surrogate(...)` / `orch.run_command("fit_surrogate",
...)`), from the CLI (`flex-orch cmd fit_surrogate`), and over HTTP (`POST /commands/fit_surrogate`, Phase 2) with
**the same JSON parameters**. It is built on flex-pse's generic regression interface,
`flexparameterize.regression`:
- the `Regressor` protocol (`fit(X, y)`, `to_fit_result()`, `to_surrogate_spec(**kw)`);
- `get_regressor(SurrogateType)`, which in 0.2 maps `constant_intensity` and `multilinear` to their classes and raises
  `NotImplementedError` for the reserved types.

So the orchestrator never needs to know which kind of regressor it is running.

**Surrogate coverage follows the pinned flex-pse.** D19 is built and tested against the surrogate types the pinned
flex-pse implements end to end, which in 0.2 means `multilinear` and `constant_intensity`. Nothing in `fitting/` is
specific to a type. A newly implemented type (ARIMA from U8 first, then quadratic, exponential, neural network) is
added when the pin moves: bump the SHA, add the type to the test matrix and the docs, and add any `prep` it needs
(a time-series regressor such as ARIMA needs a regular, gap-free index). A reserved type gives a clear error naming
the installed flex-pse version.

*1. Data selectors* (JSON, so they work over HTTP). X and y each take one selector, or a list of them, which are
merged column-wise. Every selector resolves to a list of Acquirium stream references plus a window:

| Selector | Source |
|---|---|
| `{"dataset": "biogas_data", "columns": [...]}` | a dataset in the active study (D6) |
| `{"study": "example_b", "dataset": "...", "columns": [...]}` | a dataset in **another study of the same tenant** |
| `{"result": "<result tag>", "keys": [...]}` | exported model results (D3), e.g. fitting on simulated data |
| `{"ref_uris": ["urn:…"]}` | specific Acquirium streams in the tenant's general store |
| `{"streams": [{"source_id": "historian", "ref_name": "FT_101"}]}` | the same, by the names their driver registered them with (Acquirium derives the URI from the pair) |
| `{"resolve": "digester biogas volume"}` | Acquirium text resolution (`ac.resolve`). The command fails if the match is ambiguous. |
| `{"sparql": "SELECT ?ref WHERE { … }"}` | a **read-only** SPARQL query that returns stream references (UPDATE is rejected) |

- Shared options: `start`, `end`, `units` (target QUDT units, converted through Acquirium), `rename` (column names).
- Python callers can also pass a DataFrame or an Acquirium `Query` directly. The HTTP API accepts only the JSON forms.
- Selectors reach only the tenant's own Acquirium. Under D14 there is nothing else to reach.

*2. Preparation* (`prep`). The 0.2 regressors drop rows with nulls (and `constant_intensity` also drops zero-flow
rows), but X and y must still share one index, and time-series regressors added later need a regular, gap-free
DatetimeIndex. So the command prepares the data first:
- `align`: `inner` or `outer` join on timestamps; timezones normalised to UTC.
- `resample`: `{freq, agg}`.
- `gaps`: `error` (the default), `interpolate` (with a `limit`), or `longest_segment`.
- `max_rows`: a guard against running out of memory (default 1e6).
- Unit conversion to each selector's `units`.

Every preparation step is recorded with the fit.

*3. Fitting:*
- `regressor` is either a `SurrogateType` name, resolved by the flex-pse hook
  `flex_orchestrator.flexpse:get_regressor`, or an **import string** to any class that follows the `Regressor`
  protocol (user code or a D18 plugin).
- `params` are the constructor arguments (none for the 0.2 regressors; ARIMA's `order` once U8 lands).
- `spec_options` are passed to `to_surrogate_spec`.
- **Column names and units.** The surrogate's variable names are the column names, so they must be the unit's
  variable names (`flow_in_feed`, `biogas_volume`); `rename` on a selector maps them. The flex-pse hook passes each
  column's units to the regressor: to `fit(input_units=, output_units=)` for `multilinear`, and to
  `to_surrogate_spec(input_units=, output_units=)` for `constant_intensity`. Selector units are QUDT, and surrogate
  specs need Pyomo unit strings, so the hook converts them with the D3 unit table in reverse (`M3-PER-HR` to
  `m^3/hr`). A unit with no Pyomo equivalent is an error.
- `variants` holds several parameter sets, and `tag` is a template rendered for each, e.g. `"biogas_{name}"`. A
  variant can override `params`, `regressor`, `X`, `y`, `window` and `prep`, so one call can fit, say, the same
  relation on two seasons of data, or `multilinear` against `constant_intensity`.
  - The data is loaded and prepared **once** in the main process.
  - With `max_workers > 1`, the variants fit in parallel on the tenant's Ray (data shared with `ray.put`, D13).
  - The main process writes all results.
- `validation`: `{"holdout": "7D"}` gives holdout metrics (RMSE, MAE, MAPE). This is used only if the regressor has
  `predict`, which the base protocol doesn't require and neither 0.2 regressor has. For the 0.2 types the flex-pse
  hook computes the holdout predictions itself, from the fitted `multilinear` coefficients (intercept plus the sum of
  coefficient times input) or the `constant_intensity` coefficient (coefficient times input).

*4. What gets stored:*
- **A surrogate spec** for each variant under its tag in `<s>::specs`. The D9 rules and `on_existing` apply.
- **A fit run** in `<s>::fits`, recording:
  - `to_fit_result()` and `fit_diagnostics` (where available), the regressor and its parameters;
  - **the training-data provenance**: the selectors as sent, the stream references they resolved to, the window,
    the preparation steps, the row count, and a sha256 of the prepared data;
  - optional `fitted` / `actual` / `residual` timeseries outputs with units, for plotting.
- The spec's metadata links back to its fit run (`fit_run_id`). D17 exports include `fits/<tag>.json`.

*5. Fitting and sweeping in one `StudyConfig`:* an optional `fits:` section runs before `sweeps`, so a surrogate sweep
can refer to tags fitted in the same run. The original pseudocode workflow (fit orders 1–3, then swap each into the
model and solve) becomes one config, or one `POST /commands/execute_study` request. With 0.2 the surrogates are
`multilinear`; the ARIMA order sweep becomes a `variants` list once U8 lands, with no other change.

```python
orch.fit_surrogate(
    tag="biogas_{name}", regressor="multilinear",
    X={"dataset": "biogas_data", "columns": ["feed_volume"], "rename": {"feed_volume": "flow_in_feed"}},
    y={"resolve": "digester biogas volume", "units": "M3-PER-DAY", "rename": {"*": "biogas_volume"}},
    prep={"resample": {"freq": "1h", "agg": "mean"}, "gaps": "interpolate", "limit": 3},
    variants=[
        {"name": "h1", "window": {"start": "2025-01-01", "end": "2025-06-30"}},
        {"name": "h2", "window": {"start": "2025-07-01", "end": "2025-12-31"}},
    ],
)
```

```http
POST /tenant-a/commands/fit_surrogate            (editor token; body = the same parameters + "study")
→ 202 {"job_id": "…"}
GET  /tenant-a/jobs/{id} → {"status": "succeeded", "result": {"tags": ["biogas_h1","biogas_h2"], "fit_runs": [...], "diagnostics": {...}}}
```

**Built** (M4) in `flex_orchestrator.fitting` and `flexpse/regression.py`, with these details settled:
- **Selectors.** All seven kinds. The general-store ones (`ref_uris`, `streams`, `resolve`, `sparql`) need a backend with the
  optional `StreamStore` methods (the Acquirium backend has them). `resolve` matches stream labels through SPARQL
  (an equal label first, then labels containing every word) and falls back to Acquirium's `resolve` for a concept's
  streams; a local server started with `exact_only=True` does no fuzzy text search, so label matching is what finds
  a historian tag. A column is named by the stream's source-local name unless `rename`d. `units` takes a Pyomo unit
  or a QUDT key and converts with pint, so it doesn't call Acquirium.
- **Units to the regressor** go by signature: `fit` and `to_surrogate_spec` get `input_units` / `output_units` when
  they take them, so the same path serves flex-pse's and user regressors. A column the regressor needs a unit for
  must have one. QUDT keys convert to Pyomo units by reversing the D3 rule (`export.units.qudt_to_pyomo`).
- **Prep** adds `gaps: drop`. Data is loaded and prepared once per distinct selection; variants that change
  `X`, `y`, `window` or `prep` load their own.
- **Validation:** `{"holdout": "7D"}` fits on the rows before the last 7 days and reports `rmse`, `mae`, `mape` on
  them, using the regressor's `predict` or, for the 0.2 types, `flexpse:predict_surrogate`.
- **Stored:** see the D10 table. The surrogate spec gets `provenance.fit = {regressor, data_hash, n_samples,
  data_window}`, so the same data gives an identical spec. Tags are checked before anything is fitted;
  `on_existing` is `error`, `skip` or `replace`.
- A `constant_intensity` surrogate is applied with the same `apply_surrogate` hook: flex-pse's surrogate element
  fixes the unit's `energy_intensity`.
- **Variants on Ray (built, M5).** With `max_workers > 1` (or a study's `execution`), each prepared dataset is
  `ray.put` once, the fits run as Ray tasks (`fitting.command._compute`, no store), and the main process stores
  them in plan order, so the results equal fitting in-process.

**D21 — Model inputs from stored data (`model_inputs`).** A study (or one build) can set model variables from data
already stored: a dataset of the study, a stored result, or general-store streams. A `model_inputs` entry uses
**the same selector syntax as a D19 fit's `X` / `y`** (`dataset`, `result`, `ref_uris`, `streams`, `resolve`,
`sparql`, with `start`, `end`, `units`, `rename`), so a user who can select fit data can select model inputs. After
`rename`, **each column's name is the model path it sets** (a Pyomo component path, as in a `variable` sweep).

```yaml
base_model_spec: base
model_inputs:                            # D21: applied to every task, before its sweep values
  - dataset: ro_operation                # this study's dataset; one column per path
    columns: [demand]
    rename: {demand: plant.product.flow}
    units: m^3/hr                        # convert to the model's units (pint)
  - streams: [{source_id: historian, ref_name: FT_101}]
    rename: {"*": plant.feed.temperature}
    timezone: America/Los_Angeles        # the facility's zone; default UTC (see below)
    resample: {agg: mean}                # onto the model's time step; mean by default
    gaps: interpolate                    # interpolate (default) | ffill | zero | error
    limit: 4                             # longest run of missing steps filled; more fails
  - dataset: ro_operation                # one number, not a series
    columns: [membrane_age]
    rename: {membrane_age: plant.ro.age}
    reduce: last                         # mean | last | first | min | max | median
  - dataset: ro_operation                # last year's July for this July's horizon
    columns: [price]
    rename: {price: costing.energy_prices.electrical}
    offset: -365D
fits: [...]                              # D19, unchanged
sweeps:
  - type: variable
    path: plant.ro.recovery
    values: [0.45, 0.5]
output_name: "recovery_{plant.ro.recovery}"
```

- **Where it runs.** The main process resolves every input once per distinct model horizon, before tasks are
  dispatched, and passes plain values as build overrides (the path `variable` sweeps already use; for flex-pse a
  `fix` element, or a costing field edit). Workers never read the store (D13). A sweep or case that sets the same
  path as an input is an error, not a silent override.
- **The model's time axis** comes from a new model-agnostic hook, `time_grid(spec) -> DatetimeIndex`, so the
  orchestrator doesn't read flex-pse specs itself. The flex-pse version builds the `time` element's
  `pd.date_range(start_date, periods=n, freq=time_step)`. A model without one takes only `reduce`d inputs.
- **Time zone: the model runs on the facility's local time.** We model flexible operation at the site the data
  comes from, so flex-pse's naive times are that site's wall clock. Stored timestamps are UTC (D6, and streams);
  `timezone` names the site's zone, and the data is converted to it and made naive before alignment. The default
  is UTC: data stored in UTC means a model in UTC. Across DST, the repeated autumn hour averages into one step and
  the missing spring hour is a gap, filled by `gaps`.
- **Alignment.** Take the window `[start, end)` of the horizon (shifted by `offset`), resample to `time_step` with
  `resample.agg`, fill gaps, check there is exactly one value per time point, convert `units`, and pass the list.
  `reduce` instead reduces the window to one number.
- **Gaps.** `interpolate` (the default) fills linearly in time, up to `limit` steps in a row. `ffill` carries the
  last value forward, `zero` writes 0, and `error` refuses any gap. A gap `interpolate` can't fill (before the first
  or after the last sample, or longer than `limit`) fails the input with the steps it couldn't fill.
- **Provenance.** The resolved values go into the result's `overrides` (D4), so an export rebuilds the model with no
  database (D17). A new `model_inputs` JSON input records, per entry, the selector as sent, what it resolved to (the
  dataset `run_id`, the result's run, the stream refs), the window, the alignment and a `data_hash`, as a fit run
  records its training data (D19).
- **Sweeping inputs** (`type: model_inputs`). Each value is a `label` and a set of `inputs` (entries as above), and a
  task takes one set. It combines with other sweeps (`combine: product` / `zip`) and in `cases` (a `model_inputs:`
  key per case) like any sweep, and `output_name` uses the label (`{feed}` → `line_a`), since selectors are too long
  for a name. An input from a sweep or case **replaces** the top-level entry for the same path; a `variable` sweep
  on a path that has an input is still an error. Each distinct set is resolved once, in the main process, and reads
  are cached per distinct selector, so 50 values means at most 50 reads before anything solves. Values that differ
  only in `offset` (or `start` / `end`) sweep the same streams over time windows.

  ```yaml
  sweeps:
    - type: model_inputs
      name: feed
      values:
        - label: line_a
          inputs:
            - {streams: [{source_id: historian, ref_name: FT_101}], rename: {"*": plant.feed.flow}}
            - {streams: [{source_id: historian, ref_name: TT_101}], rename: {"*": plant.feed.temperature}}
        - label: line_b
          inputs:
            - {streams: [{source_id: historian, ref_name: FT_201}], rename: {"*": plant.feed.flow}}
            - {streams: [{source_id: historian, ref_name: TT_201}], rename: {"*": plant.feed.temperature}}
        - label: last_july                 # the same stream, another window
          inputs:
            - {streams: [{source_id: historian, ref_name: FT_101}], rename: {"*": plant.feed.flow}, offset: -365D}
  combine: product
  output_name: "feed_{feed}_recovery_{plant.ro.recovery}"
  ```
- **Outside studies,** inputs are applied to a built model as a separate, explicit step:
  `model = orch.build(tag)`, then `orch.apply_model_inputs(model, entry)` (one entry or a list). It reads the
  model's own time steps (`time_grid` takes a built model too) and fixes the values through a new
  **`apply_values(model, values)` hook** (flex-pse: `flexops.apply_spec` with `fix` elements, checked against
  bounds, all or nothing). A model from `orch.build` remembers what was applied, so storing its results records
  the values in `overrides` and their provenance in `model_inputs`; applying a path again replaces it. A study still
  resolves inputs before building and passes them as build overrides, so its Ray workers get plain values.
  `orch.resolve_model_inputs(tag, [...])` returns the values and provenance without building.
- **Selectors stay** in `fitting/selectors.py`; model inputs call the same `load`, and turn its `FitError` into
  `ModelInputError`. (Moving them would only rename the module tests patch.)
- **Built** (M5.6) in `flex_orchestrator.inputs` (`models.py`: `ModelInput`, `InputSet`; `resolve.py`) and
  `flexpse/timegrid.py`, with these details settled:
  - Entries resolve in `study.task.prepare` (main process), against the spec the task builds, so a configuration
    sweep that changes the horizon is seen. The values go into `Work.overrides`; `TaskSpec` keeps the entries
    (`model_inputs`, `swept_inputs`), so a dry-run plan shows them.
  - A study run reads each distinct selection (selector and window) once, cached next to the spec cache.
  - A value with a unit is passed as `{"value", "units"}`, so flex-pse converts it to the variable's units; Pyomo's
    `m**3` is written `m^3`, which flex-pse's unit parser needs.
  - flex-pse's `fix` sets Vars only: a Param (a product's `delivery_min`, say) can't be a model input.
  - The provenance is the `model_inputs` JSON input of the result run, `ResultHandle.model_inputs`, and
    `model_inputs` in the D17 result file (imported back with the result).
- **Later:** an input baked into a derived spec as `$source` files (`orch.bind_data(spec_tag, model_inputs)`, like
  `configure`) for data that is part of the model rather than of a run.

**D12 — Python 3.12**, distribution `flex-orchestrator`, import `flex_orchestrator`, src layout, hatchling, pydantic v2.

**D20 — Code style: the same as flex-pse, enforced from the first commit.** **black** formats and **ruff** lints,
with the same rules and versions as flex-pse (`D:\github\flex-pse\pyproject.toml`, `.pre-commit-config.yaml`). Code
that moves between the two repos then needs no reformatting.

```toml
[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "D"]   # flex-pse's set, plus "D" (see below)
pydocstyle.convention = "google"

[tool.ruff.lint.per-file-ignores]
"**/tests/**" = ["D"]
"examples/**" = ["D"]
"docs/**" = ["D"]

[tool.black]
line-length = 88
target-version = ["py312"]                  # flex-pse uses py311; D12 fixes 3.12 here
```

- **One deliberate difference:** flex-pse sets `pydocstyle.convention = "google"` but doesn't select the `D` rules,
  so its docstrings aren't actually checked. Here `D` is selected for `src/`, because §8 relies on ruff to enforce
  docstrings on the public API.
- **Formatter:** black, not `ruff format`, to match flex-pse exactly. Ruff's `I` rule sorts imports, so isort is not
  needed.
- **Versions:** the same pre-commit revs as flex-pse (black `26.5.1`, ruff-pre-commit `v0.15.20`), with matching
  versions pinned in `environment-dev.yml` so the editor, pre-commit and CI agree. They're bumped together with
  flex-pse, in a PR that does nothing but bump them and reformat.
- **Enforcement:**
  - **pre-commit:** black, ruff and import-linter on every commit.
  - **pre-push:** `ruff check . && black --check . && lint-imports && pytest -q`, the same as flex-pse.
  - **CI:** the `lint` job runs the same three commands, and every other job depends on it.
- **Editor:** a committed `.vscode/settings.json` makes black format on save and ruff fix on save, using the
  `flex-orch-dev` interpreter.
- **Deliberately not used:** a type checker such as mypy or pyright, because flex-pse doesn't use one. It can be
  revisited after 0.1.0.

**D13 — Parallel execution: Ray only.** Acquirium already installs Ray (2.59), so the orchestrator supports Ray and
nothing else: no executor abstraction, no second backend.
- `max_workers=1` (the default) runs tasks in an ordinary loop in the same process, for debugging and unit tests.
- `max_workers > 1` goes through Ray.

```python
orch.execute_study(cfg, max_workers=8)                                  # Ray inside this machine / tenant container
orch.execute_study(cfg, ray_address="ray://tenant-a-ray-head:10001",    # the tenant's own Ray cluster (D14)
                   resources={"num_cpus": 1, "memory": 4e9})
```

```yaml
execution: {max_workers: 8, ray_address: null, task_timeout_s: 1800, retries: 1, on_error: continue}
```

- **One task function.**
  - `run_task(TaskSpec) -> TaskResult` is an importable, top-level function wrapped in `ray.remote`.
  - A `TaskSpec` is plain data: the spec, the overrides, the hook import strings, the export selection and the solver
    options.
  - **Pyomo models never cross a process boundary.**
- **The main process writes.** Workers return `TaskResult`s through Ray's object store, and the main process writes
  them with a `ray.wait` loop. This works in every connection mode, keeps D9 in one process, and means **workers
  never hold database or Acquirium credentials**.
- **Timeouts and failures.**
  - `ray.cancel(force=True)` at `task_timeout_s`; `max_retries` for crashed workers.
  - A failure only affects its own task (`experiment.fail`). Resume works through `spec_hash`.
- **Memory and threads.**
  - `max_calls` restarts workers after N tasks, to keep memory bounded.
  - `runtime_env` sets `OMP/MKL/OPENBLAS_NUM_THREADS=1`.
- **Progress and cancellation.** The `ray.wait` loop sends progress callbacks (the CLI progress bar now, Phase 2 job
  status later). Cancelling cancels every outstanding task.
- **Deferred:** reusing built models through a Ray actor pool, if the M5 benchmark shows build time dominates.
- **Isolation (D14): Ray is per tenant, never shared across tenants.** Ray workers run user hook code, so a shared
  cluster would let one tenant's code see another tenant's memory, files and network.
  - The default is local Ray inside the tenant's container, `ray.init(address="local", namespace="flexorch",
    include_dashboard=False)`.
  - Larger tenants can get their own Ray cluster (KubeRay `RayCluster` per tenant) on the tenant network.
- **Coexisting with Acquirium's Ray.** Acquirium runs in its own container in the tenant stack, so the two never
  share a Ray instance. In local mode, the separate namespace and explicit `ray.init` handle it. M5 checks this.

**Built** (M5) in `flex_orchestrator.parallel` and `study.task`:
- **Three steps per task.** `prepare` (main: check the tag, store the derived spec, read the spec and its files into
  a `Work`), `execute` (anywhere: materialize, build, solve, export; returns a `Done` with the outcome, the
  `ModelData`, the step times and the worker's memory) and `finish` (main: map units, store). `run_task` runs the
  three in-process; `parallel.run_tasks` runs `execute` as `ray.remote` tasks. Both store the same results, which is
  tested.
- **What crosses to a worker:** the `TaskSpec`, the hooks as import strings (checked before anything runs; a
  `__main__` function or lambda is refused), the export selection, the unit overrides, and the spec bundle (spec,
  asset refs, file bytes) `ray.put` once per `spec_hash`. Workers export with a unit mapper that has no resolver,
  and the main process maps every series' units again with its own (verified) mapper, so worker results need no
  Acquirium.
- **The loop.**
  - At most `max_workers` tasks are in flight.
  - A timeout is counted from submission, so it includes starting a worker: a few seconds the first time on
    Windows. A task past `task_timeout_s` gets `ray.cancel(force=True)` and is stored as a failed result with
    outcome `error` / `termination: timeout`. It isn't retried.
  - Ray's own `max_retries` is 0. One budget, `retries`, covers a dead worker, a raising build and a raising solve.
  - `on_error: stop` and a cancelled command cancel every running task. Results are returned in task order.
- **Options.** `execution.ray_address` (else `execute_study(ray_address=)`, else `Orchestrator(ray_address=)` /
  `FLEXORCH_RAY_ADDRESS`, else local Ray), `execution.resources` (`num_cpus`, `memory`) and `execution.max_calls`
  (passed to `ray.remote`, which is the only place Ray accepts it). The thread variables are set through
  `runtime_env`. A Ray the caller already initialized is used as it is.
- **Metadata.** Result runs record the hooks the task used (`flexorch_hooks`), including the study's build hook.
  `TaskResult.seconds` gives build, solve, export and store times.

**D14 — Tenancy, isolation and backups (option A: one stack per tenant).**

*Tenant = a group/project.* Users belong to one or more tenants with a role (viewer, editor, owner). A solo user is a
tenant with one member. With fewer than 100 users, that is roughly 10–50 tenant stacks, which is easy to manage.

*Per-tenant stack:*

```
             ┌───────────────── tenant-a network (internal) ─────────────────┐
gateway ───► │ flex-orchestrator (lib / Phase 2 API) ──► acquirium ──► PG db tenant_a (role tenant_a)
 (Phase 2)   │        │ local Ray (or per-tenant Ray cluster)   │                (shared PG cluster)
             │        └── volume: orchestrator scratch          └── volume: acquirium data_dir
             └───────────────────────────────────────────────────────────────┘
```

*Isolation guarantees (every one is tested, §7):*

| Layer | Guarantee | How |
|---|---|---|
| Database | One tenant's role can't connect to, read or write another tenant's database | One database and one role per tenant. `REVOKE ALL ON DATABASE tenant_x FROM PUBLIC`; `GRANT CONNECT` to `tenant_x` only. The tenant role is non-superuser, with no `CREATEDB` or `CREATEROLE`. The timescaledb extension is created by the provisioning admin. |
| DB secret | Only the tenant's Acquirium container holds the tenant's database password | A random password per tenant, generated at provisioning and mounted as a secret file. Users and the orchestrator never see it. Rotated by re-provisioning. |
| Graph / files | Each tenant's Oxigraph, artifacts and configs are on its own volume | One volume per tenant, mounted only into that tenant's containers. |
| Network | No route between tenants. Acquirium is unreachable from outside its tenant | One internal network per tenant (a Docker network, or a k8s namespace with a default-deny `NetworkPolicy`). Only the gateway can reach the orchestrator, and nothing outside can reach Acquirium or the database. The database is reachable only from tenant Acquirium containers, using their own credentials. |
| Compute | User hook code runs only on that tenant's compute | Ray per tenant (D13). Containers run as non-root, read-only root filesystem, no Docker socket (**note:** Acquirium's compose mounts `docker.sock`; the tenant stack must not). |
| Configs / specs / results | Everything lives in the tenant's own Acquirium | The orchestrator keeps no state shared across tenants. |
| Identity (Phase 2) | A request is served only if its token is valid for this tenant | D15: the orchestrator checks the tenant claim and role on every request. |

*Backups (Postgres is the main reason we use it):*
- **Cluster level:** pgBackRest or WAL-G with continuous WAL archiving, which gives point-in-time recovery for every
  tenant database. Backups are encrypted and stored off the host.
- **Tenant level:** the Acquirium `data_dir` volume is snapshotted on the same schedule. For a consistent pair, Oxigraph
  is quiesced or exported (`/export_graph`) at snapshot time, and the WAL position is recorded alongside it.
- **A nightly `pg_dump` for each tenant**, so one tenant can be restored or moved on its own.
- **Restoring one tenant:** restore its database (from PITR or the dump) and its volume to the same point. Start
  Acquirium and run `sync_metadata_graph`. Run the orchestrator's `verify` command (study counts, tag uniqueness,
  sample reads).
- **The restore is rehearsed:** a scripted restore drill into a scratch tenant is an exit criterion (M6) and is run
  every quarter.

*Lifecycle:*
- Provisioning (create the database, role, secret, volume and network, start the containers) and deprovisioning
  (final backup, then drop) are scripted in `deploy/tenant/`. The Phase 2 control plane calls the same scripts.
- Idle tenant stacks can be stopped with their volumes kept, and started again on demand.

### Phase 2: HTTP and integrations

**D15 — HTTP API and auth (no passwords in the orchestrator).**
- **Users, passwords, SSO and memberships live in the control plane**, using an OIDC identity provider (Keycloak,
  possibly in front of Stanford SSO, or a hosted one). The orchestrator stores **no users and no passwords**.
- **Every request carries a JWT.** It comes from the frontend login or from a personal access token for scripts
  (`flex-orch login`, the device-code flow). The tenant orchestrator checks:
  - the signature against the identity provider's public keys (JWKS);
  - `iss`, `aud` and `exp`;
  - a `tenant` claim equal to its own `FLEXORCH_TENANT`;
  - the `role` against the route: viewer for GET, editor for store/execute, owner for delete.
- The gateway routes `/<tenant>/…` to that tenant's stack, and the orchestrator checks the token **again**
  (defence in depth). It never trusts headers set by the gateway alone.
- The library client for a deployed tenant is `Orchestrator(url="https://flex.example.org/tenant-a",
  token=...)`, or the token is read from `FLEXORCH_TOKEN`.
- When the orchestrator runs with no control plane (a single lab server), it falls back to static hashed API tokens in
  `FLEXORCH_API_TOKENS`. There is still no password store.

**D16 — Control plane contract.** The multi-user control plane and UI are **separate projects**. This repo provides
what they need:
- a container image;
- `deploy/tenant/` provisioning and backup scripts;
- an env contract (`FLEXORCH_TENANT`, `FLEXORCH_OIDC_ISSUER`, `FLEXORCH_OIDC_AUDIENCE`, `FLEXORCH_ACQUIRIUM_URL`,
  `FLEXORCH_RAY_ADDRESS`);
- `/health` and `/ready` endpoints;
- an OpenAPI schema, from which a UI client is generated.

---

## 3. Target architecture

```
src/flex_orchestrator/
  __init__.py            # Orchestrator, StudyConfig, export_model_data
  orchestrator.py        # facade: studies, store/get, results, `acquirium` (the raw client)
  results.py             # D3 step 4 writer: ModelData -> declarations + run records, and read back
  errors.py              # StudyExistsError, StudyNotFoundError, TagExistsError, UnitError, DBAuthError, ...
  connection.py          # D11 modes: local / postgres / remote; env and *_FILE secrets, DSN, preflight, server lock
  frames.py              # polars/pandas conversion (D7)
  hooks.py               # BuildFn/SolveFn/ExportFn protocols, SolveOutcome, import-string resolution, registry
  specs.py               # spec hashing, canonical JSON, template overrides (format-agnostic)
  export/
    walker.py            # component discovery, key selection, identity-based time-set detection (pyomo only)
    units.py             # UnitMapper: pyomo → QUDT table, resolver chain, conversion
    model_data.py        # ModelData / ExportedSeries containers
  backend/
    base.py              # StorageBackend protocol: studies, variables, runs, records, deletion
    acquirium.py         # D9/D10 mapping onto ac.study / timeseries
    memory.py            # in-memory backend for unit tests (implements the same contract)
    files.py             # FileBackend: the same contract in a folder on disk, no Acquirium (M5.5)
  flexpse/               # optional default hooks (import guarded)
    build.py  solve.py  surrogate.py  regression.py   # regression.py: get_regressor hook over flexparameterize.regression
    timegrid.py          # time_grid hook: a spec's time points, before it's built (D21)
  study/
    config.py            # StudyConfig schema (sweeps, hooks, output_name template, export selection), load_config
    task.py              # TaskSpec / TaskResult; prepare (main) → execute (anywhere, no store) → finish (main); run_task
    runner.py            # expand (sweeps → tasks) and the execute_study command (fits, then the tasks: in-process or Ray)
  parallel.py            # D13: connect to Ray, the ray.wait loop (timeouts, retries, cancellation), map_ordered for fits
  archive/               # D17 study export / import (named so it doesn't clash with export/, the model-data exporter)
    models.py            # pydantic models for flexorch-export/1 (study, spec, dataset, fit, result) → JSON Schema
    export.py            # export_study / export_result: streaming, temp-folder-then-rename, checksums
    load.py              # read_json, load_result (no DB)
    imports.py           # import_study (checks schema + checksums, re-creates via the store paths)
  fitting/               # D19 fit_surrogate command (core)
    models.py            # Selector, Prep, Validation, Variant, FitParams (JSON), FitRecord, FitSummary
    selectors.py         # selectors → frames + units + provenance (dataset, study, result, ref_uris, streams, resolve, read-only sparql)
    prep.py              # align, resample, gap policy, max_rows, holdout split, data hash
    regress.py           # regressor resolution (SurrogateType | import string), fit, predict, metrics
    store.py             # fit runs in <s>::fits
    command.py           # the fit_surrogate command: variants, tag checks, store fit run + spec
  inputs/                # D21 model inputs
    models.py            # ModelInput (a Selector plus alignment), InputSet (a model_inputs sweep value)
    resolve.py           # entries -> build overrides + provenance: zone, offset, resample, gaps, reduce
  ext/                   # D18 (core, no FastAPI)
    commands.py          # @command, Command, CommandRegistry, Progress, the command run ledger
    plugins.py           # Plugin, entry-point discovery, FLEXORCH_PLUGINS allowlist, api_version check, collisions
    events.py            # lifecycle events + subscription
  schemas.py             # the JSON Schemas published in docs/schemas/
  testing/               # D18 helpers for plugin authors (memory_orchestrator, run_command_in_test; Phase 2: make_test_app, token_for)
  ops/
    verify.py            # post-restore / health verification of a tenant store
  cli.py                 # flex-orch run | cmd <name> | commands | export | import  (built, M3/M4); ls | query | verify later; Phase 2 adds serve, login
  server/                # Phase 2 only
    app.py               # create_app(settings, plugins) factory — no module-level app
    deps.py              # get_orchestrator, current_principal, require_role, get_jobs, get_settings (public for plugins)
    extensions.py        # mounts plugin routers under /ext/<plugin> with auth dependencies, reserved prefixes, /commands/{name}
    auth.py  jobs.py  routes/
deploy/
  docker/                # image (Phase 1, M6)
  tenant/                # provision / deprovision / backup / restore scripts + compose template (M6)
  ray/                   # per-tenant Ray cluster template (M5)
  gateway/               # Phase 2 reference gateway config (M8)
```

Layering rules, enforced with import-linter:
- **Nothing outside `server/` imports `server/`, FastAPI or JWT libraries.** The core library has no HTTP in it.
  `ext/` holds plugin `routers` without touching them; only `server/extensions.py` imports and mounts them.
- `export.walker` depends only on pyomo.
- `specs`, `study.config` and `hooks` don't import pyomo, Acquirium or flex-pse.
- `flexpse/` is the only package that imports flex-pse.
- `backend/acquirium.py` is the only module that imports Acquirium.

---

## 4. Public API (target)

Phase 1, the library:

```python
from flex_orchestrator import Orchestrator, export_model_data

orch = Orchestrator(acquirium_url="http://acquirium:8000", frame="pandas")   # inside a tenant stack
# or Orchestrator() for local DuckDB, or Orchestrator(db="postgresql://host/tenant_a", data_dir=...) for dev Postgres
orch.create_study("example_a")            # raises StudyExistsError if present
orch.select_study("example_a")            # raises StudyNotFoundError if absent

orch.store_data("biogas_data", df, units={"biogas_volume": "m3/day", "flow_in_feed": "m3/day"})
X = orch.get_data("biogas_data", columns=["flow_in_feed"], frame="pandas")

orch.store_model_spec("base_model", spec_dict)                         # TagExistsError on repeat
orch.store_surrogate_spec("biogas_h1", reg.to_surrogate_spec())        # a spec fitted elsewhere, or:
orch.fit_surrogate(tag="biogas_{name}", regressor="multilinear",       # D19: fit from Acquirium data
                   X={"dataset": "biogas_data", "columns": ["flow_in_feed"]},
                   y={"dataset": "biogas_data", "columns": ["biogas_volume"]},
                   variants=[{"name": "h1", "window": H1}, {"name": "h2", "window": H2}])
spec = orch.apply_surrogate("base_model", unit="plant.digester", surrogate="biogas_h1",
                            relation="biogas_relation")
orch.store_model_spec("model_with_surrogate_biogas_h1", spec, parent="base_model")

m = my_build(spec); outcome = my_solve(m)                              # user's own build/solve
orch.store_model_results("model_with_surrogate_biogas_h1", m, outcome, include=["costing.*", "plant.digester.*"])

for r in orch.stored_model_results():                                  # lazy ResultHandles
    df = r.to_frame("polars"); r.tag; r.metadata; r.units

ac = orch.acquirium                                                    # D8: Acquirium's own API, no wrappers
ac.study.get("example_a::runs").experiments.where(metadata={"flexorch_kind": "result"}, status="succeeded").frame()
orch.delete("result", "model_with_surrogate_biogas_h2")                  # deletes it for good (D9)
orch.export_study("exports/example_a")                                 # D17: folder of JSON specs/data/results
orch.import_study("exports/example_a", name="example_a_copy")
orch.execute_study(cfg, build=my_build, solve=my_solve, max_workers=8) # Ray (a built-in command)

orch = Orchestrator(plugins=["digester_tools"])                        # D18: plugins, commands
orch.run_command("price_sweep", prices=[0.1, 0.2], base_tag="base_model")
```

Study config:

```yaml
base_model_spec: base_model
model_inputs:                            # D21: model variables set from stored data, same selectors as fits
  - {dataset: biogas_data, columns: [flow_in_feed], rename: {flow_in_feed: plant.digester.flow_in}}
  - {streams: [{source_id: historian, ref_name: TT_200}], rename: {"*": plant.feed.temperature},
     timezone: America/Los_Angeles, gaps: interpolate}
fits:                                    # D19: optional, runs before sweeps
  - tag: "biogas_{name}"
    regressor: multilinear               # 0.2 types: multilinear, constant_intensity (arima after U8)
    X: {dataset: biogas_data, columns: [flow_in_feed]}
    y: {dataset: biogas_data, columns: [biogas_volume]}
    prep: {resample: {freq: 1h, agg: mean}, gaps: interpolate, limit: 3}
    variants:
      - {name: h1, window: {start: 2025-01-01, end: 2025-06-30}}
      - {name: h2, window: {start: 2025-07-01, end: 2025-12-31}}
hooks:
  build: "mypkg.models:build"
  solve: "mypkg.models:solve"
  apply_surrogate: "flex_orchestrator.flexpse:apply_surrogate"
sweeps:
  - type: surrogate
    unit: plant.digester
    relation: biogas_relation
    surrogates: [biogas_h1, biogas_h2]
  - type: configuration_option            # how the model is built: a construction option or element field
    name: price
    path: costing.energy_prices.electrical
    values: [0.1, 0.2, 0.3]
  - type: variable                        # a model variable, fixed to each value
    path: plant.digester.temperature
    values: [35, 40]
  # also: spec_list (a stored spec per task), model_inputs (a labelled set of D21 inputs per task) and
  #   cases (named combinations: {name, spec, surrogates: {unit: tag}, configuration_options: {...},
  #   variables: {...}, model_inputs: [...]})
combine: product                         # product (nested) | zip (in step)
export: {include: ["costing.*", "plant.digester.*"], strict_units: true}
output_name: "model_with_surrogate_{surrogate}_price_{price}_{plant.digester.temperature}"
on_existing: error                       # error | skip (resume) | replace (write, then delete the old)
execution: {max_workers: 8, task_timeout_s: 1800, retries: 1, on_error: continue}
```

Phase 2, the HTTP API (every route needs a valid tenant JWT, D15):

| Method | Path | Min role | Purpose |
|---|---|---|---|
| GET/POST | `/studies`, `/studies/{s}` | viewer / editor | list / create (409 if it exists) |
| POST | `/studies/{s}/execute` | editor | submit StudyConfig → `202 {job_id}` |
| GET | `/jobs/{id}` · DELETE `/jobs/{id}` | viewer · editor | status, progress, errors · cancel |
| GET | `/studies/{s}/results` | viewer | all model results as JSON: `{tag: {metadata, units, index, columns: {key: [...]}}}` (`?tags=`, `?keys=`) |
| GET/POST | `/studies/{s}/specs/{kind}/{tag}` | viewer / editor | read / store specs (409 on duplicate) |
| POST | `/studies/{s}/data/{name}` · GET | editor · viewer | upload / read datasets |
| DELETE | `/studies/{s}/{kind}/{tag}` | owner | delete (D9) |
| GET | `/studies/{s}/export` · `/studies/{s}/results/{tag}/export` | viewer | the D17 export as a zip download · one result JSON |
| POST | `/studies/import` | editor | upload a D17 export zip → `202 {job_id}` |
| POST | `/studies/{s}/surrogates/fit` | editor | alias of `POST /commands/fit_surrogate` for that study (D19); the same code path |
| GET | `/commands` | viewer | list available commands (built-in + enabled plugins) and their parameter schemas |
| POST | `/commands/{name}` | the command's `role` | run a command (D18): long-running → `202 {job_id}`, otherwise the result |
| * | `/ext/<plugin>/…` | set by the plugin (auth always applies) | plugin routers (D18) |
| GET | `/health`, `/ready` | none | liveness, plus Acquirium connectivity |

---

## 5. Milestones

Each milestone ends when its exit criterion is met, its **pytest suite passes in CI** (§7), and an example script
under `examples/` runs from start to finish.

**Built so far:** `examples/ro_surrogates/example.py`, one script with no options, on a local Acquirium. It stores
synthetic RO data generated from a parabolic specific-energy curve, builds a tariff-priced feed → RO → tank →
product model in Python, emits its spec (`emit_model`) and stores it, fits an average intensity
(`ConstantIntensityRegressor`) and a linear power surrogate (`LinearRegressor`), solves the model with each, and
compares each model's RO energy with the true curve. `examples/ro_study/` runs the same model as `StudyConfig`
dicts, one script per kind of sweep: `sweep_surrogates.py` (`fits` with the built-in regression, then a surrogate
sweep; it reproduces `ro_surrogates` exactly and is the M4 "script 3"), `sweep_variables.py`,
`sweep_configuration.py` (the feed's `max_withdrawal`), `sweep_nested.py` (surrogate × withdrawal; stores the full model, exports the study and checks every value read back) and
`sweep_cases.py`; `store_on_disk.py` runs the surrogate sweep on the file backend (M5.5) and `compare_ray.py`
times a month-long nested sweep in-process against Ray. `examples/plugins/ro_tools/` is the sample plugin.
`test_examples.py` runs them all except `compare_ray.py`, which takes minutes (integration tier).

### Phase 1: core library

#### M0 — Repo scaffold, environments, test harness (≈ 1 wk)
- `pyproject.toml` (hatchling; extras `flexpse`, `pandas`, `dev`; the `server` extra is added in M7), src layout,
  README, CHANGELOG.
- `environment-dev.yml` / `environment.yml` (**drafted**, see §6).
- pytest setup (§7): tier markers, shared fixtures, coverage configuration.
- Code style per D20:
  - `[tool.black]` / `[tool.ruff]` in `pyproject.toml`;
  - `.pre-commit-config.yaml` running black, ruff and import-linter, plus the pre-push suite (import-linter includes
    the rule that nothing imports `server/`);
  - `.vscode/settings.json`;
  - `CONTRIBUTING.md` with the "run it before submitting" rule from flex-pse.
- GitHub Actions: `lint` (`ruff check .`, `black --check .`, `lint-imports`) → unit (3.11–3.13, ubuntu + windows) →
  component, plus `docs.yml`.
  **Built** as `.github/workflows/ci.yml`, on PRs, pushes to `main`, weekly and by hand:
  - `lint` installs only the pinned tools and the package (no dependencies);
  - every other job needs it and builds `environment-dev.yml` with micromamba (cached), so CI uses the
    developer env's pins;
  - `unit` (`-n auto`) and `component` run on ubuntu + windows, `integration` on ubuntu;
  - Python is the env's 3.12 only, since the package needs `>=3.12`;
  - not yet: `docs.yml` (no Sphinx project yet), the coverage floor, and benchmarks (opt-in).
- The docs skeleton (§8): `conf.py`, `index.rst` with the flex-pse toctree captions, `_ext/flexorchdoc.py` (an empty
  shell), the `installation` and `architecture_and_layering` pages, and deployment to GitHub Pages.
- **Exit:** the env builds, `pytest` collects and passes a smoke test, the docs build with `-W`, and CI passes.
  `pre-commit run --all-files` is clean, and a deliberately misformatted file fails both the hook and the CI `lint`
  job.

#### M1 — Storage backend, connections, specs (≈ 1.5 wks)
- The `StorageBackend` protocol, plus the memory and Acquirium backends (D10): **built**, including the `file`
  kind (`FileValue`, an Acquirium artifact) and `studies()` that specs need.
- `connection.py` (D11): **built**: the three modes, credentials from env or secret file as `SecretStr`, checks
  before starting, the server lock, `recreate` never set, redaction. Left: a run against a real TimescaleDB.
- Study create, select and list (`orch.studies()`) with the D9 guards. Tagged stores for specs and data
  (`units=`, polars or pandas input): **built**.
- Referenced spec files (D2): finders, assets stored as `file` inputs, `spec_hash` covering asset hashes, and
  `materialize_spec` / `build` for replay: **built**.
- D9 uniqueness, deletion and `replace=True`: **built** for results, datasets and specs.
- `frames.py` and result handles (D7): **built**.
- **Exit:**
  - Script 1 up to `store_model_spec` works in modes 1, 2 and 3.
  - A spec with a tariff file, stored and then rebuilt from another working directory, gives the same model; editing
    the tariff file changes `spec_hash`.
  - A wrong password gives `DBAuthError`, and the password never appears in logs or metadata.
  - The contract and duplicate tests pass.
- **Status:** met apart from one item, checked on 2026-10-22 with quickstart scripts 1 and 2 in modes 1 and 3
  (since removed for the simpler `examples/ro_surrogates/`, so modes 2 and 3 are no longer run by an example); the tariff
  spec rebuilds from another directory to the same model and an edited tariff changes the hash (both backends);
  `DBAuthError` is checked against a real Postgres. **Open:** mode 2 end to end, which needs a TimescaleDB-enabled
  Postgres (CI, or a Linux/WSL host); the test is in place behind `FLEXORCH_TEST_DB`.

#### M2 — Model data exporter, units, results (≈ 3 wks)
- `export.walker`: key and pattern selection, identity-based time-set detection, product sets split into time and
  custom factors, and `name[idx]` scalars.
- `export.units`: the mapping table, resolver fallback, overrides, strict mode, and converting to the declared unit.
- The writer (D3 step 4), `store_model_results`, `solve_and_store`, `stored_model_results` and the
  `ResultHandle` frames: **built** (D4, D7).
- Default flex-pse hooks and `SolveOutcome`: **built** (D1).
- The D5 benchmark: **built** and measured (D5); 1,000 keys waits on U11.
- **Exit:** scripts 1 and 2 run with the flex-pse hooks and with a user-written build/solve pair. Units round-trip.
- **Status: exit met** on 2026-10-22: quickstart scripts 1 and 2 ran with flex-pse's hooks and with a plain Pyomo
  build/solve pair; script 2 checked each key's unit, converted kW to W, and re-solved each result from its stored
  spec and overrides. The scripts were then removed for the simpler `examples/ro_surrogates/`. The user-hook path
  is still covered by the component tests; an end-to-end run of it isn't.

#### M3 — Study export/import (≈ 1.5 wks)
- `archive/` (D17):
  - the `flexorch-export/1` pydantic models, with their JSON Schemas published to `docs/schemas/`;
  - `export_study` / `export_result` (streaming, temp folder then rename, checksums, optional gzip);
  - `load_result`, and `import_study`;
  - CLI `export` / `import`.
- **Exit:**
  - Script 1's study exports to a folder that matches the D17 layout and validates against the published schemas.
  - Importing it under a new name and exporting again gives files identical to the first export, apart from the
    provenance fields (run ids and timestamps).
  - `load_result` rebuilds the same frames and units with no database.
- **Status: exit met.** The scripts 1 and 2 the exit names were removed with the quickstart, so the exit is checked
  on stand-ins (`archive/tests/test_archive.py`, both backends): a study with a spec and its file, a derived spec, a
  dataset, a fit with its series and results (one covering part of time, with a stale value and a non-string index)
  exports to the D17 layout and every file validates against the published schemas (`jsonschema`); importing it
  under a new name and exporting again gives the same files apart from provenance (run ids, timestamps, the study
  name, `flexorch_imported_from`, `fit_run_id`), and byte-identical asset files; `load_result` gives the same frames,
  scalars and units as the stored result. Checksums, schema versions and spec hashes are checked on import. The CLI
  exports and imports in the plugin's integration test.

#### M4 — Declarative studies (`execute_study`), commands/plugins, regression (≈ 4 wks)
- `StudyConfig` plus its JSON schema, hook import strings, sweeps (`surrogate`, `variable`,
  `configuration_option`, `spec_list`, `cases`; product or zip), `output_name`, `on_existing`, and `export`.
- `--dry-run`, `study/task.py`, and the in-process loop (`max_workers=1`) with resume.
- CLI: `flex-orch run study.yaml [--workers N] [--ray-address ...]`.
- **Extension API, core part (D18):**
  - `ext/`: `@command`, the registry, `run_command`, `Progress`, and the command run ledger;
  - `Plugin`, entry-point discovery, the `FLEXORCH_PLUGINS` allowlist, the `api_version` check and collision checks;
  - lifecycle events, and the stable methods for subclasses;
  - `execute_study` re-implemented as a built-in command;
  - `flex-orch cmd <name>`, with options generated from the params model;
  - the `flex_orchestrator.testing` helpers;
  - the sample plugin `examples/plugins/digester_tools/`.
- **Regression command (D19):**
  - `fitting/`: the selectors (all six kinds), preparation, regressor resolution (flex-pse `SurrogateType` or an
    import string), `variants`, optional holdout validation;
  - the surrogate types of the pinned flex-pse only: `multilinear` and `constant_intensity` for 0.2;
  - storing the spec plus a fit run in `<s>::fits` with provenance and the data hash;
  - `fits:` in `StudyConfig`;
  - the `flexpse/regression.py` hook;
  - D17 export and import of `fits/`.
- **Exit:**
  - The pseudocode's fit-then-sweep workflow runs as one `StudyConfig`, with 0.2's `multilinear` surrogates. It
    fits two variants from a stored dataset, swaps each into the digester's `biogas_relation` and solves. A
    `constant_intensity` fit of a pump runs in the same config.
  - ARIMA is **not** an M4 exit criterion. It's a follow-up when U8 lands (see the upstream track).
  - Running it again with the same data gives the same `data_hash` and identical specs.
  - Script 3 runs in-process. `on_existing: skip` does nothing new on a rerun, and `on_existing: error` raises.
  - The sample plugin, installed with pip, loads only when it is in the allowlist. Its command gives the same result
    from Python and from the CLI.
  - An orchestrator subclass provided by the plugin overrides `before_store` and `default_export_selection`.
- **Status: exit met, apart from what M5 brings.** Checked by:
  - `study/tests/test_study_flexpse.py` (both backends, HiGHS): one `StudyConfig` fits two `multilinear` variants
    of the RO's power from two windows of a stored dataset and a `constant_intensity` one, swaps each into the RO
    and solves; the fits come out as generated (2.8 and 3.4 kWh/m³) and the costs order accordingly. Refitting the
    same data gives the same `data_hash` and spec hashes. The exit's digester and pump are stand-ins from the
    pseudocode; the RO plays both parts here. A `configuration_option` sweep sets the feed's `max_withdrawal`
    and the tank's `max_volume.value`, and another sweeps a price (the costing element).
  - Script 3 is `examples/ro_study/sweep_surrogates.py`, in-process. `on_existing: skip` does nothing new on a rerun, and
    `on_existing: error` raises before anything runs (tested in both suites).
  - The sample plugin, pip-installed into a temporary folder, loads only when `FLEXORCH_PLUGINS` lists it; its
    `cost_table` gives the same result from Python and from `flex-orch cmd`; its subclass's `before_store` and
    `default_export_selection` apply, and its own tests pass (`test_examples.py`).
  - **Not yet:** `max_workers > 1`, `--workers N > 1` and `--ray-address` (Ray, M5) raise a clear error;
    `task_timeout_s` is recorded but only enforced on Ray; the `solver events` log (D10).

#### M5 — Ray parallel execution (≈ 1–1.5 wks)
- Ray dispatch in `runner.py`:
  - `ray.remote(run_task)`, with `num_cpus`, `memory`, `max_retries` and `max_calls`;
  - a `ray.wait` loop that enforces timeouts (`ray.cancel(force=True)`), writes results and sends progress events;
  - cancellation;
  - thread pinning through `runtime_env`;
  - `namespace="flexorch"`;
  - parallel fitting of `fit_surrogate` variants (D19), with the prepared data shared through `ray.put`.
- `deploy/ray/`: a **per-tenant** Ray cluster template.
- **Benchmark:** 30 tasks (3 surrogates × 10 prices) on one machine and on 2 nodes, on Windows and Linux. Record the
  overhead per task, build versus solve time, write rate, memory per worker, and whether Ray coexists with
  Acquirium's Ray. Write it up in `docs/decisions/0001-ray-execution.md`.
- **Exit:**
  - The same stored results with `max_workers=1` and `max_workers=8`.
  - A hung task is killed at its timeout, and the sweep continues.
  - At least 5× faster than in-process with 8 workers.
  - The example runs against a 2-node per-tenant Ray cluster.
- **Status: exit met on one machine; the 2-node run is waiting.** See D13 "Built" and
  `docs/decisions/0001-ray-execution.md`.
  - **Same results:** `study/tests/test_parallel.py` (1 vs 4 workers) and the benchmark (1 vs 8, every stored
    value compared).
  - **Hung task:** killed at its timeout, stored as a failed result, and the sweep goes on (`test_parallel.py`).
    Retries, `on_error: stop`, cancellation, `max_calls` restarts, thread variables, 50-task uniqueness and
    parallel fits are tested there too.
  - **Speed:** 5.6× with 8 workers. The benchmark is the RO plant over a month at 15-minute steps, 3 surrogates ×
    10 withdrawal limits, on Windows with local Ray: 423 s serial, 75 s with 8 workers.
  - **Deviation:** the benchmark sweeps feed withdrawal limits, not prices, because the RO model is priced by a
    tariff file.
  - **Not yet:**
    - The 2-node cluster and the Linux benchmark. Ray has no multi-node support on Windows and this machine has
      no Docker; `deploy/ray/` (compose and KubeRay templates) is written but not run. They're picked up with
      the M6 tenant stack.
    - An actor pool reusing built models stays deferred (build is about 40% of a task, but sweeps that change
      the model can't reuse one).

#### M5.5 — File backend: studies on disk without Acquirium (≈ 0.5 wk)
Inserted after M5. Users who don't want to run Acquirium can still keep studies durably on disk. Before this,
the only way was the memory backend plus `export_study` at the end, which loses everything on a crash, rewrites the
whole folder on each export, and needs a full `import_study` to resume.
- **`backend/files.py`: `FileBackend(path)`** implements `StorageBackend` on a folder, so everything above the
  backend works unchanged: studies, fits, sweeps, Ray (the main process stores, as with Acquirium), export, import,
  resume with `on_existing: skip`, deletion guards.
- **Layout:** it mirrors the backend contract, not the D17 export layout. The export layout is built from the
  orchestrator's meaning of each ledger, and a backend mustn't depend on that (§3 layering). `export_study` still
  writes the portable copy.
  ```
  flexorch-files.json                 # {format: "flexorch-files", version: 1}; an unmarked non-empty folder is refused
  studies/<safe name>-<sha256[:8]>/   # one per backend study; the hash keeps names that differ by case apart
    study.json                        # the exact name and every declaration, in order
    runs/<run_id>/run.json            # status, seq (start order), metadata, started_at, error, records in order;
                                      # scalar and JSON values inline
    runs/<run_id>/<n>.parquet|.bin    # a timeseries (time, value) or table as Parquet; a file's bytes
  ```
- **Durability:** every file is written to a temporary name and renamed (retrying a Windows `PermissionError`,
  the helper shared with the export), so a crash loses only the run in progress; that run stays `running`, as on
  Acquirium. Deletion is real: the directory is renamed aside, then removed; leftovers go on the next open.
- **Limits:** one process writes a folder at a time; the index is read when the folder is opened and kept in
  memory. No unit vocabulary (`resolve_unit` is `None`, as in the memory backend), no stream store for the
  `ref_uris` / `streams` / `resolve` / `sparql` fit selectors, and no `orch.acquirium`.
- **Selection:** `Orchestrator(FileBackend(path))` and `flex-orch --files DIR`.
- **Exit:** the contract tests and every backend-parametrized orchestrator test pass on it (the `backend` fixture
  runs memory, files and Acquirium); reopening returns everything recorded; an example runs end to end.
- **Status: built.**
  - `test_contract.py` and the orchestrator, archive and study tests run on `files` in the unit tier;
    `backend/tests/test_files.py` covers reopening, order across reopenings, a run cut short, crash leftovers, no
    temporary files, deletion on disk, case-clashing names, foreign and newer folders, non-JSON metadata, and a
    study run, reopened, resumed and exported. `test_cli.py` exports and imports with `--files`.
  - `examples/ro_study/store_on_disk.py` (in `test_examples.py`).
  - **Store time per result:** about 45 ms on files against about 580 ms on a local Acquirium, for
    `sweep_nested` with the full model or scalars only. Storing is the serial part of a Ray study (M5), so Ray
    gains more here: `compare_ray.py`'s 24 month-long solves with 10 workers took 89.5 s in-process and 39.2 s on
    Ray (2.3×) on files, against 112 s and 67 s (1.7×) on Acquirium.

#### M5.6 — Model inputs from stored data (≈ 1 wk)
D21.
- ~~Move the selectors to `selection`~~ (they stay in `fitting`; see D21).
- The `time_grid` hook and its flex-pse version.
- `ModelInput` (a `Selector` plus `timezone`, `resample`, `gaps`, `limit`, `reduce`, `offset`) and the resolver,
  with unit tests on memory and files: zones and DST, resampling, each gap rule, `reduce`, `offset`, units, a
  length that doesn't match, a path set twice.
- `model_inputs` in `StudyConfig`, `orch.build` and `orch.resolve_model_inputs`; the `model_inputs` provenance
  input; the published schemas.
- The `model_inputs` sweep type and a case's `model_inputs:`: replacing the top-level entry for a path, labels in
  `output_name`, one read per distinct selector, and the error for a `variable` sweep on an input's path.
- An example: ~~the RO plant's product demand taken from its stored dataset~~ (its demand is a Param, which
  `fix` can't set), so instead the schedule the plant ran: the RO's permeate fixed to the stored operating data.
- **Exit:** a study whose demand comes from a dataset and whose feed temperature comes from a stream runs, on
  memory, files and Acquirium, and its export rebuilds each task's model with no database. A `model_inputs` sweep
  over two sets of streams gives one result per set, each recording the streams it used.
- **Status: built.**
  - `inputs/tests/test_inputs.py` (memory and files): hourly data onto 15-minute steps, a unitless column, the
    facility's zone, both DST days, resampling to a coarser step, each gap rule, gaps that can't be filled,
    `reduce` with and without time steps, `offset`, units, a path set twice, swept entries replacing, a selector
    that matches nothing, one read per selection, and invalid entries.
  - `inputs/tests/test_inputs_study.py` (memory and files): a study with `model_inputs` and a `model_inputs`
    sweep, a case, the clash with a `variable` sweep, a failing input, the dry-run plan, export and import,
    `orch.build` and `orch.resolve_model_inputs`, labels and the schema.
  - `inputs/tests/test_inputs_flexpse.py` (solved with HiGHS): `time_grid` matches the built model; the demo's
    pump flow from a dataset over two windows (memory, files and Acquirium); and on Acquirium from two historian
    streams, by `source_id` / `ref_name`, each result recording its stream ref.
  - `test_parallel.py`: inputs reach Ray workers as values, with the same results as in-process.
  - `examples/ro_study/model_inputs.py`: the optimized schedule costs $1,296.47; the one the plant ran (June's
    stored permeate) $2,580.02.

#### M6 — Single-tenant deployment and backups (≈ 2 wks)
- **Image** (`deploy/docker/`): micromamba plus `environment.yml` plus `idaes get-extensions`. Runs as non-root.
  It is used for the orchestrator and the Ray nodes.
- **Tenant template** (`deploy/tenant/`): a compose template for one tenant stack (orchestrator container, Acquirium
  container, per-tenant network and volumes) pointing at a shared Postgres cluster. There is no `docker.sock`
  mount, and secrets come from secret files. A shared `compose.cluster.yaml` for the Postgres cluster plus its backup
  agent.
- **Provisioning scripts:** `provision_tenant.sh <name>` and `deprovision_tenant.sh <name>`:
  - create the database and role, `REVOKE ... FROM PUBLIC`, and create the extension as admin;
  - generate a random secret;
  - create the volume and network;
  - start the stack.
  Deprovisioning takes a final backup before dropping anything.
- **Backups:**
  - pgBackRest (or WAL-G) for the cluster, with WAL archiving;
  - a nightly `pg_dump` per tenant;
  - per-tenant volume snapshots, with the Oxigraph export taken at the same point;
  - `restore_tenant.sh <name> <timestamp>`, and `flex-orch verify`.
- **Exit:**
  - Two tenants provisioned on one cluster pass the **isolation suite** (§7): database, network, volume and secret.
  - **Restore drill:** tenant A is deleted, then restored to a scratch tenant at a chosen point in time, and
    `verify` passes with the same studies, tags and sample values.
  - Scripts 1–3 run inside a tenant stack, in mode 3.
  - `0.1.0` (the library) is released.
- **Status: built, apart from the release** (`0.1.0` is tagged and published by hand). Choices made:
  - **Image** `deploy/docker/Dockerfile`: `mambaorg/micromamba:2.3.3`, `environment.yml` (which now lists `git`,
    for its pinned git installs), `idaes get-extensions`, this package; user `flexorch` (uid 1000), the env on
    `PATH` so `docker exec` and health checks work, and every stack container read-only.
  - **Cluster** `timescale/timescaledb-ha:pg17.6-ts2.22.0` (pgBackRest 2.56 included) and **SeaweedFS 3.97** as the
    local S3 stand-in: MinIO stopped publishing images. pgBackRest needs S3 over TLS, so `init_cluster.sh` makes a CA
    and a certificate; the repository is encrypted (AES-256). A fresh SeaweedFS can't read the first bucket until
    its gateway restarts; `init_cluster.sh` checks a write reads back and restarts it once.
  - **Networks:** each tenant has an internal network and an internal `flexorch_<t>_db` network that Postgres
    joins; no tenant is on another's, and the orchestrator is on no database network. `pg_hba.conf` allows a role
    only its own database (`sameuser`), and the admin only over the socket.
  - **The password** is a libpq password file (`PGPASSFILE`), mounted only into the tenant's Acquirium: it is in no
    environment variable, DSN or command line. `psql` gets it on stdin.
  - **Tenant backups** stop the tenant's Acquirium for the dump and volume copy, so they match; the graph export is
    taken just before. A point-in-time restore runs pgBackRest into a throwaway Postgres (the whole cluster when
    the database is newer than the last full backup) and dumps the tenant from it. Timescale's job catalog names
    the source role, so the restore creates a stand-in for a deleted tenant and hands the jobs to the new one.
  - **`flex-orch verify`** (`flex_orchestrator.verify`): per study and kind, run counts by status, tags and value
    hashes of a sample; problems (a tag stored twice, runs left running, unreadable values); `--against` a
    report exits 1 on any difference.
  - **Mode 3 by default:** `Orchestrator()` with no store uses `FLEXORCH_ACQUIRIUM_URL` (D16), so scripts run
    unchanged in a stack. The quickstart scripts 1–3 are gone; `deploy/tenant/smoke.py` (data, spec, a
    `model_inputs` sweep, HiGHS solves) is what runs there.
  - **Tests** `tests/test_deploy.py`, opt-in with `FLEXORCH_TEST_DEPLOY` (a command prefix such as
    `wsl.exe -d Ubuntu --` on Windows): the smoke study in mode 3; database (sameuser, no CONNECT for PUBLIC, role
    attributes), network (no route to another tenant's Acquirium by name or address, none to Postgres from the
    orchestrator, none out), volume and secret isolation; uid 1000, read-only root, no capabilities, no Docker
    socket; and the restore drill. All pass (about 4.5 minutes).
  - **CI:** `.github/workflows/deploy.yml` builds the image (with GitHub's build cache) and runs these tests on
    `ubuntu-latest`, when the deployment's files change, weekly and by hand, with throwaway secrets; container logs
    are uploaded on failure. Its own workflow, so it doesn't hold up CI.
  - **Not done:** the isolation suite with a hostile plugin (M7, with the server), and the `[server]` extra's code
    (M7; the extra itself is in `pyproject.toml`).

### Phase 2: HTTP and integrations

#### M7 — HTTP API, auth, and extensible app (≈ 2.5–3 wks)
- `server/`: a FastAPI app factory and the §4 routes:
  - a job queue (one runner per tenant; it runs `execute_study` on the tenant's Ray);
  - results JSON with pagination;
  - 409 on duplicates, 202 plus a job id for anything long.
- `auth.py` (D15): JWT checks against JWKS, the tenant claim and role per route; the static-token fallback.
  `Orchestrator(url=, token=)` client mode, and `flex-orch serve` / `flex-orch login`.
- **Extension API, HTTP part (D18):**
  - the `create_app(settings, plugins)` factory (uvicorn `--factory`, no module-level app);
  - the public `server/deps.py`;
  - `server/extensions.py`, which mounts plugin routers under `/ext/<plugin>` with auth, tenant and audit dependencies
    on the router, reserves the core prefixes, checks for collisions and adds an OpenAPI tag per plugin;
  - `GET /commands` and `POST /commands/{name}`, on the same job path as `/execute`;
  - `make_test_app` and `token_for` in `flex_orchestrator.testing`;
  - the sample plugin gains a router.
- **Exit:**
  - Script 4 runs against one tenant stack with a test identity provider (a Keycloak container).
  - A token for tenant B is refused by tenant A (403).
  - A viewer token can't execute, and an editor token can't delete.
  - `POST /commands/fit_surrogate` and the `/studies/{s}/surrogates/fit` alias store **the same** specs and fit
    runs as the Python call with the same parameters.
  - The sample plugin's route and command work over HTTP with an editor token. With no token, or a token for the
    wrong tenant, they return 401/403.
  - A plugin that claims a reserved prefix fails at startup.

#### M8 — Multi-tenant integration (≈ 1.5–2 wks)
- The control plane contract (D16): the env contract, health and ready endpoints, OpenAPI export, and a generated
  TypeScript/Python client for the UI. A reference gateway config (Traefik or Envoy) routing `/<tenant>/` and
  enforcing TLS.
- Progress streaming for the UI (Server-Sent Events on `/jobs/{id}/events`). CORS limited to the UI's origin.
- Starting and stopping idle tenants on demand through the provisioning scripts.
- **Exit:**
  - The full isolation suite, including the auth layer, passes against 3 tenants behind the gateway.
  - A UI-like client (a generated SDK test) runs scripts 3 and 4.
  - The docs cover how an operator adds a tenant, a user and a backup.

#### Upstream track (runs alongside both phases)
- ~~**U1 (flex-pse):** `emit_model(m)`~~ — done in flex-pse 0.2.
- ~~**U2 (flex-pse):** surrogates per relation~~ — done in 0.2 (`surrogate` elements name their relation).
- **U8 (flex-pse):** upstream the ARIMA regressor from `arima_integration` (`ArimaRegressor`, `get_regressor("arima")`,
  `to_surrogate_spec(history_length=...)`) and the `ArimaSurrogate` build onto 0.2's flowsheet spec. **Not blocking**:
  D19 ships with the 0.2 types. When a flex-pse release has it, a follow-up bumps the pin and adds ARIMA (with the
  gap-free `prep` check and ARMA fixture data) to the D19 test matrix and docs. The same goes for quadratic,
  exponential and neural-network surrogates as they get implemented.
- **U9 (Acquirium):** QUDT has no compound currency units (`USD/kWh`, `USD/a`, `USD/kW`) or `kWh/m^3`, which
  flex-pse costing uses everywhere. The orchestrator already generates QUDT-style keys for them
  (`CCY_USD-PER-KiloW-HR`, `KiloW-HR-PER-M3`), so the ask is that Acquirium's converter recognise composed keys:
  parse them by the same rule, and derive their multiplier from the parts. Then these values get queryable,
  convertible units. Until then they're stored with the Pyomo unit only (D3).
- **U11 (Acquirium):** a public way to record several variables' histories in one snapshot (the internal
  `_record_snapshot` already takes a list of variables; today only one keyed variable or an adapter `sample()` at a
  single time point uses it), and stream registration (`insert_graph`) whose cost doesn't grow with the number of
  streams already registered. Needed before native streams scale past a few keys per run (D5); snapshots don't
  depend on it.
- **U12 (Acquirium):** keep `file` / `table` artifact bytes in the database on the Timescale backend (a `BYTEA`
  column on `experiment_artifacts`, a schema migration, reads falling back to legacy files), so a Postgres backup
  covers result snapshots (D5). About 30–60 lines in `ExperimentStore` plus tests on both backends.
- **U13 (Acquirium, bugs found on `9a70a1f`):** `declare_from` with 100 adapter variables made
  `/experiments/metadata/sync` fail with "maximum recursion depth exceeded" on a fresh server (50 worked). Report
  on PR #78.
- **U14 (Acquirium):** `create_timeseries_store` logs `pg_dsn.split("@")[-1]` at debug level; for a key=value DSN
  whose password contains `@`, that prints the rest of the password. Log the parsed host instead. D11 works around
  it by passing the password as `PGPASSWORD`.
- **U15 (flex-pse, found by the RO example):**
  - `flexcore.solvers.classify` reports a model with inline `energy_prices` as NLP: the price's `kWh**-1` units
    make Pyomo's `polynomial_degree()` return `None` for `costing.opex.eq_electricity_cost`. So `solve(prefer=
    "highs")` falls back to ipopt even for an LP (DEMO_SPEC included). Tariff-file pricing classifies as LP.
  - The new-style HiGHS interface raises `NoFeasibleSolutionError` on an infeasible model instead of returning a
    termination, so `run_solve` records `error` rather than `failed`.
  - `Tank`'s holdup equation starts at `t = 1`, so the `t = 0` flows don't touch the volume, and there is no
    terminal-volume option; the example adds both constraints in a spec extension.
  - Multilinear surrogate inputs must be time-indexed, so a scalar `recovery` can't be one; `quadratic` and
    `exponential` surrogates are `NotImplementedError`. The example ties `energy_intensity` to `recovery` in an
    extension instead.
- **U10 (flex-pse):** declare the partial-horizon Sets that logic uses (e.g. `time_block.time_index_after_first`
  for `startup` / `shutdown`) as named Sets on the TimeBlock, built from `time_index`. Then the identity check finds
  them directly, and the `time_sets` hook no longer needs to know which Vars use them.
- **U3 (Acquirium):** hard delete of studies and runs, cascading to observations, artifacts and streams; a
  `superseded` status for runs; optionally a unique-name flag on `define`. **Delete is in PR #78 `9a70a1f`**
  (`delete_runs`, `delete_study`, `clear`) and D9 uses it; still open: a unique-name flag on `define`.
- **U4 (Acquirium):** an optional auth hook / API-key middleware, as defence in depth inside the tenant network.
- **U5 (Acquirium):** support `PG_DSN_FILE`; make sure the DSN is never logged; remove hard-coded credentials from
  the example configs.
- **U6 (Acquirium):** a "no DDL" startup mode, so the runtime role only needs read/write access.
- **U7 (Acquirium):** a consistent online backup for Oxigraph (`Store.backup()`) and the artifacts directory, e.g. a
  `POST /admin/backup` endpoint, or storing artifacts in Postgres or S3.
- Keep track of the PR #78 merge and release, then move the pin from a git SHA to a PyPI version.

Rough total: **Phase 1 ≈ 14–16 weeks; Phase 2 ≈ 4–5 weeks** (one developer).

---

## 6. Environments

| File | Env name | Purpose |
|---|---|---|
| `environment-dev.yml` | `flex-orch-dev` | flex-pse **v0.2.0** from git (upstream `flex-pse/flex-pse`, commit `b3cc361`), Acquirium from git at the same commit as `environment.yml` (`DataDrivenCPS/acquirium@227e3f6`, on `main`; never a local checkout), this package (`-e .[dev]`), plus test and lint tooling. **Created and verified**: everything imports and `pip check` is clean. To co-develop flex-pse, swap the git line for `-e ../flex-pse[...]` locally. |
| `environment.yml` | `flex-orch` | Deployment. flex-pse and Acquirium are pinned to **exact git SHAs**. Used by the M6 image. |

```bash
conda env create -f environment-dev.yml      # or: conda env update -f environment-dev.yml --prune
conda activate flex-orch-dev
idaes get-extensions                         # IPOPT + IDAES binaries
pip install -e .[dev]                        # once M0 adds pyproject.toml
```

Database credentials for local development (mode 2):
- Commit `.env.example` with `FLEXORCH_DB_URL` / `FLEXORCH_DB_USER` / `FLEXORCH_DB_PASSWORD` left blank. `.env` is
  git-ignored. Alternatively, use `~/.pgpass`.
- `compose.dev.yaml` starts a local Timescale with a password generated into `.env` by `make dev-db`. There are no
  hard-coded defaults.
- No secrets go into the conda env files or the `pyproject.toml`.

Rules:
- Change dependency SHAs in the same PR that makes the code use the new version. Commit a `conda-lock.yml` in M6.
- Binary solvers come from conda-forge or `idaes get-extensions`, never from pip.
- CI rebuilds the deployment env every week to catch upstream drift.

---

## 7. Testing (pytest, for everything)

Every module ships with tests in the same PR. Coverage is enforced in CI: an 85% floor at M1, raised to 90% from M4
on. Tests live next to the code they test, in a `tests/` folder inside each package
(`src/flex_orchestrator/tests/`, `src/flex_orchestrator/flexpse/tests/`, ...), so a module's tests are easy to
find and review. This differs from flex-pse on purpose.

**Tiers** (each test carries exactly one marker, enforced in `conftest.py` as in flex-pse):
- `unit`: no database, no solver. Uses the memory backend, fake hooks, and Pyomo models that are built but never
  solved. Should run in seconds.
- `component`: an embedded Acquirium in `tmp_path` plus HiGHS/IPOPT on tiny models, a local Ray (`num_cpus=2`), and
  a Timescale container for the mode 2 tests. Marked `needs_highs`, `needs_ipopt`, `needs_docker` and skipped when
  those aren't available.
- `integration`: tenant stacks from `deploy/tenant/` on a shared Postgres cluster, the example scripts, a multi-node
  Ray run, the **isolation suite**, and the **restore drill**. Phase 2 adds the server, gateway and identity
  provider.
- `benchmark` (opt-in): the D5 and M5 load tests.

**Shared fixtures (the repo-root `conftest.py`, plus a `conftest.py` in a package's `tests/` for fixtures only it uses)**
- `memory_backend`, `acquirium_local` (session-scoped: one local DuckDB Acquirium per session, since it takes about
  30 s to start), `study_name` (a fresh name per test), `backend` (parametrised: memory in the unit tier, Acquirium
  in the component tier) and `orch` (an orchestrator on `backend` with a fresh study). The backend contract suite
  and the results round-trip use `backend` / `orch`, so the memory backend can't drift from Acquirium.
- `logic_model`, `surrogate_model`, `network_model`: the corpus specs below, built (repo-root conftest).
- **Tests on Pyomo models use flex-pse models.** `flex_orchestrator.testing.DEMO_SPEC`, the six-hour flex-pse 0.2
  demo flowsheet, is the shared spec. The repo-root `demo_spec` fixture is a copy of it, and `demo_model` is it
  built with the default build hook (skipped without flex-pse). So tests exercise flex-pse's real `TimeBlock`,
  References, state blocks and units, not a hand-made stand-in.
- **flex-pse's own models, not hand-made ones.** Besides `DEMO_SPEC`, `flex_orchestrator.testing` holds three
  specs emitted with `flexops.emit_model` from flex-pse 0.2.0's round-trip corpus, kept as JSON and re-emitted when the
  pin moves. Between them they contain every structure the exporter has to classify:
  - `LOGIC_SPEC`: indexed RO trains `p.ro[0..2]` over the set `p.trains = [0, 1, 2]` (a **real decoy**: its values
    are valid time positions, but it isn't time), and `startup` / `shutdown` indexed over every time point but the
    first by an anonymous Set (a **real subset of time**);
  - `SURROGATE_DISPATCH_SPEC`: a pump surrogate's `coefficient_vars`, indexed by coefficient name only, and a battery
    with fixed dispatch values;
  - `NETWORK_SPEC`: paths through a network of two plants.
  The demo itself covers time-only, `(t, power kind)` and `(t, phase)` components, References and costing units.
- **Non-flex-pse models only for D1.** `test_walker_user_models.py` covers what a user's own Pyomo model can contain
  and flex-pse models don't: no TimeBlock, a custom index before time, nested products, index Sets of unknown width,
  index values that aren't strings or numbers, non-numeric Params, and inconsistent expression units.
- More flex-pse specs (a digester with `biogas_relation`, a spec with a tariff file for the referenced-files work
  in D2) are added to `flex_orchestrator.testing` as the milestones need them.
- `fake_hooks`, and `frame_kind` (`polars` or `pandas`).
- `surrogate_case`: parametrised over the surrogate types the **pinned flex-pse implements** (0.2: `multilinear` and
  `constant_intensity`). Each case carries a spec, the unit and relation it targets, the regressor that fits it,
  synthetic data with known coefficients, and the expected solved value.

**Surrogate coverage rule.** Tests use only surrogate types that work in the pinned flex-pse. A tripwire test asserts
that each reserved type (`quadratic`, `exponential`, `arima`, `neural_network` in 0.2) still raises
`NotImplementedError`. When a pin bump implements one, the tripwire fails, and that PR moves the type into
`surrogate_case` (with a regressor and data) and into the docs.
- `two_tenants` (integration): provisions tenants A and B with `deploy/tenant/`, and tears them down afterwards.

**What each area tests**

| Area | Tests |
|---|---|
| Backend contract | One suite run against **both** backends: create, get and list studies; declare and record; tables; latest-tag lookup; metadata filters; deleting runs and studies (never a running one). |
| Uniqueness (D9) | `create_study` on an existing study raises an error; `select_study` on a missing study raises an error; an existing tag → `TagExistsError`; a failed run can be retried; `replace=True` writes then deletes, and a failed replacement keeps the old item; the post-write check catches a simulated race (earliest wins; with `replace`, latest wins); `allow_delete=False` blocks deletion; `on_existing` modes. |
| Deletion (D9) | `delete` removes every run of a tag (failed ones too) and frees it; `delete_study` removes the ledgers and the study, deselects it, and refuses while a run is running; deleting a running run is refused. |
| Exporter (D3) | key and glob selection; time detection compares the **Set object itself** (a lookalike `Set(range(N))` is not time); time found among the factors of `(t, unit)` and `(unit, t)`; an unregistered subset of time is treated as custom; `datetime_index` mapping; time × custom → a series per value; custom-only → a scalar per element with `name[idx]` keys and `component`/`index` metadata; key collisions raise an error; `max_scalars_per_component`; stale values flagged; inactive components skipped. |
| Units | keys generated from pint's parts for compound, prefixed, powered and currency units (`L/day`, `kW*h`, `J/kg/K`, `km**2`, `degC/h`, `USD/kWh`); units with no key warn once (cached) or raise in strict mode; overrides win; with a resolver, keys are verified (multiplier to 1 ppm), other part orderings are tried, unlisted bases are found (`psi`), wrong multipliers are rejected (the `a` → `ARE` case), offset answers can't stand for a part, and the whole-unit lookup is the last resort; **against real QUDT** (component): every base unit, and the generated keys for a list of real units; `conversion_factor`, including dimension-mismatch and offset-unit errors. |
| Round-trip | export → store → read back: values and units are equal, for both frame kinds. |
| Connection / credentials (D11) | mode selection and conflicting arguments; DSN escaping (`@:/%` in passwords); precedence between `*_FILE`, `FLEXORCH_*`, `PG*` and `.pgpass`; the DSN reaches the child process only through the environment, and the toml contains no `pg_dsn`; a **redaction test** that searches logs, exceptions, `server.log` and metadata for the password; a wrong password → `DBAuthError`; preflight failures; a second managed server is refused; `recreate` is rejected. |
| Study export/import (D17) | the folder layout matches the study (one file per dataset, spec and result tag); every file validates against the published JSON Schema; result files keep `component`/`index`/`unit`/`pyomo_unit` for time-only, time × custom, scalar and custom-indexed values from the flex-pse fixture models; timestamps keep their timezone, and floats read back exactly; stale values and NaN → `null` plus the `stale` list; non-string raw indices round-trip; **export → import → export gives identical files** (apart from provenance fields); `load_result` frames equal the frames read from the database; checksum tampering and an unknown schema version are both rejected on import; import into an existing study → `StudyExistsError`, and an existing tag → `TagExistsError`; a crash part-way through leaves no partial folder; refuses a non-empty target unless `overwrite`; **no secrets or DSNs in any exported file** (scanned); gzip round-trip; memory stays bounded on a large study (benchmark tier). |
| Extension API (D18) | **Core:** `@command` parameter checking; Python and CLI calls give the same result; each call is recorded in the command ledger; `Progress` and cancellation; a long-running command goes through the job path, and Ray dispatch works from a command; `execute_study` behaves the same as a built-in command; name collisions fail at startup; entry-point discovery; **unlisted plugins don't load**; `api_version` mismatch → a clear error; two subclasses → a startup error; subclass overrides (`before_store` rejecting an item, `default_export_selection`); lifecycle events fire in order; the sample plugin's own test suite passes using `flex_orchestrator.testing`. **Server (Phase 2):** every route under `/ext/<plugin>` returns 401 with no token and 403 with another tenant's token, **checked by walking every route the app registers**, so a new plugin route can't escape the check; the role in `@command(role=...)` is enforced on `/commands/{name}`; reserved prefixes are protected; plugin OpenAPI tags appear in the generated client; `create_app` is pure (two apps with different plugins don't affect each other); the isolation suite passes with a hostile plugin enabled. |
| Regression (D19) | **Selectors:** each of the six kinds resolves to the expected stream references and window; a dataset in another study of the same tenant; `result` selectors on exported model keys; an ambiguous `resolve` fails; SPARQL that writes is rejected; unit conversion to `units`. **Preparation:** inner and outer alignment, timezone normalisation, resampling, `gaps=error` refuses gappy data, `interpolate` with `limit`, `longest_segment`, the `max_rows` guard. **Fitting:** `multilinear` and `constant_intensity` through `get_regressor` (the 0.2 types; the matrix grows with the pin); a reserved type gives a clear error naming the flex-pse version; QUDT to Pyomo conversion of column units, and an unmappable unit is an error; the hook's holdout metrics match a hand computation; a custom import-string regressor following the protocol; a regressor that doesn't follow the protocol → a clear error; tag templates over `variants`; `TagExistsError` / `on_existing`; holdout validation only when `predict` exists. **Provenance:** the data hash is stable across re-runs and changes when the data changes; resolved references are recorded; the spec links to its fit run; `fits/` round-trips through the D17 export. **Integration:** `fits:` then `sweeps` in one config; the same results with `max_workers=1` and `N`; Python, CLI and HTTP all store identical specs. Uses the flex-pse fixtures and synthetic linear data with known coefficients (ARMA data is added with U8). |
| Hooks (D1) | callable, registered-name and import-string resolution; a bad import string gives a clear error; `SolveOutcome` failure handling. |
| Specs (D2) | canonical hashing ignores key order and `1` vs `1.0`, and sees values and list order; a **pinned hash** guards the canonical form; `run_hash` over overrides; non-JSON values (sets, NaN, infinity) rejected; format detection; validators skipped when their library is missing; dict and pydantic forms store identical normalised JSON; normalisation is idempotent; kind/format mismatch rejected; tag rules; `SpecRecord` JSON round trip and `verify()` tamper check; a stored, normalised spec replays through `build`. |
| Frames (D7) | polars ↔ pandas, DatetimeIndex restored, dtypes kept. |
| Acquirium access (D8) | `orch.acquirium` is the Acquirium backend's client; the memory backend raises `StorageError`. |
| Study config | schema validation, sweep expansion (product and zip), `output_name`, dry-run, resume. |
| Parallel (D13) | `run_task` on its own (unit); Ray component tests: `TaskSpec` serialisation, a `__main__` hook rejected, a hung task killed at its timeout, retries, `on_error`, cancellation, `max_calls` restarts, thread env vars set, uniqueness with many tasks, **the same results with `max_workers=1` and `N`**. A multi-node per-tenant Ray integration test. |
| **Isolation suite (D14)** | With tenants A and B: **(DB)** B's role can't `CONNECT` to A's database, and can't create databases or roles; **(secret)** A's secret file isn't mounted in B's containers, and isn't readable inside the orchestrator container; **(network)** B's orchestrator and Ray can't resolve or reach A's Acquirium, orchestrator or Ray, and nothing outside can reach either Acquirium; **(volume)** A's `data_dir` isn't mounted anywhere in B; **(compute)** a malicious hook in B (it tries to open A's hostnames and ports, read `/var/run/docker.sock`, and list mounts) finds nothing of A's; **(data)** a study and spec written in A are invisible from every B API; containers are non-root. Phase 2 adds the **(auth)** cases: B's token gets 403 on every A route; no token gets 401; an expired or bad-signature token gets 401; role checks per route; forged gateway headers are ignored. |
| **Backup / restore** | drill: write known data into A → back up → destroy A → restore to a point in time in a scratch tenant → `verify` matches the studies, tags, a timeseries sample, the graph metadata and the file artifacts; a per-tenant `pg_dump` restore; a deprovision takes its final backup before dropping. |
| Server (Phase 2) | FastAPI `TestClient`: auth and roles, 409 on duplicates, job lifecycle and cancel, results JSON shape (golden files), SSE progress, OpenAPI schema snapshot. |
| Examples | every `examples/*.py` runs as an `integration` test. |
| Docs (§8) | the `flexorchdoc` table functions are unit-tested without Sphinx (field tables match the pydantic models, the env table matches `Settings`); `sphinx-build -W --keep-going` runs in CI; the scripts embedded on doc pages are the ones the integration tests run. |

---

## 8. Documentation (same structure and tooling as flex-pse)

**Tooling** (copied from flex-pse):
- **Sphinx** with `myst-parser` (Markdown pages) and the **furo** theme. Reference pages use `autodoc`,
  `autosummary` and `napoleon` with Google-style docstrings.
- `intersphinx` links to python, pyomo, pandas, pydantic and numpy, plus polars and ray. flex-pse is added once its
  Pages site publishes `objects.inv`.
- `nitpicky = True`, and the build runs with `sphinx-build -W --keep-going`, so **warnings fail CI**.
- A `docs` extra in `pyproject.toml` (`sphinx`, `myst-parser`, `furo`). `ruff` with `pydocstyle.convention = "google"`
  checks the docstrings on the public API.
- `.github/workflows/docs.yml` builds on every PR and deploys to GitHub Pages on `main`, the same as flex-pse.
- The same skip hook as flex-pse: members whose docstring is marked `TODO`, `DEPRECATED` or `.. deprecated::` are
  left out of the reference.

**Layout:**

```
docs/
  conf.py  Makefile  index.rst  changelog.md
  _ext/flexorchdoc.py            # directives that generate tables from code (below)
  _static/  _templates/autosummary/
  getting_started/
    installation.md              # conda envs, idaes get-extensions, connection modes          (M0)
    ten_minutes.md               # scripts 1 + 2: store data/specs, solve, store + read results (M2)
  explanation/
    architecture_and_layering.md          # library vs server, packages, import rules          (M0)
    studies_tags_and_uniqueness.md        # D9/D10: studies, tags, duplicates, deletion         (M1)
    connection_modes_and_credentials.md   # D11: local / postgres / remote, secrets             (M1)
    storage_layout_in_acquirium.md        # D5/D10: what is stored where, backup coverage       (M1)
    model_data_export_and_units.md        # D3: time-set detection, keys, indexing, QUDT units  (M2)
    export_format.md                      # D17: folder layout, flexorch-export/1 schema        (M3)
    parallel_execution.md                 # D13: Ray, task model, main-process writes           (M5)
    tenancy_isolation_and_backups.md      # D14: per-tenant stacks, guarantees, backups         (M6)
    auth_and_http_api.md                  # D15/D16: tokens, roles, control plane contract      (M7)
    extending_the_orchestrator.md         # D18: commands vs plugins vs subclassing vs routes   (M4, Phase 2 part in M7)
  how_to/
    connect_to_postgres.md                (M1)
    store_and_retrieve_data.md            (M1)
    bring_your_own_build_and_solve.md     (M2)
    select_and_export_model_variables.md  (M2)
    query_acquirium.md                    (M3)
    export_and_import_a_study.md          (M3)
    run_a_study_from_config.md            (M4)
    run_sweeps_in_parallel_with_ray.md    (M5)
    fit_a_surrogate_from_acquirium_data.md (M4; HTTP example added in M7)
    write_a_custom_command.md             (M4)
    write_and_ship_a_plugin.md            (M4; routes added in M7)
    subclass_the_orchestrator.md          (M4)
    add_custom_http_routes.md             (M7)
    provision_a_tenant.md                 (M6)
    back_up_and_restore_a_tenant.md       (M6)
    call_the_http_api.md                  (M7)
    integrate_with_the_control_plane_and_ui.md  (M8)
  examples/
    index.md                     # embeds examples/*.py with literalinclude; these are the same scripts the
                                 #   integration tests run, so the docs can't drift from working code
  reference/
    flex_orchestrator/index.rst  # Orchestrator, ResultHandle, errors
    flex_orchestrator/export.rst # export_model_data, ModelData, UnitMapper
    flex_orchestrator/archive.rst# export/import API + flexorch-export/1 schema tables
    flex_orchestrator/study.rst  # StudyConfig (field table), run_task, TaskSpec/TaskResult
    flex_orchestrator/hooks.rst  # BuildFn/SolveFn/ExportFn, SolveOutcome, flex-pse default hooks
    flex_orchestrator/backend.rst# StorageBackend protocol
    flex_orchestrator/settings.rst # connection modes + FLEXORCH_* env var table
    flex_orchestrator/ext.rst    # @command, Plugin, events, stable subclass methods, PLUGIN_API_VERSION
    flex_orchestrator/fitting.rst# fit_surrogate parameters, selector and prep tables (generated from pydantic)
    flex_orchestrator/testing.rst# helpers for plugin authors
    flex_orchestrator/server_deps.rst # Phase 2: create_app, deps for plugin routes
    cli.rst                      # flex-orch commands (generated from --help)
    http_api.rst                 # Phase 2: OpenAPI-generated route reference
    glossary.md                  # study, tag, spec, run, snapshot, tenant, time set, …
```

`index.rst` uses flex-pse's toctree captions in the same order: *Getting started · Explanation · How-to · Examples ·
Release notes · Reference*.

**Docs generated from code, so reference pages can't drift** (the same idea as flex-pse's `flexdoc` extension, kept
small):
- `.. flexorch-config-table:: <pydantic model>` renders a field table: name, type, default and description. It is
  used for `StudyConfig`, the `execution` and `export` blocks, every `flexorch-export/1` file model, and `Settings`.
- `.. flexorch-env-table::` lists every `FLEXORCH_*` env var and secret file, with its source and precedence.
- `.. flexorch-cli::` includes the `--help` output of each CLI command.
- Phase 2 adds `.. flexorch-openapi::`, which renders the route reference from the app's generated OpenAPI schema.
- The JSON Schemas in `docs/schemas/` are written by the same code and linked from `export_format.md`.
- The table-building functions don't depend on Sphinx, so unit tests call them directly, as flex-pse's
  `test_flexdoc_tables.py` does.

**Rules:**
- **Each milestone adds the pages listed next to it above.** A milestone isn't done until its pages are written and
  the docs build passes with `-W`.
- Every public function and class has a Google-style docstring with at least one usage example. Ruff checks the
  docstrings, and nitpicky mode checks the cross-references.
- How-to pages use only the public API. Their code blocks are copied from `examples/` or tested snippets, never
  written only for the docs.
- `docs/changelog.md` gets an entry with every PR, matching the CHANGELOG rule in §9.

---

## 9. Processes

- **Branching:** trunk-based on `main`. Short-lived `feat/…` and `fix/…` branches, squash-merged. Each PR is one
  milestone task and includes its tests.
- **PR checklist:** tests in the right tier; coverage floor met; docs or an example updated; a CHANGELOG entry; no
  layering violations (especially no HTTP in the core library); `ruff check .` and `black --check .` are clean (D20).
- **Code style:** black and ruff exactly as in flex-pse (D20). Install the hooks once with
  `pre-commit install --hook-type pre-commit --hook-type pre-push`. Never use `--no-verify` to get a commit through.
  PRs that only reformat stay separate from PRs that change behaviour.
- **Security review:** any PR that touches `connection.py`, `deploy/tenant/` or `server/auth.py` needs
  a second reviewer, and must keep the isolation suite and the redaction test passing.
- **Extension API changes:** any change to `ext/`, `testing/` or `server/deps.py` updates `PLUGIN_API_VERSION`
  (a breaking change bumps the major version) and records it in the changelog. The sample plugin must keep passing.
- **Versioning:** SemVer. `0.1.0` at the end of Phase 1, `0.2.0` at the end of Phase 2. Every stored run records the
  package versions and hook names.
- **Operations:** the restore drill runs every quarter against a scratch tenant. Tenant database secrets are rotated
  by re-provisioning.
- **Upstream sync:** U-items are tracked as issues in their upstream repos. The pinned SHAs are checked at every
  milestone.
- **Definition of done for a milestone:** the exit criterion is met; CI passes on Windows and Linux (unit +
  component); integration passes on Linux; the example is written; **the milestone's docs pages (§8) are written and
  `sphinx-build -W` passes**.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| PR #78 API changes before merge | Every Acquirium call goes through `backend/acquirium.py`, and the contract tests catch drift. |
| Data leaks between tenants | One stack per tenant (database, role, secret, volume, network, Ray), with nothing shared. The isolation suite gates M6, M7 and M8. Phase 2 checks the token's tenant claim in the orchestrator itself. |
| User hook code is arbitrary code | It runs only inside the tenant's own containers and Ray: non-root, no Docker socket, no route to other tenants. |
| Shared Postgres cluster is a single point of failure | PITR backups, monitoring, and optionally a streaming replica. Per-tenant `pg_dump` for moving tenants elsewhere. |
| Backups miss graph or artifacts (not in Postgres) | Volume snapshots plus the Oxigraph export taken with each database backup, a restore drill as an exit criterion, and U7 upstream. |
| A delete can't be undone | `replace` writes before deleting; `allow_delete=False` per deployment; backups (D14) restore deleted items. |
| Database credentials leak | Secret files only, `SecretStr`, redaction everywhere, the redaction test, and U5. |
| Two Acquirium servers on one database | One Acquirium per tenant database by construction, plus the mode 2 server lock. |
| `recreate=true` drops data | Rejected by the orchestrator. The tenant role can only affect its own database, and backups cover it. |
| Acquirium `define` silently returns an existing study | Check for existence first, verify after writing, and one writing process per tenant. |
| A label's unit can't change | Convert to the declared unit, and raise an error if the dimensions don't match. |
| Pyomo units with no QUDT match | Mapping table, resolver fallback, overrides, and strict mode. |
| Many streams per run (no parquet) | M2 benchmark and `include`/`exclude` selection. |
| Hung or leaking IPOPT solves | `ray.cancel(force=True)` and `max_calls` restarts. |
| Ray on Windows is less mature | Local Ray tested on Windows in CI. Tenant stacks are Linux. `max_workers=1` avoids Ray entirely. |
| Cost of 10–50 tenant stacks | Idle tenants stopped with their volumes kept; `exact_only=true` on Acquirium; Ray started only when needed. |
| Export format drifts from what's stored | A versioned `flexorch-export/N` schema built from the same pydantic models the code uses, a round-trip test in CI, and import that refuses unknown versions. |
| Large JSON results (many keys × 8,760 steps) | One file per result, streamed writes, optional gzip, and `include`/`exclude` at export time. |
| Regression on poor or ambiguous data | `gaps=error` by default, ambiguous `resolve` fails, every fit records its provenance and data hash, and holdout metrics are available where `predict` exists. |
| Large training pulls exhaust memory | `max_rows`, windowed selectors, data loaded once and shared through `ray.put`. |
| Regression API changes in flex-pse | Only the `Regressor` protocol and `get_regressor` are used, through `flexpse/regression.py`, and contract tests run against the pinned flex-pse. |
| A plugin route ships without auth | Auth dependencies are attached to the router when it is mounted, reserved prefixes are enforced, and a test walks every registered route. |
| Plugins break when the core changes | A small stable surface (commands, `Plugin`, events, the listed subclass methods, `server/deps`), versioned with `PLUGIN_API_VERSION`, and the sample plugin tested in CI. |
| Plugin code compromises a tenant | Plugins are installed per image, enabled per tenant through the allowlist, run inside the tenant sandbox, and the isolation suite runs with a hostile plugin enabled. |
| HTTP arriving late hides integration problems | The core API is shaped so it maps 1:1 onto the §4 routes, and hook import strings and `TaskSpec` are plain data from M4 on. M7 is mostly wiring. |
