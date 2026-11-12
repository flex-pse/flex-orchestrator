"""Turn ``model_inputs`` entries into build overrides.

Runs in the main process, which holds the store; the values it returns are
plain data a Ray worker builds with. Each entry's selector is loaded with the
fit selectors (``fitting.selectors.load``), then:

1. its UTC timestamps go to the entry's ``timezone`` and lose it, since the
   model's time steps are the facility's naive wall clock;
2. the data is shifted by ``data_years`` and ``offset`` onto the model's
   horizon;
3. it is resampled onto the model's time steps and its gaps filled;
4. or, with ``reduce``, the horizon's data becomes one number.

A model with several time blocks (a design study's horizons, or blocks that
each hold one) lines each path up with the nearest time block above it, and
sets a path under none in every one (:mod:`flexorchestrator.inputs.scopes`).
Each time block's scope is read over its own dates and gets its own
provenance record.
"""

import copy
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from flexorchestrator.errors import FitError, ModelInputError
from flexorchestrator.fitting import selectors
from flexorchestrator.fitting.models import Selector, Window
from flexorchestrator.inputs.models import InputResample, ModelInput
from flexorchestrator.inputs.scopes import grids_of, targets

# Data is read this far past each end of the horizon, so a gap at either end
# can be interpolated from the samples beyond it.
_MARGIN = pd.Timedelta(days=1)
_ALIGNMENT = (
    "timezone",
    "resample",
    "gaps",
    "limit",
    "reduce",
    "offset",
    "data_years",
)


@dataclass
class Resolved:
    """What a list of entries set.

    Attributes:
        values: ``{model path: value}``, for the build hook: a list with one
            number per time point (or one number, for ``reduce``), as
            ``{"value", "units"}`` when the data has a unit.
        provenance: Per entry (and, in a model with several time blocks, per
            scope): the entry as sent, what its selector resolved to, the
            window read, how many steps were filled, and the values' hash.
        requested: ``{model path: the path its entry named}``. They differ
            where a path under no time block is set in every scope.
    """

    values: dict[str, Any] = field(default_factory=dict)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    requested: dict[str, str] = field(default_factory=dict)


def resolve(
    orch: Any,
    inputs: Sequence[ModelInput],
    grid: Any,
    swept: Sequence[ModelInput] = (),
    cache: dict | None = None,
) -> Resolved:
    """Resolve a task's entries against its model's time steps.

    Args:
        orch: The orchestrator, with the task's study selected.
        inputs: The study's (or one build's) entries.
        grid: What the ``time_grid`` hook gave: the model's time steps,
            ``{scope: time steps}`` for a model with several time blocks, or
            ``None`` for a model without a time axis, which takes only
            ``reduce`` entries.
        swept: Entries from a sweep or case; one replaces an ``inputs`` entry
            that sets the same path.
        cache: Selector data read so far, reused for an entry that reads the
            same data (a study passes one for all its tasks).

    Raises:
        ModelInputError: If an entry's data can't be read or lined up, or two
            entries of one list set the same path.
    """
    grids = grids_of(grid)
    base, swept_found = (
        _layer(orch, inputs, grids, cache),
        _layer(orch, swept, grids, cache),
    )
    values = {**base.values, **swept_found.values}
    kept = []
    for record in base.provenance:
        paths = [p for p in record["paths"] if p not in swept_found.values]
        if paths:
            kept.append({**record, "paths": paths})
    requested = {**base.requested, **swept_found.requested}
    return Resolved(values, kept + swept_found.provenance, requested)


def _layer(
    orch: Any,
    inputs: Sequence[ModelInput],
    grids: dict[str, pd.DatetimeIndex | None],
    cache: dict | None,
) -> Resolved:
    found = Resolved()
    for entry in inputs:
        values, records, requested = _entry(orch, entry, grids, cache)
        twice = sorted(set(values) & set(found.values))
        if twice:
            raise ModelInputError(
                f"Two model_inputs entries both set {twice}; each path takes one."
            )
        found.values.update(values)
        found.provenance.extend(records)
        found.requested.update(requested)
    return found


def _entry(
    orch: Any,
    entry: ModelInput,
    grids: dict[str, pd.DatetimeIndex | None],
    cache: dict | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    """Resolve one entry: its values, a record per scope, and what it named."""
    selector = Selector(**entry.model_dump(exclude=set(_ALIGNMENT)))
    scopes = list(grids)
    loaded: dict[str, Any] = {}
    columns = _named_columns(entry)
    if columns is None:
        # The data names its columns: read it over the first time axis it has
        # data for (an entry for 2035 has none over 2030's dates).
        columns = _probe(orch, entry, selector, grids, cache, loaded)
    # The columns, renamed to paths, say which scopes the entry sets.
    plan: dict[str, list[tuple[str, str]]] = {}
    for column in columns:
        for scope, target in targets(column, scopes):
            plan.setdefault(scope, []).append((column, target))
    values: dict[str, Any] = {}
    records, requested = [], {}
    for scope, columns in plan.items():
        if scope not in loaded:
            loaded[scope] = _read(orch, entry, selector, grids[scope], cache)
        data, start, end, window = loaded[scope]
        found, filled = {}, {}
        for column, target in columns:
            series = data.frame[column]
            if entry.reduce is not None:
                value = _reduce(series, entry, start, end)
            else:
                value, filled[target] = _series(series, entry, grids[scope])
            unit = data.units.get(column)
            if unit:
                # Pyomo prints m**3; flex-pse's parser splits on '*', so write m^3.
                value = {"value": value, "units": unit.replace("**", "^")}
            found[target] = value
            requested[target] = column
        record = {
            "input": entry.model_dump(mode="json", exclude_defaults=True),
            "resolved": _plain(data.provenance),
            "paths": list(found),
        }
        if scope:
            record["scope"] = scope
        record["window"] = window
        record["timezone"] = entry.timezone
        record["filled"] = filled
        record["data_hash"] = _hash(found)
        values.update(found)
        records.append(record)
    return values, records, requested


def _named_columns(entry: ModelInput) -> list[str] | None:
    """The paths an entry sets, when it names its columns; ``None`` otherwise."""
    if entry.columns is None or "*" in entry.rename:
        return None
    return [entry.rename.get(column, column) for column in entry.columns]


def _probe(
    orch: Any,
    entry: ModelInput,
    selector: Selector,
    grids: dict[str, pd.DatetimeIndex | None],
    cache: dict | None,
    loaded: dict[str, Any],
) -> list[str]:
    """Read the entry over each time axis until one has data; its columns."""
    error = None
    for scope, grid in grids.items():
        try:
            loaded[scope] = _read(orch, entry, selector, grid, cache)
        except ModelInputError as exc:
            error = error or exc
            continue
        return list(loaded[scope][0].frame.columns)
    raise error


def _read(
    orch: Any,
    entry: ModelInput,
    selector: Selector,
    grid: pd.DatetimeIndex | None,
    cache: dict | None,
) -> tuple[Any, Any, Any, dict[str, str] | None]:
    """Load an entry's data for one time axis, indexed by the model's time.

    Returns:
        The data, its horizon's start and end in model time (``None`` without
        a time axis), and the window read, in the data's time.
    """
    if grid is None:
        if entry.reduce is None:
            raise ModelInputError(
                f"Model input {_describe(entry)} gives a series, but the model has "
                "no time axis (its time_grid hook returned None); set reduce."
            )
        data = _load(orch, selector, None, cache)
        frame = data.frame.copy()
        frame.index = frame.index.tz_convert(entry.timezone).tz_localize(None)
        data.frame = frame
        return data, None, None, None
    step = _step(grid)
    start, end = grid[0], grid[-1] + step
    years = _years(entry, start)
    shift = pd.Timedelta(entry.offset) if entry.offset else pd.Timedelta(0)
    margin = pd.Timedelta(0) if entry.reduce else _MARGIN
    if years:
        start_in_data = start + pd.DateOffset(years=years)
        end_in_data = end + pd.DateOffset(years=years)
    else:
        start_in_data, end_in_data = start, end
    first, last = start_in_data + shift, end_in_data + shift
    window = Window(
        start=_utc(first - margin, entry.timezone),
        end=_utc(last + margin, entry.timezone),
    )
    data = _load(orch, selector, window, cache)
    frame = data.frame.copy()
    local = frame.index.tz_convert(entry.timezone).tz_localize(None) - shift
    frame.index = local - pd.DateOffset(years=years) if years else local
    data.frame = frame
    # A backward step reads the interval ending at it, a step earlier.
    lag = step if _direction(entry) == "backward" else pd.Timedelta(0)
    return data, start, end, {"start": str(first - lag), "end": str(last - lag)}


def _years(entry: ModelInput, start: pd.Timestamp) -> int:
    """How many years a horizon's data year is from its modeled year."""
    if not entry.data_years or start.year not in entry.data_years:
        return 0
    return entry.data_years[start.year] - start.year


def _load(
    orch: Any, selector: Selector, window: Window | None, cache: dict | None
) -> Any:
    key = (
        orch.study,
        json.dumps(selector.model_dump(mode="json"), sort_keys=True),
        window and (window.start, window.end),
    )
    if cache is not None and key in cache:
        return copy.deepcopy(cache[key])
    try:
        found = selectors.load(orch, selector, window)
    except FitError as exc:
        raise ModelInputError(f"Model input: {exc}") from exc
    if cache is not None:
        cache[key] = copy.deepcopy(found)
    return found


def _series(
    series: pd.Series, entry: ModelInput, grid: pd.DatetimeIndex
) -> tuple[list[float], int]:
    step = _step(grid)
    agg = (entry.resample or InputResample()).agg
    backward = _direction(entry) == "backward"
    if agg == "sum":
        series = _split(series, step, backward, entry)
    # forward: [t, t + dt) labelled t; backward: (t - dt, t] labelled t.
    side = "right" if backward else "left"
    bins = series.resample(step, origin=grid[0], closed=side, label=side)
    binned = bins.sum(min_count=1) if agg == "sum" else getattr(bins, agg)()
    # The bins share the grid's origin and step, so the union stays regular.
    extended = binned.reindex(binned.index.union(grid))
    missing = int(extended.reindex(grid).isna().sum())
    if entry.gaps == "interpolate":
        extended = extended.interpolate(
            method="time", limit=entry.limit, limit_area="inside"
        )
    elif entry.gaps == "ffill":
        extended = extended.ffill(limit=entry.limit)
    elif entry.gaps == "zero":
        extended = extended.fillna(0.0)
    aligned = extended.reindex(grid)
    left = aligned.index[aligned.isna()]
    if len(left):
        shown = ", ".join(str(t) for t in left[:3]) + (", ..." if len(left) > 3 else "")
        raise ModelInputError(
            f"Model input {_describe(entry)} ({series.name!r}) has no value at "
            f"{len(left)} of the model's {len(grid)} time steps ({shown}) after "
            f"gaps: {entry.gaps}"
            + (f" with limit {entry.limit}" if entry.limit else "")
            + ". Read a wider window, or fill the gaps another way."
        )
    return [float(v) for v in aligned], missing


def _direction(entry: ModelInput) -> str:
    return (entry.resample or InputResample()).window


def _split(
    series: pd.Series, step: pd.Timedelta, backward: bool, entry: ModelInput
) -> pd.Series:
    """Spread amounts coarser than ``step`` evenly over the steps they cover.

    A sample stands for the interval that starts at it (forward) or ends at it
    (backward); its amount is split into one equal part per step of that
    interval, so a sum over any steps keeps the total.
    """
    clean = series.dropna()
    if len(clean) < 2:
        return series
    spacing = pd.Series(clean.index).diff().median()
    if spacing <= step:
        return series
    parts, rest = divmod(spacing, step)
    if rest:
        raise ModelInputError(
            f"Model input {_describe(entry)} ({series.name!r}) sums data every "
            f"{spacing} onto steps of {step}; its amounts can only be split over a "
            "whole number of steps."
        )
    sign = -1 if backward else 1
    pieces = [
        pd.Series(clean.to_numpy() / parts, clean.index + sign * k * step)
        for k in range(int(parts))
    ]
    return pd.concat(pieces).sort_index().rename(series.name)


def _reduce(series: pd.Series, entry: ModelInput, start: Any, end: Any) -> float:
    if start is not None:
        series = series[(series.index >= start) & (series.index < end)]
    series = series.dropna()
    if series.empty:
        raise ModelInputError(
            f"Model input {_describe(entry)} ({series.name!r}) has no values over "
            "the model's horizon to reduce."
        )
    if entry.reduce == "first":
        return float(series.iloc[0])
    if entry.reduce == "last":
        return float(series.iloc[-1])
    return float(getattr(series, entry.reduce)())


def _step(grid: pd.DatetimeIndex) -> pd.Timedelta:
    if grid.freq is not None:
        return pd.Timedelta(grid.freq)
    if len(grid) < 2:
        raise ModelInputError("The model's time grid has one point and no freq.")
    return grid[1] - grid[0]


def _utc(local: pd.Timestamp, zone: str) -> datetime:
    """A naive local time as UTC; a time DST skips or repeats takes the later."""
    stamp = pd.Timestamp(local).tz_localize(
        zone, ambiguous=False, nonexistent="shift_forward"
    )
    return stamp.tz_convert("UTC").to_pydatetime()


def _hash(values: dict[str, Any]) -> str:
    text = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _describe(entry: ModelInput) -> str:
    return f"{{{entry.source!r}: {getattr(entry, entry.source)!r}}}"
