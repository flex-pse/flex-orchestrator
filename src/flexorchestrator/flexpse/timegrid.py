"""The flex-pse ``time_grid`` hook: a spec's or a built model's time points."""

import datetime
from collections.abc import Mapping
from typing import Any

import pandas as pd
import pyomo.environ as pyo
from flexops.core.build import parse_quantity
from flexops.core.time_block import TimeBlockData
from pyomo.environ import units as pyunits

from flexorchestrator.errors import SpecError
from flexorchestrator.specs import is_design_spec


def time_grid(spec_or_model: Any) -> pd.DatetimeIndex | dict | None:
    """Return the time points of a flex-pse spec or model, per time block.

    For a flowsheet spec, the points ``flexops`` builds the model's time block
    on: from ``start_date`` (inclusive) to ``end_date`` (exclusive) every
    ``time_step``, in naive local time. For a built model, its time block's.

    A model with several time blocks has a time axis per **scope**, the block
    that holds each ``TimeBlock``. A design spec has one per horizon,
    ``horizon[<year>,<month>]``, with that horizon's dates; so does the merged
    model it builds. A hand-built model with ``m.plant_a.time_block`` and
    ``m.plant_b.time_block`` has scopes ``plant_a`` and ``plant_b``.

    Args:
        spec_or_model: A ``FlowsheetSpec`` or ``DesignSpec`` as JSON, or a
            built model. Not modified.

    Returns:
        The points, for one time block; ``{scope: points}`` for several;
        ``None`` if there's no time axis.

    Raises:
        SpecError: If a flowsheet has two time elements, the dates or step
            don't make a whole number of steps, or one block holds two time
            blocks.

    Example:
        >>> time_grid(spec)[:2]  # doctest: +SKIP
        DatetimeIndex(['2025-07-08 00:00:00', '2025-07-08 01:00:00'], ...)
        >>> sorted(time_grid(design_spec))  # doctest: +SKIP
        ['horizon[2030,1]', 'horizon[2030,7]', 'horizon[2035,1]', 'horizon[2035,7]']
    """
    if not isinstance(spec_or_model, Mapping):
        return _of_model(spec_or_model)
    if is_design_spec(spec_or_model):
        return _of_design(spec_or_model)
    return _of_spec(spec_or_model)


def horizon_scope(year: int, month: int) -> str:
    """Return the path of a design study's horizon block, as Pyomo names it."""
    return f"horizon[{year},{month}]"


def time_scopes(model: Any) -> dict[str, Any]:
    """Return ``{scope: block}`` for every block of ``model`` holding a time block.

    The scope is the block's path from ``model`` (``""`` for the model
    itself).

    Raises:
        SpecError: If one block holds two time blocks.
    """
    scopes: dict[str, Any] = {}
    for block in model.component_data_objects(pyo.Block, descend_into=True):
        if not isinstance(block, TimeBlockData):
            continue
        owner = block.parent_block()
        scope = (
            ""
            if owner is model
            else owner.getname(fully_qualified=True, relative_to=model)
        )
        if scope in scopes:
            raise SpecError(
                f"{owner.name!r} holds two time blocks; give each its own block."
            )
        scopes[scope] = owner
    return scopes


def _of_spec(spec: Mapping[str, Any]) -> pd.DatetimeIndex | None:
    found = [e for e in spec.get("elements", []) if e.get("kind") == "time"]
    if not found:
        return None
    if len(found) > 1:
        raise SpecError("The spec has more than one time element.")
    [element] = found
    try:
        start = datetime.datetime.fromisoformat(element["start_date"])
        end = datetime.datetime.fromisoformat(element["end_date"])
        seconds = pyo.value(
            pyunits.convert(parse_quantity(element["time_step"]), pyunits.s)
        )
    except Exception as exc:
        raise SpecError(f"Can't read the spec's time element: {exc}") from exc
    if start.tzinfo is not None or end.tzinfo is not None:
        raise SpecError("The time element's dates must be naive local time.")
    step = pd.Timedelta(seconds=seconds)
    n, remainder = divmod(pd.Timestamp(end) - pd.Timestamp(start), step)
    if remainder != pd.Timedelta(0) or n <= 0:
        raise SpecError(
            f"The time element's horizon {start} -> {end} isn't a whole number of "
            f"{element['time_step']} steps."
        )
    return pd.date_range(start, periods=n, freq=step)


def _of_design(spec: Mapping[str, Any]) -> dict[str, pd.DatetimeIndex | None]:
    # Imported here: flexpse.design imports flexdesign, which a flowsheet-only
    # install may not have.
    from flexdesign.build import expand_horizons

    from flexorchestrator.flexpse.design import load_design

    return {
        horizon_scope(year, month): _of_spec(horizon.model_dump(mode="json"))
        for (year, month), horizon in expand_horizons(load_design(spec)).items()
    }


def _of_model(model: Any) -> pd.DatetimeIndex | dict | None:
    scopes = time_scopes(model)
    grids = {
        scope: pd.DatetimeIndex(_time_block(block).datetime_index)
        for scope, block in scopes.items()
    }
    if len(grids) == 1:
        return next(iter(grids.values()))
    return grids or None


def _time_block(block: Any) -> TimeBlockData:
    return next(
        b
        for b in block.component_data_objects(pyo.Block, descend_into=False)
        if isinstance(b, TimeBlockData)
    )
