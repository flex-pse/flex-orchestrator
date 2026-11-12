"""The flex-pse design study hooks: size equipment across many horizons.

A design study is one flowsheet (``base``) run over every year and representative
month, with shared capacities. :mod:`flexdesign` builds it as one merged model.
"""

from collections.abc import Mapping
from typing import Any

import pyomo.environ as pyo
from flexcore.config.design import DesignSpec
from flexcore.config.io import load_spec
from flexcore.exceptions import FlexConfigError
from flexdesign import build_design_model
from flexops import apply_spec
from pydantic import ValidationError

from flexorchestrator.errors import SpecError
from flexorchestrator.flexpse.build import with_overrides
from flexorchestrator.flexpse.timegrid import horizon_scope


def load_design(spec: Mapping[str, Any]) -> DesignSpec:
    """Return ``spec`` as a ``DesignSpec``, its base migrated by flex-pse's loader."""
    base = spec.get("base")
    if not isinstance(base, Mapping):
        raise SpecError(
            "A stored design spec holds its base flowsheet inline, not as "
            f"{type(base).__name__}."
        )
    try:
        return DesignSpec.model_validate({**spec, "base": load_spec(base)})
    except (FlexConfigError, ValidationError) as exc:
        raise SpecError(f"Invalid flex-pse design spec: {exc}") from exc


def validate_design_spec(spec: dict) -> dict:
    """Check a design spec with flex-pse, and normalise it.

    Args:
        spec: A ``DesignSpec`` as JSON, with ``base`` inline.

    Returns:
        The validated spec as JSON, with defaults written out and an older base
        schema migrated.

    Raises:
        SpecError: If flex-pse rejects it; the message names the bad field.
    """
    return load_design(spec).model_dump(mode="json", by_alias=True)


def build_design(spec: dict, **overrides: Any) -> pyo.ConcreteModel:
    """Build the merged model of a design study.

    Args:
        spec: A ``DesignSpec`` as JSON.
        **overrides: Values to fix, as :func:`build` takes them. A path that
            starts with a horizon, ``horizon[<year>,<month>].<path>``, is fixed in
            that horizon only, after the merged model is built, and may give a
            value per time point: that's how ``model_inputs`` sets each horizon
            from its own data. Any other path is fixed in every horizon, and
            must be one value, since horizons differ in length.

    Returns:
        The merged model, ready to solve.

    Raises:
        SpecError: If an override names a capacity's variable, which the study
            decides (fix its size with the capacity's ``lower`` and ``upper``),
            gives an unscoped value per time point, or names a horizon the
            study doesn't model.
    """
    sized = {c["variable"]: c["name"] for c in spec["capacities"]}
    horizons = {
        horizon_scope(year["year"], month["month"]): (year["year"], month["month"])
        for year in spec["years"]
        for month in spec["months"]
    }
    shared, per_horizon = {}, {}
    for path, value in overrides.items():
        scope, _, rest = path.partition("].")
        scope += "]"
        if path.startswith("horizon[") and rest:
            if scope not in horizons:
                raise SpecError(
                    f"Can't fix {path!r}: the design study models no {scope!r}. "
                    f"Horizons: {sorted(horizons)}."
                )
            per_horizon.setdefault(scope, {})[rest] = value
            _check_unsized(rest, sized)
            continue
        _check_unsized(path, sized)
        per_point = isinstance(value, list) or (
            isinstance(value, Mapping) and set(value) != {"value", "units"}
        )
        if per_point:
            raise SpecError(
                f"Can't fix {path!r} per time point in every horizon: horizons "
                "differ in length. Name one horizon, as "
                f"'horizon[<year>,<month>].{path}', or use the design's changes."
            )
        shared[path] = value
    base = with_overrides(spec["base"], shared)
    model = build_design_model(load_design({**spec, "base": base}))
    for scope, values in per_horizon.items():
        block = model.horizon[horizons[scope]]
        apply_spec(block, with_overrides({"elements": []}, values)["elements"])
    return model


def _check_unsized(path: str, sized: Mapping[str, str]) -> None:
    if path in sized:
        raise SpecError(
            f"Can't fix {path!r}: the design study sizes it as capacity "
            f"{sized[path]!r}. Set that capacity's lower and upper instead."
        )
