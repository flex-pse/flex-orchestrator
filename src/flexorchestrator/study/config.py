"""``StudyConfig``: a whole study as data.

A config names a base model spec, optional ``fits`` (run first, so sweeps can
use the surrogates they store), ``sweeps`` (combined as a product or zipped),
and how each task's result is named, exported and stored. A sweep varies one
thing:

- ``surrogate``: the surrogate a unit uses;
- ``variable``: a model variable, fixed to each value;
- ``configuration_option``: an option the model is built with, such as a
  unit's construction option or a price;
- ``spec_list``: the model spec itself;
- ``model_inputs``: the stored data model variables are set from;
- ``cases``: named, hand-picked combinations of the above.

``design`` (optional) runs each task as a design study: the model spec over every
year and representative month, sizing shared capacities. Leave it out for an
ordinary study.

``model_inputs`` (outside the sweeps) sets model variables from stored data in
every task: a dataset, a stored result or general-store streams, selected as a
fit's ``X`` and ``y`` are.

For example::

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

Hooks are import strings or registered names, so a config is plain data that
a Ray worker or the HTTP API can carry. Its JSON Schema is
``StudyConfig.model_json_schema()`` (published in ``docs/schemas/``).
"""

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError, model_validator

from flexorchestrator.errors import StudyConfigError
from flexorchestrator.fitting.models import FitParams, _Strict
from flexorchestrator.inputs.models import InputSet, ModelInput


class SurrogateSweep(_Strict):
    """Each task uses one of ``surrogates`` for ``unit``.

    Attributes:
        name: The field ``output_name`` renders the surrogate's tag as;
            ``surrogate`` by default.
        unit: The unit's path, e.g. ``"plant.ro"``.
        relation: The relation the surrogate replaces; the hook's default if
            omitted.
        surrogates: Surrogate spec tags, stored or fitted by ``fits``.
    """

    type: Literal["surrogate"]
    name: str | None = None
    unit: str
    relation: str | None = None
    surrogates: list[str] = Field(min_length=1)


class VariableSweep(_Strict):
    """Each task fixes the model variable at ``path`` to one of ``values``.

    The values are passed to the build hook as overrides; for flex-pse each
    becomes a ``fix`` element. Use it for variables (``"plant.ro.recovery"``);
    an option the unit is built with is a :class:`ConfigurationSweep`.

    Attributes:
        name: The field ``output_name`` renders the value as; ``path`` by
            default.
        path: The variable's path from the model root, e.g.
            ``"plant.ro.recovery"`` or ``"plant.tank.level[*]"``.
        values: The values: numbers in the variable's units,
            ``{"value", "units"}``, or one number per time point.
    """

    type: Literal["variable"]
    name: str | None = None
    path: str
    values: list[Any] = Field(min_length=1)


class ConfigurationSweep(_Strict):
    """Each task sets the configuration option at ``path`` to one of ``values``.

    The option is set in a copy of the base spec (the ``configure`` hook), which
    is stored under the task's tag with the base as parent. For flex-pse,
    ``path`` is any element's name followed by the option: a unit's
    construction option (``"plant.feed.max_withdrawal"``) or another
    element's field (``"costing.energy_prices.electrical"``). A dict-valued
    option is set whole by a dict value, or one key at a time by a longer
    path (``"plant.feed.max_withdrawal.value"``).

    Attributes:
        name: The field ``output_name`` renders the value as; ``path`` by
            default.
        path: The option's path.
        values: The values; any JSON value, dicts included.
    """

    type: Literal["configuration_option"]
    name: str | None = None
    path: str
    values: list[Any] = Field(min_length=1)


class SpecListSweep(_Strict):
    """Each task builds from one of ``specs`` instead of ``base_model_spec``.

    Attributes:
        name: The field ``output_name`` renders the tag as; ``spec`` by
            default.
        specs: Model spec tags.
    """

    type: Literal["spec_list"]
    name: str | None = None
    specs: list[str] = Field(min_length=1)


class ModelInputsSweep(_Strict):
    """Each task sets model variables from one of ``values``' sets of data.

    An entry in a set replaces the config's ``model_inputs`` entry for the same
    path, so a sweep can vary only the streams it names.

    Attributes:
        name: The field ``output_name`` renders a set's label as;
            ``model_inputs`` by default.
        values: The sets; their labels must differ.
    """

    type: Literal["model_inputs"]
    name: str | None = None
    values: list[InputSet] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct_labels(self) -> "ModelInputsSweep":
        labels = [value.label for value in self.values]
        repeated = sorted({n for n in labels if labels.count(n) > 1})
        if repeated:
            raise ValueError(f"model_inputs labels must differ; repeated: {repeated}")
        return self


class Case(_Strict):
    """One hand-picked combination in a :class:`CasesSweep`.

    Attributes:
        name: The case's name, which ``output_name`` renders as ``{case}``.
        spec: A model spec to build from instead of ``base_model_spec``.
        surrogates: ``{unit: surrogate tag}``.
        configuration_options: ``{option path: value}``, as a
            :class:`ConfigurationSweep` sets them.
        variables: ``{variable path: value}``, as a :class:`VariableSweep`
            fixes them.
        model_inputs: Entries that set variables from stored data, as a
            :class:`ModelInputsSweep` value does.
    """

    name: str
    spec: str | None = None
    surrogates: dict[str, str] = Field(default_factory=dict)
    configuration_options: dict[str, Any] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)
    model_inputs: list[ModelInput] = Field(default_factory=list)


class CasesSweep(_Strict):
    """Each task runs one of ``cases``: chosen combinations, not every one.

    A cases sweep is one sweep like any other, so it can be combined with the
    rest (a ``product`` with a price sweep runs every case at every price).

    Attributes:
        name: The field ``output_name`` renders the case's name as; ``case``
            by default.
        cases: The cases; their names must differ.
    """

    type: Literal["cases"]
    name: str | None = None
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct_names(self) -> "CasesSweep":
        names = [case.name for case in self.cases]
        repeated = sorted({n for n in names if names.count(n) > 1})
        if repeated:
            raise ValueError(f"case names must differ; repeated: {repeated}")
        return self


Sweep = Annotated[
    SurrogateSweep
    | VariableSweep
    | ConfigurationSweep
    | SpecListSweep
    | ModelInputsSweep
    | CasesSweep,
    Field(discriminator="type"),
]


class PlanningYear(_Strict):
    """One modeled year of a design study.

    Attributes:
        year: The calendar year.
        represents: The calendar years it stands in for.
    """

    year: int
    represents: int = 1


class RepresentativeMonth(_Strict):
    """One calendar month modeled in every planning year.

    Attributes:
        month: The calendar month, 1 to 12.
        weight: The months of the year it stands in for; weights sum to 12.
        days: Model only the first N days and scale the bill up to the full
            month; the whole month if omitted.
    """

    month: int
    weight: float
    days: int | None = None


class ParameterChange(_Strict):
    """A change to one base element for some years and months.

    Attributes:
        element: The name of the base element.
        values: The fields to deep-merge into it. Files named here are stored
            with the spec.
        years: The years it applies to; all if omitted.
        months: The months it applies to; all if omitted.
    """

    element: str
    values: dict[str, Any]
    years: list[int] | None = None
    months: list[int] | None = None


class CapacityDecision(_Strict):
    """One capacity the design study sizes, shared by every horizon.

    Attributes:
        name: The decision's name, e.g. ``"battery_energy"``.
        variable: The scalar variable's path, e.g.
            ``"battery.energy_capacity"``.
        capital_cost: The overnight cost per unit of the variable.
        lifetime_years: The asset life used to annualize it.
        lower: The lower bound, in the variable's units.
        upper: The upper bound, in the variable's units.
    """

    name: str
    variable: str
    capital_cost: float
    lifetime_years: float
    lower: float = 0.0
    upper: float


class DesignStudy(_Strict):
    """Run each task's model spec as a flex-pse design study.

    This mirrors flex-pse's ``DesignSpec``; flex-pse checks the rules (weights
    sum to 12, years are consecutive) before any task runs.

    Attributes:
        schema_version: The version of this study.
        years: The modeled years, ascending.
        months: The representative months.
        changes: Changes to base elements, applied in order.
        capacities: What to size.
        capex_budget: A cap on the total overnight capital cost of every
            capacity, in the model's currency; none by default.
    """

    schema_version: str = "0.1.0"
    years: list[PlanningYear] = Field(min_length=1)
    months: list[RepresentativeMonth] = Field(min_length=1)
    changes: list[ParameterChange] = Field(default_factory=list)
    capacities: list[CapacityDecision] = Field(min_length=1)
    capex_budget: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class HooksConfig(_Strict):
    """Hooks for the study's tasks: import strings or registered names.

    Unset slots use the orchestrator's hooks; hooks passed to
    ``execute_study`` win over these.

    Attributes:
        build: The build hook.
        solve: The solve hook.
        apply_surrogate: Applies a surrogate sweep's surrogates to the spec.
        swap_surrogate: Swaps a surrogate on a live model.
        time_sets: Names a built model's time Sets for the exporter.
        configure: Sets a configuration sweep's options in the spec.
        time_grid: Gives a spec's time axis, for ``model_inputs``.
    """

    build: str | None = None
    solve: str | None = None
    apply_surrogate: str | None = None
    swap_surrogate: str | None = None
    time_sets: str | None = None
    configure: str | None = None
    time_grid: str | None = None


class ExportConfig(_Strict):
    """What each task's result stores; nothing set means the default selection.

    Attributes:
        keys: Exact paths to export.
        include: Glob patterns to export.
        exclude: Glob patterns to leave out.
        streams: Values to also store natively.
        strict_units: Raise on a unit with no QUDT key.
    """

    keys: list[str] | None = None
    include: list[str] | None = None
    exclude: list[str] | None = None
    streams: list[str] | None = None
    strict_units: bool = False


class Resources(_Strict):
    """What each Ray task reserves.

    Attributes:
        num_cpus: CPUs per task; Ray runs as many at once as the cluster
            fits, up to ``max_workers``.
        memory: Bytes of memory per task, or ``None`` to reserve none.
    """

    num_cpus: float = Field(default=1.0, gt=0)
    memory: float | None = Field(default=None, gt=0)


class Execution(_Strict):
    """How tasks run.

    ``max_workers: 1`` (the default) runs the tasks one after another in this
    process. More than 1 runs them on Ray: a local Ray started for this
    process (``namespace="flexorch"``), the one already connected, or the
    cluster at ``ray_address``. Either way the main process stores every
    result, so workers never hold database credentials.

    Attributes:
        max_workers: Tasks run at once.
        ray_address: A Ray cluster, e.g. ``"ray://tenant-ray-head:10001"``;
            local Ray if ``None``. Its workers need flex-orchestrator and the
            hooks' modules installed.
        resources: What each task reserves on Ray.
        max_calls: Restart a Ray worker process after this many tasks, to
            keep memory bounded; ``None`` never restarts.
        task_timeout_s: A task's time limit on Ray, counted from when it's
            submitted, so it includes starting a worker (a few seconds the
            first time). A task that runs over is killed and stored as a failed
            result (``termination: timeout``); it isn't retried. Two hours by
            default; ``None`` means no limit. In-process it is not enforced.
        retries: How many times a task is built and solved again when building
            or exporting raises, the solve hook raises, or its Ray worker dies.
            A task whose spec or result can't be stored isn't retried.
        on_error: Go on with the other tasks after one fails (``continue``) or
            stop (``stop``); on Ray, ``stop`` cancels the tasks still running.
    """

    max_workers: int = Field(default=1, ge=1)
    ray_address: str | None = None
    resources: Resources = Field(default_factory=Resources)
    max_calls: int | None = Field(default=None, ge=1)
    task_timeout_s: float | None = Field(default=7200.0, gt=0)
    retries: int = Field(default=0, ge=0)
    on_error: Literal["continue", "stop"] = "continue"


class StudyConfig(_Strict):
    """A study as data; see :mod:`flexorchestrator.study.config`.

    Attributes:
        study: The study to run in, created if needed; the selected one if
            omitted.
        base_model_spec: The model spec every task builds from, unless a
            ``spec_list`` sweep names others.
        design: Run every task as a design study
            (:class:`~flexorchestrator.study.config.DesignStudy`).
        fits: ``fit_surrogate`` parameters, run before the sweeps.
        model_inputs: Model variables every task sets from stored data:
            fit selectors, each column (after ``rename``) named by the path it
            sets, lined up with the model's time steps. In a design study, a
            path of the base flowsheet is set in every horizon from that
            horizon's dates, and ``horizon[<year>,<month>].<path>`` in one.
        hooks: See :class:`HooksConfig`.
        sweeps: See :class:`SurrogateSweep`, :class:`VariableSweep`,
            :class:`ConfigurationSweep`, :class:`SpecListSweep`,
            :class:`ModelInputsSweep` and :class:`CasesSweep`.
        combine: ``product`` (every combination) or ``zip`` (the sweeps in
            step; they must be the same length).
        solve_options: Passed to the solve hook. For flex-pse: ``prefer``,
            ``tee``, ``warmstart``, ``mip_gap``, ``time_limit`` and ``options``.
        export: See :class:`ExportConfig`.
        output_name: Each task's tag, e.g.
            ``"ro_{surrogate}_{plant.ro.recovery}"``: fields are the sweeps'
            names, plus ``base`` and ``index``. By default the base tag and
            each sweep's value, joined by ``_``. A value is written exactly
            (``str``; ``0.1 + 0.2`` gives ``0.30000000000000004``) unless the
            field has a format spec, as in Python: ``{recovery:.3g}``,
            ``{price:.2f}``. The tag is only a name: the values themselves are
            stored in full with the result. Values that format alike make
            tags that repeat, which is refused before anything runs.
        on_existing: When a task's tag is stored: ``error`` (checked before
            anything runs), ``skip`` it (resume), or ``replace`` it.
        execution: See :class:`Execution`.
    """

    study: str | None = None
    base_model_spec: str | None = None
    design: DesignStudy | None = None
    fits: list[FitParams] = Field(default_factory=list)
    model_inputs: list[ModelInput] = Field(default_factory=list)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    sweeps: list[Sweep] = Field(default_factory=list)
    combine: Literal["product", "zip"] = "product"
    solve_options: dict[str, Any] = Field(default_factory=dict)
    export: ExportConfig = Field(default_factory=ExportConfig)
    output_name: str | None = None
    on_existing: Literal["error", "skip", "replace"] = "error"
    execution: Execution = Field(default_factory=Execution)


def load_config(config: Any) -> StudyConfig:
    """Return ``config`` as a :class:`~flexorchestrator.study.StudyConfig`.

    Args:
        config: A ``StudyConfig``, a mapping, or the path of a YAML (``.yaml``,
            ``.yml``) or JSON file.

    Raises:
        StudyConfigError: If the file can't be read or the config doesn't
            validate; the message lists each problem.
    """
    if isinstance(config, StudyConfig):
        return config
    if isinstance(config, str | os.PathLike):
        config = _read(Path(config))
    if not isinstance(config, Mapping):
        raise StudyConfigError(
            f"A study config is a StudyConfig, a mapping or a file path, not "
            f"{type(config).__name__}."
        )
    try:
        return StudyConfig.model_validate(dict(config))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(config)'}: {e['msg']}"
            for e in exc.errors()
        )
        raise StudyConfigError(f"Invalid study config: {problems}.") from exc


def _read(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StudyConfigError(f"Can't read study config {path}: {exc}") from exc
    try:
        if path.suffix.lower() in (".yaml", ".yml"):
            import yaml

            return yaml.safe_load(text)
        return json.loads(text)
    except Exception as exc:
        raise StudyConfigError(f"Can't parse study config {path}: {exc}") from exc
