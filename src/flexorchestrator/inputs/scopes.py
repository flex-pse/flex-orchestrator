"""Time scopes: which time axis a model path is lined up with.

A model can have several time blocks: a merged design model has one per
horizon, ``horizon[2030,7].time_block``, and a hand-built model may give
``plant_a`` and ``plant_b`` one each. The block holding a time block is its
**scope**, named by its path from the model (``""`` for the model itself).

A path belongs to the **nearest time block above it**: the longest scope its
path starts with. A path under no scope is set in **every** scope, relative to
each, so ``plant.ro.permeate`` in a design study means that variable in every
horizon, each lined up with its own dates.

A ``*`` in an index of the scope part matches any value there:
``horizon[2030,*].plant.ro.permeate`` sets that variable in every 2030 horizon,
and ``horizon[*,7]...`` in every July. A ``*`` in the variable's own index
(``flow_vol_phase[*,Liq]``) is the variable's business, not a scope's.

Nothing here imports a model library: the ``time_grid`` hook gives the scopes
and their time points, and these functions only compare paths.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any

import pandas as pd

from flexorchestrator.errors import ModelInputError


def grids_of(found: Any) -> dict[str, pd.DatetimeIndex | None]:
    """Return a ``time_grid`` hook's answer as ``{scope: time points}``.

    The hook gives one ``DatetimeIndex`` for a model with one time axis, a
    mapping for several, or ``None`` for none; one axis is the scope ``""``.

    Raises:
        ModelInputError: If the hook returned something else, or an empty
            mapping.
    """
    if found is None or isinstance(found, pd.DatetimeIndex):
        return {"": found}
    if isinstance(found, Mapping) and found:
        return dict(found)
    raise ModelInputError(
        "The time_grid hook must return a DatetimeIndex, a non-empty "
        f"{{scope: DatetimeIndex}} mapping or None, not {found!r}."
    )


def scope_of(path: str, scopes: Iterable[str]) -> str | None:
    """Return the scope of the nearest time block above ``path``, or ``None``.

    Example:
        >>> scopes = ["horizon[2030,1]", "horizon[2030,7]"]
        >>> scope_of("horizon[2030,7].pump.flow", scopes)
        'horizon[2030,7]'
        >>> scope_of("pump.flow", scopes) is None
        True
    """
    best = None
    for scope in scopes:
        if scope == "" or path.startswith(scope + "."):
            if best is None or len(scope) > len(best):
                best = scope
    return best


def targets(path: str, scopes: Iterable[str]) -> list[tuple[str, str]]:
    """Return ``[(scope, model path)]``: where an entry's ``path`` is set.

    A path under a scope is set there, and one whose scope part has a ``*``
    in every scope it matches; one under none is set in every scope, as
    ``<scope>.<path>``.

    Raises:
        ModelInputError: If a ``*`` in the scope part matches no scope.

    Example:
        >>> targets("pump.flow", ["a", "b"])
        [('a', 'a.pump.flow'), ('b', 'b.pump.flow')]
        >>> targets("a.pump.flow", ["a", "b"])
        [('a', 'a.pump.flow')]
        >>> scopes = ["h[2030,1]", "h[2030,7]", "h[2035,1]"]
        >>> [scope for scope, _ in targets("h[2030,*].pump.flow", scopes)]
        ['h[2030,1]', 'h[2030,7]']
    """
    scopes = list(scopes)
    scope = scope_of(path, scopes)
    if scope is not None:
        return [(scope, path)]
    matched = _wildcard(path, scopes)
    if matched is not None:
        return matched
    return [(s, f"{s}.{path}") for s in scopes]


def _wildcard(path: str, scopes: list[str]) -> list[tuple[str, str]] | None:
    """The scopes a ``*`` in ``path``'s leading blocks matches, if it has one.

    Each leading part of the path that ends a block (before a ``.``, outside
    brackets) is tried, longest first, as a pattern where ``*`` is one index
    value. ``None`` if no such part has a ``*``.
    """
    cuts = [i for i, char in enumerate(path) if char == "." and _depth(path, i) == 0]
    patterns = [path[:i] for i in reversed(cuts) if "*" in path[:i]]
    if not patterns:
        return None
    for prefix in patterns:
        regex = re.compile(re.escape(prefix).replace(r"\*", r"[^,\[\]]+"))
        found = [s for s in scopes if regex.fullmatch(s)]
        if found:
            rest = path[len(prefix) :]
            return [(s, s + rest) for s in found]
    named = {s.split("[")[0] for s in scopes}
    if patterns[-1].split("[")[0] in named:
        raise ModelInputError(
            f"Model input path {path!r} matches no time block; they are "
            f"{sorted(scopes)}."
        )
    return None


def _depth(path: str, end: int) -> int:
    """How many brackets are open at ``end`` in ``path``."""
    return path.count("[", 0, end) - path.count("]", 0, end)
