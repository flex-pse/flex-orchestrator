"""User-replaceable build and solve steps.

The orchestrator manages storage and runs; it never requires flex-pse to build
or solve a model. A run is ``build(spec, **overrides)`` followed by
``solve(model, **options)``, and both steps are *hooks*: plain callables that
follow :class:`BuildFn` and :class:`SolveFn`. Two optional hooks change the
surrogate a unit uses, either in the spec before building
(:class:`ApplySurrogateFn`) or on a live model (:class:`SwapSurrogateFn`),
one sets a configuration option in a spec (:class:`ConfigureFn`), and one
names a built model's time Sets for the exporter (:class:`TimeSetsFn`).

A hook can be given three ways, and a :class:`HookSet` slot accepts any of them:

1. a callable;
2. a name registered with :meth:`HookRegistry.register` (``"my_model"``), which
   picks that set's hook of the same kind;
3. an import string, ``"package.module:function"``. Ray workers and the HTTP
   API need this form, because they can't receive a callable.

The flex-pse defaults are :data:`DEFAULT_HOOKS`. They are import strings, so
this module doesn't import flex-pse (or pyomo) and works without it.
"""

import importlib
import time
import traceback
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, computed_field

from flexorchestrator.errors import HookError

SolveStatus = Literal["ok", "failed", "error"]
"""How a solve ended. ``"ok"`` (optimal), ``"failed"`` (the solver finished
without an optimal solution) or ``"error"`` (the solve raised)."""


class SolveOutcome(BaseModel):
    """The result of one solve, as stored with its run.

    Attributes:
        status: ``"ok"``, ``"failed"`` or ``"error"`` (see :data:`SolveStatus`).
        termination: The solver's termination condition, e.g. ``"optimal"`` or
            ``"infeasible"``; ``None`` when the solve raised.
        message: A human-readable message from the solver or the exception.
        wall_time: Seconds the solve took. :func:`run_solve` fills it in when
            the hook leaves it unset.
        solver: The solver's name, e.g. ``"highs"``.
        log: Solver output or the traceback of a solve that raised.

    Example:
        >>> outcome = SolveOutcome(status="ok", termination="optimal")
        >>> outcome.ok
        True
    """

    model_config = ConfigDict(frozen=True)

    status: SolveStatus
    termination: str | None = None
    message: str | None = None
    wall_time: float | None = None
    solver: str | None = None
    log: str | None = None

    @computed_field
    @property
    def ok(self) -> bool:
        """Whether the solve found an optimal solution."""
        return self.status == "ok"

    @classmethod
    def from_stored(cls, stored: Mapping[str, Any] | None) -> "SolveOutcome | None":
        """Return the outcome a run stored (its JSON), or ``None`` if there is none.

        The stored ``ok`` is dropped: it is computed from ``status``.

        Example:
            >>> stored = SolveOutcome(status="ok").model_dump(mode="json")
            >>> SolveOutcome.from_stored(stored).ok
            True
        """
        if stored is None:
            return None
        return cls.model_validate({k: v for k, v in stored.items() if k != "ok"})


class BuildFn(Protocol):
    """Builds a model from a stored spec.

    Example:
        >>> def build(spec: dict, **overrides) -> dict:
        ...     return {**spec, **overrides}
    """

    def __call__(self, spec: dict, **overrides: Any) -> Any:
        """Build a model.

        Args:
            spec: The JSON model spec, exactly as stored.
            **overrides: Run-level values keyed by dotted path, e.g.
                ``{"costing.electricity_price": 0.1}``. What a path means is up
                to the hook; the flex-pse default treats it as a model
                component path.

        Returns:
            The built model: a Pyomo model, or anything the solve and export
            hooks understand.
        """


class SolveFn(Protocol):
    """Solves a built model.

    Example:
        >>> def solve(model, **options) -> SolveOutcome:
        ...     return SolveOutcome(status="ok", termination="optimal")
    """

    def __call__(self, model: Any, **options: Any) -> SolveOutcome:
        """Solve ``model`` in place.

        Args:
            model: The model a :class:`BuildFn` returned.
            **options: Solver options from the study config.

        Returns:
            How the solve ended. A hook may raise instead of returning an
            ``"error"`` outcome; :func:`run_solve` records the exception.
        """


class ApplySurrogateFn(Protocol):
    """Puts a surrogate into a spec, so the next build uses it.

    Example:
        >>> def apply_surrogate(spec, unit, surrogate_spec, relation=None) -> dict:
        ...     return {**spec, "surrogates": {unit: surrogate_spec}}
    """

    def __call__(
        self,
        spec: dict,
        unit: str,
        surrogate_spec: dict,
        relation: str | None = None,
    ) -> dict:
        """Return a copy of ``spec`` whose ``unit`` uses ``surrogate_spec``.

        Args:
            spec: The model spec. Must not be modified.
            unit: The unit's path in the model, e.g. ``"plant.digester"``.
            surrogate_spec: The JSON surrogate spec.
            relation: The unit's relation to replace, e.g. ``"biogas_relation"``;
                ``None`` means the hook's default.

        Returns:
            The new spec.
        """


class SwapSurrogateFn(Protocol):
    """Swaps a surrogate into a model that is already built.

    Example:
        >>> def swap_surrogate(model, unit, surrogate_spec, relation=None) -> None:
        ...     model.surrogates[unit] = surrogate_spec
    """

    def __call__(
        self,
        model: Any,
        unit: str,
        surrogate_spec: dict,
        relation: str | None = None,
    ) -> None:
        """Replace ``relation`` on ``unit`` with ``surrogate_spec``, in place.

        Args:
            model: The built model.
            unit: The unit's path in the model, e.g. ``"plant.digester"``.
            surrogate_spec: The JSON surrogate spec.
            relation: The relation to replace, e.g. ``"biogas_relation"``;
                ``None`` means the hook's default.
        """


class ConfigureFn(Protocol):
    """Sets one configuration option in a spec, so the next build uses it.

    Example:
        >>> def configure(spec, path, value) -> dict:
        ...     return {**spec, path: value}
    """

    def __call__(self, spec: dict, path: str, value: Any) -> dict:
        """Return a copy of ``spec`` with the option at ``path`` set to ``value``.

        Args:
            spec: The model spec. Must not be modified.
            path: The option's dotted path, e.g. ``"plant.feed.max_withdrawal"``.
                What it means is up to the hook; the flex-pse default reads it
                as an element's name followed by one of its options.
            value: The option's new value; any JSON value, a dict included.

        Returns:
            The new spec.
        """


class TimeSetsFn(Protocol):
    """Names a built model's time Sets, for the exporter.

    Example:
        >>> def time_sets(model) -> list:
        ...     return [model.t]
    """

    def __call__(self, model: Any) -> list[Any]:
        """Return the model's time Sets, main one first.

        Args:
            model: The built model.

        Returns:
            The Pyomo Sets that are time, compared by identity: the main time
            Set, then any Sets indexing only part of it (e.g. every point but
            the first). The exporter maps their values to the main Set's
            timestamps.
        """


class TimeGridFn(Protocol):
    """Gives a model's time axis, so stored data can be aligned to it.

    Example:
        >>> import pandas as pd
        >>> def time_grid(spec_or_model):
        ...     return pd.date_range("2025-07-08", periods=24, freq="1h")
    """

    def __call__(self, spec_or_model: Any) -> Any:
        """Return the model's time points, or ``None`` if it has no time axis.

        Args:
            spec_or_model: A model spec (a dict: a study reads the time steps
                before it builds) or a built model. Must not be modified.

        Returns:
            A naive pandas ``DatetimeIndex`` in the model's local time, one
            entry per time point with a fixed ``freq``, or ``None``. A model
            with several time blocks returns ``{scope: DatetimeIndex}``, where
            the scope is the path of the block holding each time block
            (``"horizon[2030,7]"``); each model input path is then lined up
            with the nearest one above it (:mod:`flexorchestrator.inputs.scopes`).
        """


class ApplyValuesFn(Protocol):
    """Fixes variables of a built model to values.

    ``orch.apply_model_inputs`` uses it to set stored data on a model that is
    already built.

    Example:
        >>> def apply_values(model, values) -> None:
        ...     for path, value in values.items():
        ...         model.find_component(path).fix(value)
    """

    def __call__(self, model: Any, values: dict[str, Any]) -> None:
        """Fix each path's variable to its value, in place.

        Args:
            model: The built model.
            values: ``{variable path: value}``, as a build hook's overrides
                take them: a number, one number per time point, or
                ``{"value": ..., "units": ...}``.
        """


HookRef = Any
"""A hook as given by a user: a callable, an import string or a registered name."""


@dataclass(frozen=True)
class HookSet:
    """The hooks for a run, one slot per kind in :data:`HOOK_KINDS`.

    Each slot holds a callable, an import string, a registered name, or
    ``None`` (not set). :meth:`HookRegistry.resolve` turns every slot into a
    callable.

    Attributes:
        build: The :class:`BuildFn`.
        solve: The :class:`SolveFn`.
        apply_surrogate: The optional :class:`ApplySurrogateFn`.
        swap_surrogate: The optional :class:`SwapSurrogateFn`.
        time_sets: The optional :class:`TimeSetsFn`.
        configure: The optional :class:`ConfigureFn`.
        time_grid: The optional :class:`TimeGridFn`.
        apply_values: The optional :class:`ApplyValuesFn`.

    Example:
        >>> hooks = DEFAULT_HOOKS.merged(HookSet(build="mypkg.models:build"))
        >>> hooks.build
        'mypkg.models:build'
    """

    build: HookRef = None
    solve: HookRef = None
    apply_surrogate: HookRef = None
    swap_surrogate: HookRef = None
    time_sets: HookRef = None
    configure: HookRef = None
    time_grid: HookRef = None
    apply_values: HookRef = None

    def merged(self, other: "HookSet") -> "HookSet":
        """Return these hooks with every slot ``other`` sets taking precedence.

        This is how the precedence call arguments > study config > orchestrator
        defaults is applied: ``defaults.merged(config).merged(arguments)``.

        Args:
            other: The hooks that win where they are set.

        Returns:
            The combined hooks.
        """
        return replace(
            self,
            **{kind: ref for kind, ref in other.slots().items() if ref is not None},
        )

    def slots(self) -> dict[str, HookRef]:
        """Return ``{kind: hook}`` for every slot, set or not."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def to_refs(self) -> dict[str, str]:
        """Return every set slot as an import string.

        For callers that need portable hooks, which a Ray worker or another
        process can import. Resolve registered names first, with
        :meth:`HookRegistry.resolve`.

        Returns:
            ``{kind: "package.module:function"}`` for every slot that is set.

        Raises:
            HookError: If a slot holds a callable that can't be imported back
                (a lambda, a closure, or a function defined in ``__main__``),
                or a registered name.

        Example:
            >>> DEFAULT_HOOKS.to_refs()["build"]
            'flexorchestrator.flexpse:build'
        """
        refs = {}
        for kind, hook in self.slots().items():
            if hook is None:
                continue
            if isinstance(hook, str):
                if ":" not in hook:
                    raise HookError(
                        f"The {kind} hook is the registered name {hook!r}; "
                        "resolve it with HookRegistry.resolve() first."
                    )
                refs[kind] = hook
                continue
            ref = hook_ref(hook)
            if ref is None:
                raise HookError(
                    f"The {kind} hook {hook!r} can't be imported by name, so it "
                    "can't run on a Ray worker or be recorded with the run. "
                    "Define it at module level in an importable module, or "
                    "pass its import string ('package.module:function')."
                )
            refs[kind] = ref
        return refs


HOOK_KINDS = tuple(f.name for f in fields(HookSet))
"""tuple[str, ...]: The hook slots a :class:`HookSet` has: ``build``, ``solve``,
``apply_surrogate``, ``swap_surrogate``, ``time_sets``, ``configure``,
``time_grid`` and ``apply_values``."""

DEFAULT_HOOKS = HookSet(
    build="flexorchestrator.flexpse:build",
    solve="flexorchestrator.flexpse:solve",
    apply_surrogate="flexorchestrator.flexpse:apply_surrogate",
    swap_surrogate="flexorchestrator.flexpse:swap_surrogate",
    time_sets="flexorchestrator.flexpse:time_sets",
    configure="flexorchestrator.flexpse:configure",
    time_grid="flexorchestrator.flexpse:time_grid",
    apply_values="flexorchestrator.flexpse:apply_values",
)
"""HookSet: The flex-pse hooks, used when nothing else is configured."""


def import_hook(ref: str) -> Any:
    """Import the callable an import string names.

    Args:
        ref: ``"package.module:attribute"``; the attribute may be dotted
            (``"package.module:Class.method"``).

    Returns:
        The callable.

    Raises:
        HookError: If ``ref`` is malformed, the module can't be imported, the
            attribute doesn't exist, or it isn't callable.

    Example:
        >>> import_hook("flexorchestrator.testing:fake_build").__name__
        'fake_build'
    """
    module_name, _, attr_path = ref.partition(":")
    if not module_name or not attr_path:
        raise HookError(
            f"{ref!r} is not an import string; write it as 'package.module:function'."
        )
    try:
        obj = importlib.import_module(module_name)
    except ImportError as exc:
        hint = ""
        if module_name.split(".")[:2] == ["flexorchestrator", "flexpse"]:
            hint = (
                " The default hooks need flex-pse: install it, or configure "
                "your own build and solve hooks."
            )
        raise HookError(
            f"Could not import {module_name!r} for hook {ref!r}: {exc}.{hint}"
        ) from exc
    for part in attr_path.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            raise HookError(
                f"{module_name!r} has no attribute {attr_path!r} (hook {ref!r})."
            ) from exc
    if not callable(obj):
        raise HookError(f"Hook {ref!r} is a {type(obj).__name__}, not a callable.")
    return obj


def hook_ref(fn: Any) -> str | None:
    """Return the import string that resolves back to ``fn``, if there is one.

    Args:
        fn: A callable.

    Returns:
        ``"module:qualname"``, or ``None`` when importing that string wouldn't
        give back ``fn`` (lambdas, closures, bound methods, functions defined in
        ``__main__``).

    Example:
        >>> from flexorchestrator.testing import fake_build
        >>> hook_ref(fake_build)
        'flexorchestrator.testing.hooks:fake_build'
    """
    module = getattr(fn, "__module__", None)
    qualname = getattr(fn, "__qualname__", None)
    if not module or not qualname or module == "__main__" or "<" in qualname:
        return None
    ref = f"{module}:{qualname}"
    try:
        return ref if import_hook(ref) is fn else None
    except HookError:
        return None


class HookRegistry:
    """Named hook sets, and resolution of any :class:`HookSet` to callables.

    Example:
        >>> registry = HookRegistry()
        >>> _ = registry.register(
        ...     "fake",
        ...     build="flexorchestrator.testing:fake_build",
        ...     solve="flexorchestrator.testing:fake_solve",
        ... )
        >>> registry.resolve(HookSet(build="fake")).build.__name__
        'fake_build'
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self._sets: dict[str, HookSet] = {}

    def register(
        self,
        name: str,
        *,
        build: HookRef = None,
        solve: HookRef = None,
        apply_surrogate: HookRef = None,
        swap_surrogate: HookRef = None,
        time_sets: HookRef = None,
        configure: HookRef = None,
        time_grid: HookRef = None,
        apply_values: HookRef = None,
        replace: bool = False,
    ) -> HookSet:
        """Register a named set of hooks.

        Args:
            name: The name a :class:`HookSet` slot or a study config uses to
                refer to this set. Must not contain ``":"``.
            build: A callable or import string.
            solve: A callable or import string.
            apply_surrogate: A callable or import string.
            swap_surrogate: A callable or import string.
            time_sets: A callable or import string.
            configure: A callable or import string.
            time_grid: A callable or import string.
            apply_values: A callable or import string.
            replace: Allow replacing a set already registered under ``name``.

        Returns:
            The registered set.

        Raises:
            HookError: If ``name`` is empty, contains ``":"`` or is already
                registered (without ``replace``), or a hook is neither a
                callable nor an import string.
        """
        if not name or ":" in name:
            raise HookError(
                f"Hook set name {name!r} must be non-empty and can't contain "
                "':' (that marks an import string)."
            )
        if name in self._sets and not replace:
            raise HookError(
                f"Hooks {name!r} are already registered; pass replace=True to "
                "replace them."
            )
        hooks = HookSet(
            build,
            solve,
            apply_surrogate,
            swap_surrogate,
            time_sets,
            configure,
            time_grid,
            apply_values,
        )
        for kind, hook in hooks.slots().items():
            if hook is None or callable(hook):
                continue
            if not isinstance(hook, str) or ":" not in hook:
                raise HookError(
                    f"The {kind} hook for {name!r} must be a callable or an "
                    f"import string ('package.module:function'), got {hook!r}."
                )
        self._sets[name] = hooks
        return hooks

    def names(self) -> list[str]:
        """Return the registered names, sorted."""
        return sorted(self._sets)

    def resolve_ref(self, ref: HookRef, kind: str) -> Any:
        """Resolve one hook to a callable.

        Args:
            ref: A callable, an import string or a registered name.
            kind: Which slot this is for (one of :data:`HOOK_KINDS`); picks the
                hook out of a registered set.

        Returns:
            The callable.

        Raises:
            HookError: If ``kind`` is unknown, the name isn't registered, its
                set has no hook of this kind, or an import string fails.
        """
        if kind not in HOOK_KINDS:
            raise HookError(
                f"Unknown hook kind {kind!r}; expected one of {HOOK_KINDS}."
            )
        if callable(ref):
            return ref
        if not isinstance(ref, str):
            raise HookError(
                f"The {kind} hook must be a callable, an import string or a "
                f"registered name, got {ref!r}."
            )
        if ":" in ref:
            return import_hook(ref)
        hooks = self._sets.get(ref)
        if hooks is None:
            raise HookError(
                f"No hooks are registered as {ref!r}. Registered: "
                f"{self.names() or 'none'}. Write an import string as "
                "'package.module:function'."
            )
        hook = getattr(hooks, kind)
        if hook is None:
            raise HookError(f"Hooks {ref!r} have no {kind} hook.")
        return hook if callable(hook) else import_hook(hook)

    def resolve(self, hooks: HookSet) -> HookSet:
        """Resolve every set slot of ``hooks`` to a callable.

        Args:
            hooks: The hooks to resolve.

        Returns:
            A :class:`HookSet` holding callables (unset slots stay ``None``).

        Raises:
            HookError: If any slot fails to resolve (see :meth:`resolve_ref`).
        """
        return HookSet(
            **{
                kind: None if ref is None else self.resolve_ref(ref, kind)
                for kind, ref in hooks.slots().items()
            }
        )


def run_solve(solve: SolveFn, model: Any, **options: Any) -> SolveOutcome:
    """Call a solve hook and always come back with a :class:`SolveOutcome`.

    An exception from the hook becomes an ``"error"`` outcome carrying the
    traceback, so one failed solve doesn't end a sweep. ``wall_time`` is filled
    in when the hook didn't set it.

    Args:
        solve: The resolved solve hook.
        model: The built model.
        **options: Passed to the hook.

    Returns:
        The hook's outcome, or an ``"error"`` outcome if it raised.

    Raises:
        HookError: If the hook returns something other than a
            :class:`SolveOutcome` (a bug in the hook, not a failed solve).

    Example:
        >>> from flexorchestrator.testing import fake_solve
        >>> run_solve(fake_solve, model=None).status
        'ok'
    """
    start = time.perf_counter()
    try:
        outcome = solve(model, **options)
    except Exception as exc:
        return SolveOutcome(
            status="error",
            message=f"{type(exc).__name__}: {exc}",
            wall_time=time.perf_counter() - start,
            log=traceback.format_exc(),
        )
    if not isinstance(outcome, SolveOutcome):
        raise HookError(
            f"Solve hook {solve!r} returned {type(outcome).__name__}; it must "
            "return a flexorchestrator.hooks.SolveOutcome."
        )
    if outcome.wall_time is None:
        outcome = outcome.model_copy(update={"wall_time": time.perf_counter() - start})
    return outcome
