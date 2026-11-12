"""The flex-pse build hook: flat spec + overrides -> built Pyomo model."""

import copy
from collections.abc import Mapping
from typing import Any

import pyomo.environ as pyo
from flexops import apply_spec, build_model

from flexorchestrator.errors import SpecError
from flexorchestrator.specs import is_design_spec


def build(spec: dict, **overrides: Any) -> pyo.ConcreteModel:
    """Build a flex-pse model from a stored flowsheet spec.

    The overrides are added to the spec as ``fix`` elements
    (:func:`with_overrides`), then :func:`flexops.build_model` validates and
    builds it, arcs expanded.

    Relative paths in the spec (``tariff_source``, ``$source`` files) resolve
    against the working directory of the process that builds, which on Ray is
    the worker's.

    Args:
        spec: A ``FlowsheetSpec`` as JSON (schema ``0.1.1``; older specs migrate
            on load), or a ``DesignSpec``, which
            :func:`~flexorchestrator.flexpse.build_design` builds.
        **overrides: Values to fix, keyed by variable path from the model root
            (see :func:`with_overrides`).

    Returns:
        The model, ready to solve.

    Raises:
        flexcore.exceptions.FlexConfigError: If the spec is invalid or an
            override names no variable; the message names the offending field.

    Example:
        >>> model = build(spec, **{"demo.pump.flow_in": 10.0})  # doctest: +SKIP
    """
    if is_design_spec(spec):
        # Imported here: flexpse.design builds on with_overrides.
        from flexorchestrator.flexpse.design import build_design

        return build_design(spec, **overrides)
    return build_model(with_overrides(spec, overrides))


def with_overrides(spec: dict, overrides: Mapping[str, Any]) -> dict:
    """Return a copy of ``spec`` with one ``fix`` element per override.

    A path whose first part names a ``costing`` element and whose next part is
    one of its fields (``"costing.energy_prices.electrical"``) edits that
    field instead: that's how a price is swept.

    Args:
        spec: A ``FlowsheetSpec`` as JSON. Not modified.
        overrides: ``{variable path: value}``. The path is a Pyomo ComponentUID
            path from the model root (``"demo.pump.flow_in"``,
            ``"demo.pump.flow_in[3]"``, ``"demo.ro[*].status"``). The value is
            anything a ``fix`` element takes: a number in the variable's own
            units, ``{"value": ..., "units": ...}``, a list with one number per
            time point, or a ``{time index: value}`` mapping.

    Returns:
        The new spec. Its ``fix`` elements are named ``"override:<path>"``.

    Example:
        >>> with_overrides(spec, {"demo.pump.flow_in": 10.0})  # doctest: +SKIP
    """
    elements = copy.deepcopy(spec["elements"])
    costing = {e["name"]: e for e in elements if e.get("kind") == "costing"}
    fixes = []
    for path, value in overrides.items():
        head, _, rest = path.partition(".")
        if head in costing and rest.partition(".")[0] in costing[head]:
            _set_field(costing[head], rest.split("."), value, path)
            continue
        element = {"kind": "fix", "name": f"override:{path}", "variable": path}
        if isinstance(value, Mapping) and set(value) == {"value", "units"}:
            element.update(value)
        else:
            element["value"] = value
        fixes.append(element)
    return {**spec, "elements": [*elements, *fixes]}


def apply_values(model: pyo.Block, values: Mapping[str, Any]) -> None:
    """Fix variables of a built flex-pse model, as ``fix`` elements would.

    In a model with several time blocks (a merged design model, or one with
    ``plant_a.time_block`` and ``plant_b.time_block``), each path is fixed
    against the nearest time block above it: the values are grouped by the
    block that holds that time block (its scope), and each group is applied
    to that block with paths relative to it.

    The values are checked against the variables' bounds and applied together
    (:func:`flexops.apply_spec`), all or nothing for each time block. A later
    apply of a point replaces an earlier one.

    Args:
        model: The built model, changed in place.
        values: ``{variable path: value}``, as :func:`with_overrides` takes
            them. A costing field (a price) can't be set on a built model; set
            it before building, as a configuration option.

    Raises:
        flexcore.exceptions.FlexConfigError: If a path names no variable, or a
            value doesn't fit it.
        SpecError: If the model has several time blocks and a path is under
            none of them.
    """
    # Imported here: flexpse.timegrid imports this module's package.
    from flexorchestrator.flexpse.timegrid import time_scopes
    from flexorchestrator.inputs.scopes import scope_of

    scopes = time_scopes(model)
    if len(scopes) <= 1:
        apply_spec(model, with_overrides({"elements": []}, values)["elements"])
        return
    grouped: dict[str, dict[str, Any]] = {}
    for path, value in values.items():
        scope = scope_of(path, scopes)
        if scope is None:
            raise SpecError(
                f"Can't fix {path!r}: the model has several time blocks and it is "
                f"under none of them. Name it from one of {sorted(scopes)}."
            )
        relative = path[len(scope) + 1 :] if scope else path
        grouped.setdefault(scope, {})[relative] = value
    for scope, group in grouped.items():
        apply_spec(scopes[scope], with_overrides({"elements": []}, group)["elements"])


def refuse_design_model(model: pyo.Block, action: str) -> None:
    """Raise if ``model`` is a merged design model; flex-pse can't edit one whole."""
    if hasattr(model, "_design_spec"):
        raise SpecError(
            f"Can't {action} a merged design model. Vary it with the design's "
            "changes before building."
        )


def configure(spec: dict, path: str, value: Any) -> dict:
    """Return a copy of ``spec`` with one configuration option set.

    ``path`` is an element's name followed by the option: the element is the
    one whose name is the longest leading part of ``path``, so this works for
    any unit or block, however deeply named (``"plant.train_1.ro.recovery"``).
    A unit's options are its ``construction_options``; any other element's are
    its own fields (``"costing.energy_prices.electrical"``,
    ``"properties.options.default_temperature"``).

    Dict-valued options can be set whole or one key at a time: the value
    ``{"value": 200, "units": "m^3/hr"}`` at ``"plant.feed.max_withdrawal"``
    replaces the option, and ``200`` at ``"plant.feed.max_withdrawal.value"``
    changes only its number. Missing dicts on the way are created, so an option
    the spec leaves at its default can be set too. The option isn't checked
    here; flex-pse validates the spec when it's stored or built.

    In a design study, ``capacities.<name>.<field>`` sets a field of that
    capacity (``"capacities.battery_energy.capital_cost"``), and any other path
    is an option of the ``base`` flowsheet.

    Args:
        spec: A ``FlowsheetSpec`` or ``DesignSpec`` as JSON. Not modified.
        path: The element's name, then the option's dotted path within it.
        value: The new value: a number, a string, a list or a dict.

    Returns:
        The new spec.

    Raises:
        SpecError: If no element's name starts ``path``, ``path`` names only
            an element, or a part of the path on the way isn't a dict.

    Example:
        >>> configure(spec, "plant.feed.max_withdrawal.value", 200)  # doctest: +SKIP
    """
    if is_design_spec(spec):
        return _configure_design(spec, path, value)
    elements = copy.deepcopy(spec["elements"])
    named = {e["name"]: e for e in elements if "name" in e}
    parts = path.split(".")
    for cut in range(len(parts), 0, -1):
        element = named.get(".".join(parts[:cut]))
        if element is not None:
            break
    else:
        raise SpecError(
            f"Configuration option {path!r} doesn't start with an element's "
            f"name. Elements: {sorted(named)}."
        )
    if cut == len(parts):
        raise SpecError(
            f"Configuration option {path!r} names the element itself; add the "
            "option, e.g. 'plant.feed.max_withdrawal'."
        )
    if element.get("kind") == "unit":
        options = element.setdefault("construction_options", {})
    else:
        options = element
    _set_field(options, parts[cut:], value, path)
    return {**spec, "elements": elements}


def _configure_design(spec: dict, path: str, value: Any) -> dict:
    """Set a capacity's field, or an option of the design's base flowsheet."""
    parts = path.split(".")
    if parts[0] != "capacities":
        return {**spec, "base": configure(spec["base"], path, value)}
    if any(e.get("name") == "capacities" for e in spec["base"]["elements"]):
        raise SpecError(
            f"{path!r} is ambiguous: the base flowsheet has an element named "
            "'capacities'. Rename it to sweep the design's capacities."
        )
    capacities = copy.deepcopy(spec["capacities"])
    named = {c["name"]: c for c in capacities}
    if len(parts) < 3 or parts[1] not in named:
        raise SpecError(
            f"{path!r} doesn't name a capacity's field, as in "
            f"'capacities.<name>.capital_cost'. Capacities: {sorted(named)}."
        )
    _set_field(named[parts[1]], parts[2:], value, path)
    return {**spec, "capacities": capacities}


def _set_field(target: dict, parts: list[str], value: Any, path: str) -> None:
    """Set ``target[parts[0]][parts[1]]...``, creating missing dicts on the way."""
    *parents, last = parts
    for part in parents:
        child = target.setdefault(part, {})
        if not isinstance(child, dict):
            raise SpecError(
                f"Can't set {path!r}: {part!r} is {child!r}, not a dict of options."
            )
        target = child
    target[last] = value
