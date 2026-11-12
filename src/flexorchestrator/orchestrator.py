"""The orchestrator: studies, datasets and model results on a storage backend.

Model results are stored **explicitly**: every call names the model and the
tag, and there is no "current model". A study ``s`` is laid out as
backend studies: ``s`` itself; ``s::runs``, where each stored result is
one run carrying ``{flexorch_kind: "result", flexorch_tag: tag}``, the model
snapshot (:mod:`flexorchestrator.results`), any values stored natively, and
the ``solve_outcome`` JSON; ``s::data``, where each dataset is one run
(:mod:`flexorchestrator.data`); ``s::specs``, where each model or surrogate
spec is one run with the files it references
(:mod:`flexorchestrator.spec_store`); and ``s::fits``, where each surrogate
fit is one run with its training-data provenance
(:mod:`flexorchestrator.fitting`). Every run's metadata also records the
versions, the user, the hooks and the connection, so a result can be
reproduced.

Commands extend it: ``execute_study`` and ``fit_surrogate`` are built in,
plugins add more, and every call is recorded in ``flexorch::commands``.

Without a backend, the orchestrator connects to Acquirium itself, in one of
three modes (:mod:`flexorchestrator.connection`). To keep studies in a
folder on disk with no Acquirium, pass a
:class:`~flexorchestrator.backend.FileBackend`.
"""

import copy
import getpass
import os
import tempfile
import threading
import warnings
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal, Self

from flexorchestrator import data as datasets
from flexorchestrator import frames, results, spec_store
from flexorchestrator.assets import collect_assets, find_assets, materialize_spec
from flexorchestrator.backend.base import StorageBackend, to_utc
from flexorchestrator.connection import Connection, DBCredentials, connect
from flexorchestrator.data import DATA_KIND, StoredData
from flexorchestrator.errors import (
    AmbiguousColumnError,
    HookError,
    ModelInputError,
    SpecError,
    StorageError,
    StudyExistsError,
    StudyNotFoundError,
    TagExistsError,
    TagNotFoundError,
)
from flexorchestrator.export import ModelData, UnitMapper, export_model_data
from flexorchestrator.ext.commands import (
    COMMAND_KIND,
    Command,
    CommandRegistry,
    Progress,
    record_call,
)
from flexorchestrator.ext.events import EventBus
from flexorchestrator.ext.plugins import (
    Plugin,
    hook_sets,
    listeners,
    load_plugins,
)
from flexorchestrator.hooks import (
    DEFAULT_HOOKS,
    HookRegistry,
    HookSet,
    SolveOutcome,
    hook_ref,
    run_solve,
)
from flexorchestrator.results import MODEL_MANIFEST, MODEL_VALUES
from flexorchestrator.specs import (
    SpecKind,
    SpecRecord,
    SpecRecordWithFiles,
    check_tag,
    is_design_spec,
    normalise_spec,
    run_hash,
    to_json_spec,
)

_VERSION_KEYS = {
    "flex-orchestrator": "flexorch_version",
    "flex-pse": "flexpse_version",
    "acquirium": "acquirium_version",
}
"""dict: Distribution -> the run metadata key recording its version."""
RESULT_KIND = "result"
MODEL_SPEC = "model_spec"
SURROGATE_SPEC = "surrogate_spec"
FIT_KIND = "fit"
LEDGERS = {
    RESULT_KIND: "runs",
    DATA_KIND: "data",
    MODEL_SPEC: "specs",
    SURROGATE_SPEC: "specs",
    FIT_KIND: "fits",
}
"""dict: Item kind -> the ledger study (``<s>::<ledger>``) that holds it."""
STORED_EVENTS = {
    RESULT_KIND: "result_stored",
    DATA_KIND: "data_stored",
    MODEL_SPEC: "spec_stored",
    SURROGATE_SPEC: "spec_stored",
    FIT_KIND: "fit_stored",
}
"""dict: Item kind -> the lifecycle event emitted once one is stored."""
BUILTIN_COMMANDS = ("execute_study", "fit_surrogate")
"""tuple[str, ...]: The commands every orchestrator has; plugins can't reuse them."""
MODEL_SPEC_REF = "model_spec_ref"
"""str: The label of a result's JSON input naming the spec it was built from."""
OVERRIDES = "overrides"
"""str: The label of a result's JSON input holding the build overrides."""
MODEL_INPUTS = "model_inputs"
"""str: The label of a result's JSON input recording where its model inputs came
from."""
SOLVE_OUTCOME = "solve_outcome"
"""str: The label of the JSON output that holds a result's solve outcome."""


class SolveNotOptimal(Exception):  # noqa: N818 (recorded as the run's error type)
    """Recorded as the error of a result run whose solve didn't succeed."""


@dataclass
class StoreItem:
    """An item about to be stored, as ``before_store`` sees it.

    See :meth:`~flexorchestrator.Orchestrator.before_store`.

    Attributes:
        kind: ``"result"``, ``"data"``, ``"model_spec"``, ``"surrogate_spec"``
            or ``"fit"``.
        tag: The item's tag (a dataset's name).
        metadata: Extra run metadata; ``before_store`` may add to it. It can't
            replace the orchestrator's own keys.
        run_id: The run, once stored (in ``after_store`` and the events).
    """

    kind: str
    tag: str
    metadata: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None


@dataclass(frozen=True)
class StoredResult:
    """What :meth:`Orchestrator.store_model_results` stored.

    Attributes:
        tag: The result's tag.
        run_id: The backend run.
        status: ``"succeeded"``, or ``"failed"`` when the solve wasn't ``ok``
            (then only the solve outcome is stored, and the tag can be reused).
        outcome: The solve outcome, if one was given.
        keys: The exported keys stored in the snapshot.
        streams: The keys also stored as native variables.
    """

    tag: str
    run_id: str
    status: Literal["succeeded", "failed"]
    outcome: SolveOutcome | None = None
    keys: list[str] = field(default_factory=list)
    streams: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the result was stored with its model values."""
        return self.status == "succeeded"


@dataclass(eq=False)
class ResultHandle(results.ResultReads):
    """A stored result, read lazily from its snapshot.

    The snapshot is read the first time values are asked for, then kept.

    Attributes:
        tag: The result's tag.
        run_id: The backend run.
        metadata: The run's metadata (``flexorch_tag``, versions, ...).
        started_at: When the run started (UTC).
    """

    tag: str
    run_id: str
    metadata: dict[str, Any]
    started_at: Any
    _orch: "Orchestrator" = field(repr=False)
    _loaded: tuple[ModelData, Any] | None = field(default=None, repr=False)

    @classmethod
    def _from_run(cls, orch: "Orchestrator", run: Any) -> "ResultHandle":
        return cls(
            run.metadata.get("flexorch_tag"),
            run.run_id,
            dict(run.metadata),
            run.started_at,
            orch,
        )

    def _snapshot(self) -> tuple[ModelData, Any]:
        if self._loaded is None:
            self._loaded = self._orch._read_snapshot(self.run_id)
        return self._loaded

    def data(self, units: Mapping[str, str] | None = None) -> ModelData:
        """Return the values, converted to ``units`` if given.

        Args:
            units: ``{key or component path: Pyomo unit}``, as in
                :meth:`Orchestrator.model_results`.
        """
        data = self._snapshot()[0]
        return results.convert(data, units) if units else data

    def to_frame(
        self,
        frame: frames.FrameKind | None = None,
        units: Mapping[str, str] | None = None,
    ) -> Any:
        """Return the timeseries as one table, a column per key.

        polars gets a ``time`` column first, pandas a UTC index. Scalars are in
        :meth:`~flexorchestrator.results.ResultReads.scalars`.

        Args:
            frame: ``"polars"`` or ``"pandas"``; the orchestrator's default if
                omitted.
            units: Units to convert to, as in :meth:`data`.
        """
        return frames.model_frame(self.data(units), frame or self._orch.frame)

    @property
    def spec_tag(self) -> str | None:
        """The tag of the model spec the result was built from, if recorded."""
        return self.metadata.get("spec_tag")

    @property
    def overrides(self) -> dict[str, Any]:
        """The build overrides recorded with the spec (``{}`` if none)."""
        found = self._orch.backend.records(
            self._orch._runs, self.run_id, labels=[OVERRIDES]
        )
        return dict(found[0].value) if found else {}

    @property
    def model_inputs(self) -> list[dict[str, Any]]:
        """Where its model inputs came from, per entry; ``[]`` if none.

        Each entry holds the entry as sent (``input``), what its selector
        resolved to (``resolved``: the dataset run, or the stream refs), the
        ``paths`` it set, the ``window`` read, the steps ``filled`` and the
        values' ``data_hash``. The values themselves are in :attr:`overrides`.
        """
        found = self._orch.backend.records(
            self._orch._runs, self.run_id, labels=[MODEL_INPUTS]
        )
        return list(found[0].value) if found else []

    def spec(self) -> SpecRecord:
        """Return the model spec the result was built from.

        Raises:
            TagNotFoundError: If no spec was recorded, or it was deleted.
        """
        if self.spec_tag is None:
            raise TagNotFoundError(f"Result {self.tag!r} records no model spec.")
        return self._orch.model_spec(self.spec_tag)

    @property
    def outcome(self) -> SolveOutcome | None:
        """The solve outcome stored with the result, if any."""
        return SolveOutcome.from_stored(self._snapshot()[1])


class Orchestrator:
    """Studies, model results and hooks on one storage backend.

    Without ``backend``, it connects to Acquirium in one of three modes:

    - ``Orchestrator()``: a local managed Acquirium with DuckDB in
      ``data_dir`` (``.flexorch`` by default);
    - ``Orchestrator(db="postgresql://host/db", data_dir=...)``: a managed
      Acquirium on Postgres, with the login from ``FLEXORCH_DB_PASSWORD_FILE``,
      ``FLEXORCH_DB_USER`` / ``FLEXORCH_DB_PASSWORD`` or libpq's ``PG*`` /
      ``~/.pgpass``, never from script code;
    - ``Orchestrator(acquirium_url="http://acquirium:8000")``: a running one.

    Args:
        backend: The store, e.g. :class:`~flexorchestrator.backend.FileBackend`
            (a folder on disk, no Acquirium),
            :class:`~flexorchestrator.backend.MemoryBackend` or
            :class:`~flexorchestrator.backend.AcquiriumBackend`. Leave it out
            to connect to Acquirium with the arguments below.
        study: A study to select; it must exist.
        data_dir: The managed Acquirium's directory (modes 1 and 2).
        db: A Postgres URL without a password (mode 2).
        acquirium_url: A running Acquirium (mode 3). ``FLEXORCH_ACQUIRIUM_URL``
            (from ``environ``) selects it for the whole process, which is how a
            container or a host application points the library at its
            Acquirium; while it's set, passing another store raises (see
            :func:`~flexorchestrator.connection.connect`).
        db_credentials: An explicit Postgres login, for developers and tests.
        hooks: Hooks that replace the flex-pse defaults, slot by slot.
        registry: Where registered hook names resolve.
        unit_mapper: Maps Pyomo units to QUDT. By default it verifies keys
            with the backend's ``resolve_unit``.
        strict_units: With the default mapper, raise on a unit with no QUDT
            key instead of storing it without one.
        frame: What reads return by default, ``"polars"`` or ``"pandas"``.
        allow_delete: Whether :meth:`delete`, :meth:`delete_study` and
            ``replace=True`` may delete anything. A deployment that must
            never lose a stored item turns this off.
        plugins: Plugins to load: :class:`~flexorchestrator.ext.Plugin`
            objects or installed plugin names. Plugins named in
            ``FLEXORCH_PLUGINS`` load too. Use
            :func:`~flexorchestrator.ext.create_orchestrator` to get a
            plugin's orchestrator subclass.
        environ: Where ``FLEXORCH_PLUGINS``, ``FLEXORCH_RAY_ADDRESS``,
            ``FLEXORCH_ACQUIRIUM_URL`` and ``FLEXORCH_START_ACQUIRIUM`` are
            read; ``os.environ`` by default.
        ray_address: The Ray cluster for studies with ``max_workers > 1``
            when their config names none; ``FLEXORCH_RAY_ADDRESS`` if
            unset, else local Ray.

    Attributes:
        user: Who runs and stores are recorded as (``flexorch_user``); if
            ``None``, ``FLEXORCH_USER`` or the login name. A host application
            sets it on each caller's study handle.

    Selecting a study (:meth:`select_study`, :meth:`create_study`,
    :meth:`get_or_create_study`) makes it this orchestrator's study, and also
    returns a **study handle**: a light copy fixed to that study, sharing the
    connection. A script keeps using the orchestrator; code that works in
    several studies at once, or from several threads, keeps the handle.

    Example:
        >>> from flexorchestrator.backend import MemoryBackend
        >>> orch = Orchestrator(MemoryBackend())
        >>> a = orch.create_study("example_a")
        >>> a.study, orch.study
        ('example_a', 'example_a')
    """

    def __init__(
        self,
        backend: StorageBackend | None = None,
        *,
        study: str | None = None,
        data_dir: str | os.PathLike[str] | None = None,
        db: str | None = None,
        acquirium_url: str | None = None,
        db_credentials: DBCredentials | None = None,
        hooks: HookSet | None = None,
        registry: HookRegistry | None = None,
        unit_mapper: UnitMapper | None = None,
        strict_units: bool = False,
        frame: frames.FrameKind = "polars",
        allow_delete: bool = True,
        plugins: Sequence[Plugin | str] | None = None,
        environ: Mapping[str, str] | None = None,
        ray_address: str | None = None,
    ) -> None:
        """Set up the orchestrator; see the class docstring for the arguments."""
        self.connection: Connection | None = None
        self.user: str | None = None
        self._handle = False
        self._root = self  # the orchestrator a study handle came from
        if backend is None:
            self.connection = connect(
                data_dir=data_dir,
                db=db,
                acquirium_url=acquirium_url,
                db_credentials=db_credentials,
                environ=environ,
            )
            backend = self.connection.backend()
        elif any(a is not None for a in (data_dir, db, acquirium_url, db_credentials)):
            raise ValueError(
                "Pass either a backend or connection arguments (data_dir, db, "
                "acquirium_url, db_credentials), not both."
            )
        self.backend = backend
        self.hooks = self.build_hooks().merged(hooks) if hooks else self.build_hooks()
        self.registry = registry or HookRegistry()
        self.unit_mapper = unit_mapper or UnitMapper(
            resolver=backend.resolve_unit, strict=strict_units
        )
        self.frame = frames.check_frame(frame)
        self.allow_delete = allow_delete
        env = os.environ if environ is None else environ
        self.ray_address = ray_address or env.get("FLEXORCH_RAY_ADDRESS") or None
        self._study: str | None = None
        self._locks: dict[str, threading.RLock] = {}
        self._locks_lock = threading.Lock()
        self._call = threading.local()
        self.events = EventBus()
        self.commands = CommandRegistry()
        for builtin in _builtin_commands():
            self.commands.register(builtin, source="flex-orchestrator")
        self.plugins = load_plugins(
            plugins, environ=environ, builtin_commands=BUILTIN_COMMANDS
        )
        for plugin in self.plugins:
            self._load_plugin(plugin)
        self._closed = False
        if study is not None:
            self.select_study(study)
        for plugin in self.plugins:
            if plugin.on_startup is not None:
                plugin.on_startup(self)

    def close(self) -> None:
        """Disconnect from an Acquirium this orchestrator connected to.

        A managed server this process started is stopped; its data is kept.
        A backend that was passed in is left alone. Plugins' ``on_shutdown``
        runs first, once. A study handle shares the connection, so closing it
        does nothing: close the orchestrator it came from.
        """
        if self._handle:
            return
        if not self._closed:
            self._closed = True
            for plugin in self.plugins:
                if plugin.on_shutdown is not None:
                    plugin.on_shutdown(self)
        if self.connection is not None:
            self.connection.close()

    def __enter__(self) -> "Orchestrator":
        """Return the orchestrator; :meth:`close` runs on exit."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the connection (see :meth:`close`)."""
        self.close()

    @property
    def acquirium(self) -> Any:
        """The backend's Acquirium client, for Acquirium's own API.

        The orchestrator doesn't wrap queries: use the client directly, e.g.
        ``orch.acquirium.study.get("example_a::runs").experiments.where(...)``.

        Raises:
            StorageError: If the backend isn't backed by Acquirium.
        """
        client = getattr(self.backend, "acquirium", None)
        if client is None:
            raise StorageError(
                f"{type(self.backend).__name__} has no Acquirium client; "
                "orch.acquirium needs the Acquirium backend."
            )
        return client

    # Studies

    @property
    def study(self) -> str:
        """The selected study.

        Raises:
            StudyNotFoundError: If no study is selected.
        """
        if self._study is None:
            raise StudyNotFoundError(
                "No study is selected; call create_study() or select_study() first."
            )
        return self._study

    def create_study(self, name: str) -> Self:
        """Create a study and select it.

        Returns:
            A handle fixed to the study (see :meth:`select_study`).

        Raises:
            StudyExistsError: If the study exists. Use
                :meth:`get_or_create_study` when either is fine.
        """
        check_tag(name)
        if self.backend.has_study(name):
            raise StudyExistsError(
                f"Study {name!r} exists; use select_study({name!r}) to work in it."
            )
        self.backend.define_study(name)
        self._define_ledgers(name)
        self._study = name
        self.on_study_created(name)
        self.events.emit("study_created", study=name)
        return self._study_handle(name)

    def select_study(self, name: str) -> Self:
        """Select an existing study, and return a handle fixed to it.

        The study becomes this orchestrator's, so a script goes on with
        ``orch``. The handle is a light copy that keeps working in ``name``
        whatever is selected on ``orch`` later, or from another thread: it
        shares the connection, plugins, hooks and the per-study locks. Set
        :attr:`user` on it to record who works through it. Closing a handle
        does nothing; close the orchestrator.

        Raises:
            StudyNotFoundError: If it doesn't exist.

        Example:
            >>> from flexorchestrator.backend import MemoryBackend
            >>> orch = Orchestrator(MemoryBackend())
            >>> a, b = orch.create_study("a"), orch.create_study("b")
            >>> a.study, b.study, orch.study
            ('a', 'b', 'b')
        """
        if not self.backend.has_study(name):
            raise StudyNotFoundError(
                f"There is no study {name!r}; use create_study({name!r}) to make it."
            )
        self._define_ledgers(name)
        self._study = name
        return self._study_handle(name)

    def _study_handle(self, name: str) -> Self:
        handle = copy.copy(self)
        handle._study = name
        handle._handle = True
        return handle

    def __repr__(self) -> str:
        """Show the class and the selected study."""
        return f"<{type(self).__name__} study={self._study!r}>"

    def get_or_create_study(self, name: str) -> Self:
        """Select the study, creating it if it doesn't exist; return its handle."""
        if self.backend.has_study(name):
            return self.select_study(name)
        return self.create_study(name)

    def studies(self) -> list[str]:
        """Return the names of the orchestrator studies in the store, sorted.

        The ledger studies (``<s>::runs`` and so on) aren't listed.
        """
        return [name for name in self.backend.studies() if "::" not in name]

    def register_hooks(self, name: str, **hooks: Any) -> HookSet:
        """Register a named set of hooks, e.g. ``build=my_build``.

        A :class:`~flexorchestrator.hooks.HookSet` slot, or
        ``build(..., build="name")``, can then refer to it by name. See
        :meth:`~flexorchestrator.hooks.HookRegistry.register`.
        """
        return self.registry.register(name, **hooks)

    def delete_study(self, name: str) -> None:
        """Delete a study and everything stored in it, for good.

        Deletes the study and its ledgers (results, datasets, specs and fits)
        with their declarations in Acquirium. If it is the
        selected study, none is selected afterwards, here and on the
        orchestrator a study handle came from; other handles to it then fail.

        Raises:
            StorageError: If deletion is turned off, or a run in the study is
                still running (then nothing is deleted).
            StudyNotFoundError: If the study doesn't exist.
        """
        self._check_delete("delete a study")
        if not self.backend.has_study(name):
            raise StudyNotFoundError(f"There is no study {name!r}.")
        with self._lock(name):
            ledgers = [self._ledger(name, ledger) for ledger in _ledger_names()]
            present = [ledger for ledger in ledgers if self.backend.has_study(ledger)]
            for ledger in present:
                if self.backend.runs(ledger, status="running"):
                    raise StorageError(
                        f"Study {name!r} has a running run in {ledger!r}; "
                        "finish it before deleting the study."
                    )
            for ledger in present:
                self.backend.delete_study(ledger)
            self.backend.delete_study(name)
        cache = self._spec_cache
        for key in [k for k in cache or {} if k[0] == name]:
            del cache[key]
        for orch in {id(self): self, id(self._root): self._root}.values():
            if orch._study == name:
                orch._study = None

    def delete(self, kind: str, tag: str) -> int:
        """Delete a stored item, for good: every run with this kind and tag.

        Args:
            kind: ``"result"``, ``"data"``, ``"model_spec"``,
                ``"surrogate_spec"`` or ``"fit"``.
            tag: The item's tag (a result, spec or fit tag, or a dataset name).

        Returns:
            How many runs were deleted (failed attempts included).

        Raises:
            StorageError: If deletion is turned off, or the item's run is
                still running.
            TagNotFoundError: If nothing has this kind and tag.
            ValueError: If ``kind`` isn't one of the kinds above.
        """
        self._check_delete(f"delete {kind} {tag!r}")
        ledger = self._ledger(self.study, _ledger_of(kind))
        with self._lock(self.study):
            found = self.backend.runs(ledger, metadata=_tag_metadata(tag, kind))
            if not found:
                raise TagNotFoundError(
                    f"Study {self.study!r} has no stored {kind} {tag!r}."
                )
            self.backend.delete_runs(ledger, [run.run_id for run in found])
        cache = self._spec_cache
        if cache is not None:
            cache.pop((self.study, kind, tag), None)
        return len(found)

    # Specs

    def store_model_spec(
        self,
        tag: str,
        spec: Any,
        *,
        parent: str | None = None,
        base_dir: str | os.PathLike[str] | None = None,
        files: Mapping[str, bytes] | None = None,
        assets: Iterable[str] = (),
        spec_format: str | None = None,
        replace: bool = False,
    ) -> SpecRecord:
        """Store a model spec, with the files it references, under ``tag``.

        The spec is validated and normalised by its format's validator when
        that library is installed (a flex-pse spec gets its defaults written
        out). Every file it references (for flex-pse: tariff and events files
        and ``{"$source": path}`` data) is read and stored with it, and its
        path in the stored spec becomes the file's stored name, so the spec
        rebuilds anywhere (:meth:`build`). The hash covers the files too:
        editing a tariff changes ``spec_hash``.

        Args:
            tag: The spec's tag, unique among the study's model specs.
            spec: A dict or pydantic model.
            parent: The tag of the model spec this one was derived from. Files
                the parent stored are reused when the spec names them by their
                stored names, as :meth:`apply_surrogate`'s specs do.
            base_dir: The directory relative file paths resolve against; the
                working directory by default (flex-pse's rule for a dict spec).
            files: The referenced files' bytes, ``{path as the spec writes it:
                bytes}``, instead of reading them (a service with an upload):
                then nothing is read from disk. Stored exactly as the same
                files read from disk, with the same ``spec_hash``. A spec read
                back with :meth:`model_spec` ``(tag, with_files=True)`` stores
                again unchanged with its ``files``. Not with ``base_dir``.
            assets: More file references, as JSON pointers to path strings,
                for a format the orchestrator can't search.
            spec_format: The format; detected from the spec when omitted.
            replace: Replace a stored spec with this tag.

        Returns:
            The stored record.

        Raises:
            TagExistsError: If a model spec with this tag is stored.
            TagNotFoundError: If ``parent`` isn't a stored model spec.
            SpecError: If the spec or its tag is invalid, a referenced file
                can't be read, or ``files`` lacks one or has one the spec
                doesn't reference.
        """
        return self._store_spec(
            MODEL_SPEC, tag, spec, parent, base_dir, assets, spec_format, replace, files
        )

    def store_surrogate_spec(
        self,
        tag: str,
        spec: Any,
        *,
        parent: str | None = None,
        spec_format: str | None = None,
        replace: bool = False,
    ) -> SpecRecord:
        """Store a surrogate spec under ``tag``.

        Args:
            tag: The spec's tag, unique among the study's surrogate specs.
            spec: A dict or pydantic model (for flex-pse, a ``SurrogateSpec``).
            parent: The tag of the surrogate spec this one was derived from.
            spec_format: The format; detected from the spec when omitted.
            replace: Replace a stored spec with this tag.

        Returns:
            The stored record.

        Raises:
            TagExistsError: If a surrogate spec with this tag is stored.
            TagNotFoundError: If ``parent`` isn't a stored surrogate spec.
            SpecError: If the spec or its tag is invalid.
        """
        return self._store_spec(
            SURROGATE_SPEC, tag, spec, parent, None, (), spec_format, replace
        )

    def model_spec(self, tag: str, *, with_files: bool = False) -> SpecRecord:
        """Return a stored model spec, checked against its hash.

        Args:
            tag: The spec's tag.
            with_files: Also return its files' bytes: a
                :class:`~flexorchestrator.specs.SpecRecordWithFiles`, whose
                ``files`` is ``{stored name: bytes}``. For a caller with no
                folder to :meth:`materialize_spec` into.

        Raises:
            TagNotFoundError: If there is none with this tag.
        """
        record, files = self._read_spec(MODEL_SPEC, tag)
        if not with_files:
            return record
        return SpecRecordWithFiles(**dict(record), files=dict(files))

    def surrogate_spec(self, tag: str) -> SpecRecord:
        """Return a stored surrogate spec, checked against its hash.

        Raises:
            TagNotFoundError: If there is none with this tag.
        """
        return self._read_spec(SURROGATE_SPEC, tag)[0]

    def stored_specs(self, kind: SpecKind = MODEL_SPEC) -> list[SpecRecord]:
        """Return the study's stored specs of one kind, oldest first.

        Args:
            kind: ``"model_spec"`` or ``"surrogate_spec"``.

        Raises:
            ValueError: If ``kind`` isn't a spec kind.
        """
        ledger = self._ledger(
            self.study, _ledger_of(kind, (MODEL_SPEC, SURROGATE_SPEC))
        )
        return [
            spec_store.read(self.backend, ledger, run.run_id)[0]
            for run in self.backend.runs(
                ledger, status="succeeded", metadata={"flexorch_kind": kind}
            )
        ]

    def materialize_spec(self, tag: str, directory: str | os.PathLike[str]) -> dict:
        """Write a stored model spec's files to ``directory``; return the spec.

        The returned spec points at the written files by absolute path, so the
        build hook can read them from any working directory (or Ray worker).
        """
        record, files = self._read_spec(MODEL_SPEC, tag)
        return materialize_spec(record.spec, record.assets, files, directory)

    def build(self, tag: str, *, build: Any = None, **overrides: Any) -> Any:
        """Build the model a stored spec describes.

        The spec's files are written to a temporary directory for the build,
        so it doesn't matter where they were when the spec was stored. The
        model remembers the spec tag, hash and overrides, so storing its
        results links them to the spec. Set variables from stored data
        afterwards, with :meth:`apply_model_inputs`.

        Args:
            tag: The model spec's tag.
            build: The build hook; the orchestrator's by default.
            **overrides: Passed to the build hook (for flex-pse, ``fix``
                elements: ``{"plant.ro.recovery": 0.6}``).

        Raises:
            TagNotFoundError: If the spec isn't stored.
            SpecError: If ``overrides`` aren't JSON.
        """
        hook = self.registry.resolve_ref(build or self.hooks.build, "build")
        record, files = self._read_spec(MODEL_SPEC, tag)
        link = {
            "tag": tag,
            "spec_hash": record.spec_hash,
            "overrides": to_json_spec(overrides),
        }
        with tempfile.TemporaryDirectory(prefix="flexorch-spec-") as directory:
            spec = materialize_spec(record.spec, record.assets, files, directory)
            model = hook(spec, **overrides)
        try:
            model._flexorch_spec = link
        except (AttributeError, TypeError):  # pragma: no cover - a frozen model
            pass
        return model

    def apply_model_inputs(self, model: Any, model_inputs: Any) -> Any:
        """Set variables of a built model from stored data.

        Each entry's data is lined up with the model's time steps (the
        ``time_grid`` hook) and its variables fixed to it (the ``apply_values``
        hook). A model from :meth:`build` remembers what was applied, so
        storing its results records the values (in ``overrides``) and where
        they came from (``model_inputs``). Applying a path again replaces it.

        Args:
            model: The built model, changed in place.
            model_inputs: One entry or a list, each a
                :class:`~flexorchestrator.inputs.ModelInput` or a mapping in
                its shape (a fit selector, its columns renamed to the paths
                they set, plus how to line it up).

        Returns:
            A :class:`~flexorchestrator.inputs.resolve.Resolved`: the
            ``values`` fixed and their ``provenance``.

        Raises:
            ModelInputError: If an entry can't be resolved, or sets a path the
                build's overrides set.

        Example:
            >>> model = orch.build("base")  # doctest: +SKIP
            >>> orch.apply_model_inputs(model, {  # doctest: +SKIP
            ...     "streams": [{"source_id": "historian", "ref_name": "FT_101"}],
            ...     "rename": {"*": "plant.ro.permeate"},
            ... })
        """
        from flexorchestrator.inputs.models import ModelInput

        if isinstance(model_inputs, ModelInput | Mapping):
            model_inputs = [model_inputs]
        built = getattr(model, "_flexorch_spec", None)
        link = dict(built) if isinstance(built, Mapping) else None
        applied = list((link or {}).get("model_inputs") or [])
        earlier = {path for record in applied for path in record["paths"]}
        overrides = (link or {}).get("overrides", {})
        found = self._resolve_inputs(
            model,
            model_inputs,
            (),
            self.hooks,
            {k: v for k, v in overrides.items() if k not in earlier},
        )
        hook = self.registry.resolve_ref(self.hooks.apply_values, "apply_values")
        hook(model, found.values)
        if link is not None:
            kept = [
                {**r, "paths": [p for p in r["paths"] if p not in found.values]}
                for r in applied
            ]
            link["overrides"] = {**overrides, **to_json_spec(found.values)}
            link["model_inputs"] = [r for r in kept if r["paths"]] + found.provenance
            model._flexorch_spec = link
        return found

    def resolve_model_inputs(self, tag: str, model_inputs: Sequence[Any]) -> Any:
        """Resolve model inputs against a stored spec, without building.

        Args:
            tag: The model spec whose time steps the data is lined up with.
            model_inputs: The entries: :class:`~flexorchestrator.inputs.ModelInput`
                or mappings.

        Returns:
            A :class:`~flexorchestrator.inputs.resolve.Resolved`: ``values``
            (the build overrides) and ``provenance``.

        Raises:
            TagNotFoundError: If the spec isn't stored.
            ModelInputError: If an entry can't be resolved.
        """
        record, _ = self._read_spec(MODEL_SPEC, tag)
        return self._resolve_inputs(record.spec, model_inputs, (), self.hooks, {})

    def _resolve_inputs(
        self,
        spec_or_model: Any,
        model_inputs: Sequence[Any],
        swept: Sequence[Any],
        hooks: HookSet,
        overrides: Mapping[str, Any],
    ) -> Any:
        """Resolve entries against a spec's or model's time steps (``time_grid``)."""
        from flexorchestrator.inputs.models import ModelInput
        from flexorchestrator.inputs.resolve import resolve

        def entries(items: Sequence[Any]) -> list[ModelInput]:
            return [
                i if isinstance(i, ModelInput) else ModelInput.model_validate(i)
                for i in items
            ]

        ref = hooks.time_grid or self.hooks.time_grid
        grid = None
        if ref is not None:
            try:
                hook = self.registry.resolve_ref(ref, "time_grid")
            except HookError:
                if ref != DEFAULT_HOOKS.time_grid:
                    raise
                hook = None  # flex-pse isn't installed: no time steps to read
            grid = hook(spec_or_model) if hook is not None else None
        found = resolve(
            self,
            entries(model_inputs),
            grid,
            entries(swept),
            cache=getattr(self._call, "inputs", None),
        )
        # A path under no time block is set in every scope, as <scope>.<path>,
        # so an override of <path> collides with each of those.
        both = sorted(
            {
                found.requested.get(path, path)
                for path in found.values
                if path in overrides or found.requested.get(path) in overrides
            }
        )
        if both:
            raise ModelInputError(
                f"{both} are set both by model_inputs and by a variable (a sweep, "
                "a case or an override); set each path one way."
            )
        return found

    def apply_surrogate(
        self,
        base: str,
        *,
        unit: str,
        surrogate: str,
        relation: str | None = None,
    ) -> dict:
        """Return a stored model spec with a stored surrogate applied to a unit.

        Calls the ``apply_surrogate`` hook; the result isn't stored. Store
        it with ``store_model_spec(tag, spec, parent=base)``, which reuses the
        base spec's files.

        Args:
            base: The model spec's tag.
            unit: The unit to apply it to, e.g. ``"plant.digester"``.
            surrogate: The surrogate spec's tag.
            relation: The relation it replaces; the hook's default if omitted.
        """
        hook = self.registry.resolve_ref(self.hooks.apply_surrogate, "apply_surrogate")
        spec = copy.deepcopy(self.model_spec(base).spec)
        return hook(spec, unit, self.surrogate_spec(surrogate).spec, relation)

    def _store_spec(
        self,
        kind: SpecKind,
        tag: str,
        spec: Any,
        parent: str | None,
        base_dir: Any,
        pointers: Iterable[str],
        spec_format: str | None,
        replace: bool,
        given: Mapping[str, bytes] | None = None,
    ) -> SpecRecord:
        # Refuse a taken tag before reading any files; _guarded checks it
        # again, under the lock, when it writes.
        self._check_new(tag, kind, replace)
        known: dict[str, tuple[Any, bytes]] = {}
        if parent is not None:
            parent_record, parent_files = self._read_spec(kind, parent)
            known = {
                ref.name: (ref, parent_files[ref.name])
                for ref in parent_record.assets.values()
            }
        data, spec_format = normalise_spec(kind, spec, spec_format)
        references = find_assets(data, spec_format, pointers)
        data, refs, files = collect_assets(data, references, base_dir, known, given)
        record = SpecRecord.create(
            kind,
            tag,
            data,
            parent_tag=parent,
            spec_format=spec_format,
            validate=False,
            assets=refs,
        )
        self._write_spec(record, files, replace)
        cache = self._spec_cache
        if cache is not None:
            cache[(self.study, kind, tag)] = (record, files)
        return record

    def _write_spec(
        self, record: SpecRecord, files: Mapping[str, bytes], replace: bool = False
    ) -> str:
        """Store a spec record and its files, refusing duplicates; return the run."""
        ledger = self._ledger(self.study, LEDGERS[record.kind])

        def write() -> _Written:
            metadata = {**self._metadata(record.tag, record.kind), **record.metadata()}
            return _Written(
                spec_store.write(self.backend, ledger, record, files, metadata)
            )

        return self._guarded(record.kind, record.tag, replace, write).run_id

    @property
    def _spec_cache(self) -> dict | None:
        """The specs read in this thread's :meth:`_caching_specs` block, if any."""
        return getattr(self._call, "specs", None)

    def _read_spec(self, kind: str, tag: str) -> tuple[SpecRecord, dict[str, bytes]]:
        cache = self._spec_cache
        key = (self.study, kind, tag)
        if cache is not None and key in cache:
            return cache[key]
        run = self._latest(tag, kind)
        ledger = self._ledger(self.study, LEDGERS[kind])
        found = spec_store.read(self.backend, ledger, run.run_id)
        if cache is not None:
            cache[key] = found
        return found

    @contextmanager
    def _caching_specs(self) -> Iterator[None]:
        """Read each spec once in this block (a study run reads the same ones).

        A spec stored or replaced in the block is cached as written, and a
        deleted one is dropped, so the cache stays what the store holds as long
        as this orchestrator is the only writer, which a study run assumes.
        """
        previous = self._spec_cache
        inputs = getattr(self._call, "inputs", None)
        if previous is None:
            # Model inputs' data too: read once per distinct selection.
            self._call.specs, self._call.inputs = {}, {}
        try:
            yield
        finally:
            self._call.specs, self._call.inputs = previous, inputs

    def _spec_link(
        self,
        model: Any,
        spec: str | None,
        overrides: Mapping[str, Any] | None,
        model_inputs: Sequence[Mapping[str, Any]] | None = None,
    ) -> "_SpecLink | None":
        """The spec a result was built from: given, or remembered by build()."""
        if spec is not None:
            record = self.model_spec(spec)
            return _SpecLink(
                spec,
                record.spec_hash,
                to_json_spec(overrides or {}),
                [dict(m) for m in model_inputs] if model_inputs else None,
            )
        built = getattr(model, "_flexorch_spec", None)
        if isinstance(built, Mapping):
            return _SpecLink(
                built["tag"],
                built["spec_hash"],
                built["overrides"],
                built.get("model_inputs"),
            )
        return None

    # Datasets

    def store_data(
        self,
        name: str,
        df: Any,
        units: dict[str, str | None] | None = None,
        *,
        time_column: str | None = None,
        replace: bool = False,
    ) -> StoredData:
        """Store a table of measured data as the dataset ``name``.

        Each column becomes a timeseries with its unit, so it can be queried,
        converted and used to fit surrogates. Timestamps are stored in
        UTC; naive ones are taken as UTC.

        Args:
            name: The dataset's name, unique among the study's datasets.
            df: A polars or pandas DataFrame of numeric columns.
            units: ``{column: unit}``, as QUDT keys (``"M3-PER-DAY"``) or as
                written (``"m3/day"``, ``"gal/min"``). Columns left out have no
                unit.
            time_column: The datetime column. For pandas the default is the
                index; for polars, the only datetime column.
            replace: Replace a stored dataset of this name: the new one is
                written first, then the old one is deleted.

        Returns:
            What was stored.

        Raises:
            TagExistsError: If the dataset exists and ``replace`` is false.
            SpecError: If ``name`` isn't a valid tag.
            StorageError: If the table can't be stored (no datetimes, repeated
                timestamps, a non-numeric column, an empty table).

        Example:
            >>> units = {"biogas_volume": "m3/day"}
            >>> orch.store_data("biogas_data", df, units=units)  # doctest: +SKIP
        """

        def write() -> StoredData:
            timestamps, columns = frames.to_columns(df, time_column)
            return datasets.store(
                self.backend,
                self._ledger(self.study, LEDGERS[DATA_KIND]),
                name,
                timestamps,
                columns,
                dict(units or {}),
                self.unit_mapper,
                self._metadata(name, DATA_KIND),
            )

        return self._guarded(DATA_KIND, name, replace, write)

    def get_data(
        self,
        name: str | None = None,
        columns: list[str] | None = None,
        start: Any = None,
        end: Any = None,
        *,
        column: str | None = None,
        frame: frames.FrameKind | None = None,
    ) -> Any:
        """Read a dataset, or one column found across the study's datasets.

        Args:
            name: The dataset.
            columns: Only these columns, in this order.
            start: Only rows at or after this datetime (naive means UTC).
            end: Only rows at or before this datetime.
            column: Instead of ``name``: a column name, looked up across the
                study's datasets.
            frame: ``"polars"`` or ``"pandas"``; the orchestrator's default if
                omitted.

        Returns:
            A polars DataFrame with a ``time`` column first, or a pandas
            DataFrame with a UTC ``DatetimeIndex``.

        Raises:
            TagNotFoundError: If the dataset, or a dataset with ``column``,
                doesn't exist.
            AmbiguousColumnError: If ``column`` is in several datasets.
        """
        if (name is None) == (column is None):
            raise TypeError("Give the dataset name, or column= to look one up.")
        if column is not None:
            name, columns = self._dataset_with(column), [column]
        return self._read_data(self.study, name, columns, start, end, frame)

    def _read_data(
        self,
        study: str,
        name: str,
        columns: list[str] | None,
        start: Any,
        end: Any,
        frame: frames.FrameKind | None,
    ) -> Any:
        """Read a dataset of any study of this store (see :meth:`get_data`)."""
        ledger = self._ledger(study, LEDGERS[DATA_KIND])
        run = self._latest(name, DATA_KIND, study)
        timestamps, values = datasets.read(
            self.backend, ledger, run.run_id, name, columns
        )
        lo = None if start is None else to_utc(start)
        hi = None if end is None else to_utc(end)
        keep = [
            i
            for i, when in enumerate(timestamps)
            if (lo is None or when >= lo) and (hi is None or when <= hi)
        ]
        return frames.from_columns(
            [timestamps[i] for i in keep],
            {c: [v[i] for i in keep] for c, v in values.items()},
            frame or self.frame,
        )

    # Model results

    def store_model_results(
        self,
        tag: str,
        model: Any,
        outcome: SolveOutcome | None = None,
        keys: Iterable[str] | None = None,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
        *,
        streams: Iterable[str] | None = None,
        replace: bool = False,
        time_sets: Sequence[Any] | None = None,
        datetime_index: Sequence[Any] | None = None,
        spec: str | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> StoredResult:
        """Export a model's values and store them under ``tag``.

        Values are exported with
        :func:`~flexorchestrator.export.export_model_data` and stored as one
        snapshot (a ``table`` of timeseries plus a JSON manifest).
        Keys named in ``streams`` are also stored as native Acquirium variables,
        for queries and fitting. Everything is checked before the run starts,
        so a value that can't be stored leaves no run behind. If ``outcome``
        isn't ``ok``, only the outcome is stored, in a ``failed`` run, so the
        tag can be used again.

        Args:
            tag: The result's tag, unique among the study's results.
            model: The model, passed explicitly (there is no current model).
            outcome: How the solve ended; stored as ``solve_outcome``.
            keys: Exact paths to export (see ``export_model_data``).
            include: Glob patterns to export.
            exclude: Glob patterns to leave out.
            streams: Keys, component paths or globs of exported values to also
                store natively (``["costing.aggregate_electrical_power"]``).
            replace: Replace a stored result with this tag: the new one is
                written first, then the old one is deleted. A failed
                solve leaves the old result in place.
            time_sets: The model's time Sets. By default the ``time_sets`` hook
                gives them (for flex-pse models, including the partial-horizon
                Sets of startup/shutdown).
            datetime_index: One datetime per time point, for models whose time
                block doesn't have them.
            spec: The tag of the model spec the model was built from. A model
                from :meth:`build` remembers its spec, so this is only needed
                for models built another way.
            overrides: The build overrides used with ``spec``.

        Returns:
            What was stored.

        Raises:
            TagExistsError: If a result with this tag is running or succeeded
                (and ``replace`` is false).
            TagNotFoundError: If ``spec`` isn't a stored model spec.
            SpecError: If ``tag`` isn't a valid tag.
            ExportError, StorageError, UnitError: If the values can't be
                exported or stored.
        """
        _check_outcome(outcome)
        link = self._spec_link(model, spec, overrides)
        if keys is None and include is None and exclude is None:
            selection = self.default_export_selection()
            keys = selection.get("keys")
            include = selection.get("include")
            exclude = selection.get("exclude")

        def write() -> StoredResult:
            data = None
            if outcome is None or outcome.ok:
                data = export_model_data(
                    model,
                    keys=keys,
                    include=include,
                    exclude=exclude,
                    time_sets=(
                        time_sets if time_sets is not None else self._time_sets(model)
                    ),
                    datetime_index=datetime_index,
                    unit_mapper=self.unit_mapper,
                )
            return self._store(tag, data, outcome, streams, link)

        return self._guarded(RESULT_KIND, tag, replace, write)

    def store_model_data(
        self,
        tag: str,
        data: ModelData,
        outcome: SolveOutcome | None = None,
        *,
        streams: Iterable[str] | None = None,
        replace: bool = False,
        spec: str | None = None,
        overrides: Mapping[str, Any] | None = None,
        model_inputs: Sequence[Mapping[str, Any]] | None = None,
    ) -> StoredResult:
        """Store values that are already exported under ``tag``.

        :meth:`store_model_results` is this after
        :func:`~flexorchestrator.export.export_model_data`. Use it directly for
        values exported another way, e.g. from a model that isn't Pyomo.

        Args:
            tag: The result's tag, unique among the study's results.
            data: The values.
            outcome: How the solve ended. If it isn't ``ok``, ``data`` is
                ignored and only the outcome is stored, in a ``failed`` run.
            streams: Values to also store natively; see
                :meth:`store_model_results`.
            replace: Replace a stored result; see :meth:`store_model_results`.
            spec: The model spec the values came from; see
                :meth:`store_model_results`.
            overrides: The build overrides used with ``spec``.
            model_inputs: Where the model inputs among ``overrides`` came from,
                recorded with ``spec``.

        Returns:
            What was stored.

        Raises:
            TagExistsError: If a result with this tag is running or succeeded
                (and ``replace`` is false).
            SpecError: If ``tag`` isn't a valid tag.
            ExportError, StorageError, UnitError: If the values can't be
                stored.
        """
        _check_outcome(outcome)
        link = self._spec_link(None, spec, overrides, model_inputs)
        return self._guarded(
            RESULT_KIND,
            tag,
            replace,
            lambda: self._store(tag, data, outcome, streams, link),
        )

    def solve_and_store(
        self,
        tag: str,
        model: Any,
        keys: Iterable[str] | None = None,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
        *,
        streams: Iterable[str] | None = None,
        replace: bool = False,
        solve: Any = None,
        time_sets: Sequence[Any] | None = None,
        datetime_index: Sequence[Any] | None = None,
        spec: str | None = None,
        overrides: Mapping[str, Any] | None = None,
        **options: Any,
    ) -> StoredResult:
        """Solve a model and store its results under ``tag``.

        The tag is checked before solving, so a solve is never wasted on a tag
        that can't be stored. A solve that raises or isn't optimal is stored as
        a ``failed`` result holding only its outcome.

        Args:
            tag: The result's tag.
            model: The built model.
            keys: Exact paths to export.
            include: Glob patterns to export.
            exclude: Glob patterns to leave out.
            streams: Values to also store natively; see
                :meth:`store_model_results`.
            replace: Replace a stored result; see :meth:`store_model_results`.
            solve: The solve hook (a callable, import string or registered
                name); the orchestrator's ``solve`` hook by default.
            time_sets: See :meth:`store_model_results`.
            datetime_index: See :meth:`store_model_results`.
            spec: See :meth:`store_model_results`.
            overrides: See :meth:`store_model_results`.
            **options: Passed to the solve hook.

        Returns:
            What was stored, with the outcome.

        Raises:
            TagExistsError: If a result with this tag is running or succeeded
                (and ``replace`` is false).
            SpecError: If ``tag`` isn't a valid tag.
        """
        self._check_new(tag, RESULT_KIND, replace)
        hook = self.registry.resolve_ref(solve or self.hooks.solve, "solve")
        outcome = run_solve(hook, model, **options)
        with self._using_hooks(HookSet(solve=solve)):  # recorded with the run
            return self.store_model_results(
                tag,
                model,
                outcome,
                keys,
                include,
                exclude,
                streams=streams,
                replace=replace,
                time_sets=time_sets,
                datetime_index=datetime_index,
                spec=spec,
                overrides=overrides,
            )

    def model_results(
        self, tag: str, units: Mapping[str, str] | None = None
    ) -> ModelData:
        """Read a stored result's model values back from its snapshot.

        Values come back as they were exported, with UTC timestamps, unless
        ``units`` asks for others.

        Args:
            tag: The result's tag.
            units: ``{key or component path: Pyomo unit}`` to convert to, with
                pint (``{"costing.aggregate_electrical_power": "W"}``).

        Raises:
            TagNotFoundError: If no succeeded result has this tag.
            ExportError, UnitError: If ``units`` names a key that wasn't stored,
                or a unit that doesn't convert.
        """
        return self.result(tag).data(units)

    def design_results(self, tag: str) -> Any:
        """Return the sizes and annualized costs of a stored design study.

        Args:
            tag: The result's tag.

        Returns:
            A :class:`~flexorchestrator.study.design.DesignSummary`.

        Raises:
            TagNotFoundError: If no succeeded result has this tag.
            SpecError: If the result wasn't built from a design study.
            ExportError: If its export left out a value the summary needs.
        """
        from flexorchestrator.study.design import read_summary

        handle = self.result(tag)
        spec = handle.spec().spec if handle.spec_tag else {}
        if not is_design_spec(spec):
            raise SpecError(f"Result {tag!r} wasn't built from a design study.")
        return read_summary(handle.data(), spec)

    def result(self, tag: str) -> "ResultHandle":
        """Return a handle on a stored result; nothing is read until asked for.

        Raises:
            TagNotFoundError: If no succeeded result has this tag.

        Example:
            >>> orch.result("base").to_pandas()  # doctest: +SKIP
        """
        return ResultHandle._from_run(self, self._latest(tag, RESULT_KIND))

    def stored_model_results(
        self, tags: Iterable[str] | None = None
    ) -> list["ResultHandle"]:
        """Return handles on the study's stored results, oldest first.

        Args:
            tags: Only these tags.

        Example:
            >>> for r in orch.stored_model_results():  # doctest: +SKIP
            ...     df = r.to_frame("polars")
        """
        wanted = None if tags is None else set(tags)
        return [
            ResultHandle._from_run(self, run)
            for run in self.backend.runs(
                self._runs, status="succeeded", metadata={"flexorch_kind": RESULT_KIND}
            )
            if wanted is None or run.metadata.get("flexorch_tag") in wanted
        ]

    def _read_snapshot(self, run_id: str) -> tuple[ModelData, Any]:
        found = {
            r.label: r.value
            for r in self.backend.records(
                self._runs, run_id, labels=[MODEL_MANIFEST, MODEL_VALUES, SOLVE_OUTCOME]
            )
        }
        data = results.read_snapshot(found.get(MODEL_VALUES), found[MODEL_MANIFEST])
        return data, found.get(SOLVE_OUTCOME)

    # Commands, studies from config and fits

    def run_command(
        self,
        name: str,
        params: Any = None,
        /,
        *,
        progress: Progress | None = None,
        runtime: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> Any:
        """Run a command by name, recording the call in ``flexorch::commands``.

        Args:
            name: The command, e.g. ``"execute_study"`` or a plugin's.
            params: Its parameters, as a mapping or its params model.
            progress: Receives progress reports and can cancel the call.
            runtime: Values JSON can't carry (e.g. a ``build`` callable); not
                recorded, apart from the import strings of callables.
            **fields: More parameters, merged over ``params``.

        Returns:
            What the command returned.

        Raises:
            CommandError: If there is no such command or the parameters don't
                validate.

        Example:
            >>> orch.run_command("fit_surrogate", tag="ro", ...)  # doctest: +SKIP
        """
        cmd = self.commands.get(name)
        validated = cmd.validate(params, **fields)
        progress = progress or Progress()
        runtime = {k: v for k, v in (runtime or {}).items() if v is not None}
        metadata = {
            **self._metadata(name, COMMAND_KIND),
            "flexorch_study": self._study,
            "flexorch_runtime": {
                key: _hook_name(value) if callable(value) else repr(value)
                for key, value in runtime.items()
            }
            or None,
        }
        metadata = {k: v for k, v in metadata.items() if v is not None}
        status, error = "failed", None
        try:
            result = record_call(
                self.backend,
                metadata,
                validated,
                lambda: cmd.call(self, validated, progress, **runtime),
            )
            status = "succeeded"
            return result
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.events.emit("command_finished", name=name, status=status, error=error)

    def register_command(self, cmd: Command) -> Command:
        """Add a command to this orchestrator (a plugin's are added for you).

        Raises:
            CommandError: If its name is taken.
        """
        return self.commands.register(cmd)

    def execute_study(
        self,
        config: Any,
        *,
        build: Any = None,
        solve: Any = None,
        max_workers: int | None = None,
        ray_address: str | None = None,
        dry_run: bool = False,
        progress: Progress | None = None,
    ) -> Any:
        """Run a study from a config: fits, then sweeps of build-solve-store tasks.

        The ``execute_study`` command; see :mod:`flexorchestrator.study`.

        Args:
            config: A :class:`~flexorchestrator.study.StudyConfig`, its dict,
                or the path of a YAML or JSON file.
            build: A build hook (callable, import string or registered name)
                that wins over the config's.
            solve: A solve hook that wins over the config's.
            max_workers: Overrides ``execution.max_workers``; more than 1
                runs the tasks (and the fits' variants) on Ray.
            ray_address: Overrides ``execution.ray_address``.
            dry_run: Expand the config and return the planned fits and tasks
                without storing or solving anything.
            progress: Receives a report after each fit and task.

        Returns:
            A :class:`~flexorchestrator.study.StudyRun`.

        Raises:
            StudyConfigError: If the config is invalid.
            TagExistsError: With ``on_existing: error``, if a task's tag is
                already stored (checked before anything runs).
        """
        from flexorchestrator.study.config import load_config

        params = {"config": load_config(config), "dry_run": dry_run}
        if max_workers is not None:
            params["max_workers"] = max_workers
        if ray_address is not None:
            params["ray_address"] = ray_address
        return self.run_command(
            "execute_study",
            params,
            progress=progress,
            runtime={"build": build, "solve": solve},
        )

    def fit_surrogate(self, *, progress: Progress | None = None, **params: Any) -> Any:
        """Fit surrogates from stored data and store them (``fit_surrogate``).

        See :class:`~flexorchestrator.fitting.FitParams` for the parameters:
        ``tag``, ``regressor``, ``X``, ``y`` and optionally ``variants``,
        ``window``, ``prep``, ``validation``, ``params``, ``spec_options`` and
        ``on_existing``.

        Returns:
            A :class:`~flexorchestrator.fitting.FitSummary`.

        Example:
            >>> orch.fit_surrogate(  # doctest: +SKIP
            ...     tag="ro_linear", regressor="multilinear",
            ...     X={"dataset": "ro", "columns": ["permeate"]},
            ...     y={"dataset": "ro", "columns": ["power_electrical"]},
            ... )
        """
        return self.run_command("fit_surrogate", params, progress=progress)

    # Study export and import

    def export_study(
        self,
        path: str | os.PathLike[str],
        *,
        parts: Iterable[str] = ("specs", "data", "fits", "results"),
        tags: Iterable[str] | None = None,
        compress: bool = False,
        overwrite: bool = False,
    ) -> Any:
        """Export the selected study to a folder of JSON files.

        See :func:`flexorchestrator.archive.export_study`.
        """
        from flexorchestrator.archive import export_study

        return export_study(
            self, path, parts=parts, tags=tags, compress=compress, overwrite=overwrite
        )

    def export_result(self, tag: str, path: str | os.PathLike[str]) -> Any:
        """Export one stored result to a JSON file.

        See :func:`flexorchestrator.archive.export_result`.
        """
        from flexorchestrator.archive import export_result

        return export_result(self, tag, path)

    def import_study(
        self,
        path: str | os.PathLike[str],
        *,
        name: str | None = None,
        on_existing: Literal["error", "skip"] = "error",
    ) -> Any:
        """Re-create an exported study in this store, and select it.

        See :func:`flexorchestrator.archive.import_study`.
        """
        from flexorchestrator.archive import import_study

        return import_study(self, path, name=name, on_existing=on_existing)

    # Extension points: stable to override in a subclass

    def build_hooks(self) -> HookSet:
        """Return the default hooks; the hooks given to ``__init__`` win over them.

        Override to change the defaults for every orchestrator of a subclass.
        """
        return DEFAULT_HOOKS

    def default_export_selection(self) -> dict[str, Any]:
        """Return the export selection used when a store names none.

        ``{"keys": [...], "include": [...], "exclude": [...]}``, any of them;
        ``{}`` (the default) exports everything.
        """
        return {}

    def before_store(self, item: StoreItem) -> None:
        """Called before an item is stored; raise to refuse it.

        Add to ``item.metadata`` to store more run metadata.
        """

    def after_store(self, item: StoreItem) -> None:
        """Called after an item is stored, with ``item.run_id`` set."""

    def on_task_result(self, result: Any) -> None:
        """Called with each task's ``TaskResult`` as a study runs."""

    def on_study_created(self, study: str) -> None:
        """Called after :meth:`create_study` creates a study."""

    def on(self, event: str, fn: Callable[..., Any] | None = None) -> Any:
        """Register a listener for a lifecycle event; a decorator without ``fn``.

        See :mod:`flexorchestrator.ext.events` for the events.

        Example:
            >>> @orch.on("result_stored")  # doctest: +SKIP
            ... def log(item):
            ...     print(item.tag)
        """
        return self.events.on(event, fn)

    # Internals

    def _load_plugin(self, plugin: Plugin) -> None:
        for cmd in plugin.commands:
            self.commands.register(cmd, source=f"plugin {plugin.name!r}")
        for name, refs in hook_sets(plugin).items():
            self.registry.register(name, **refs)
        for event, listener in listeners(plugin):
            self.events.on(event, listener)
        cls = plugin.orchestrator_cls
        if cls is not None and not isinstance(self, cls):
            warnings.warn(
                f"Plugin {plugin.name!r} provides {cls.__name__}, but this is a "
                f"{type(self).__name__}; build it with "
                "flexorchestrator.ext.create_orchestrator() to use the subclass.",
                stacklevel=3,
            )

    def _store(
        self,
        tag: str,
        data: ModelData | None,
        outcome: SolveOutcome | None,
        streams: Iterable[str] | None,
        link: "_SpecLink | None" = None,
    ) -> StoredResult:
        table, manifest, writes = None, None, []
        if data is not None and (outcome is None or outcome.ok):
            table, manifest = results.snapshot(data)
            if streams:
                native = results.select(data, streams)
                writes = results.plan_writes(self.backend, self._runs, native)
            results.declare(self.backend, self._runs, writes)
            self.backend.declare(self._runs, MODEL_MANIFEST, role="output", kind="json")
            if table is not None:
                self.backend.declare(
                    self._runs, MODEL_VALUES, role="output", kind="table"
                )
        if outcome is not None:
            self.backend.declare(self._runs, SOLVE_OUTCOME, role="output", kind="json")
        metadata = self._metadata(tag)
        if link is not None:
            for label in (MODEL_SPEC_REF, OVERRIDES):
                self.backend.declare(self._runs, label, role="input", kind="json")
            if link.model_inputs:
                self.backend.declare(
                    self._runs, MODEL_INPUTS, role="input", kind="json"
                )
            metadata.update(link.metadata())
        run_id = self.backend.start_run(self._runs, metadata)
        try:
            if link is not None:
                self.backend.record(run_id, MODEL_SPEC_REF, link.ref())
                self.backend.record(run_id, OVERRIDES, link.overrides)
                if link.model_inputs:
                    self.backend.record(run_id, MODEL_INPUTS, link.model_inputs)
            if manifest is not None:
                self.backend.record(run_id, MODEL_MANIFEST, manifest)
            if table is not None:
                self.backend.record(run_id, MODEL_VALUES, table)
            results.record(self.backend, run_id, writes)
            if outcome is not None:
                self.backend.record(
                    run_id, SOLVE_OUTCOME, outcome.model_dump(mode="json")
                )
        except BaseException as exc:
            self.backend.fail_run(run_id, exc)
            raise
        if outcome is not None and not outcome.ok:
            self.backend.fail_run(
                run_id,
                SolveNotOptimal(
                    f"solve {outcome.status} ({outcome.termination}): {outcome.message}"
                ),
            )
            return StoredResult(tag, run_id, "failed", outcome)
        self.backend.finish_run(run_id)
        return StoredResult(
            tag,
            run_id,
            "succeeded",
            outcome,
            data.keys() if data is not None else [],
            [w.series.key for w in writes],
        )

    @property
    def _runs(self) -> str:
        return self._ledger(self.study, LEDGERS[RESULT_KIND])

    @staticmethod
    def _ledger(study: str, name: str) -> str:
        return f"{study}::{name}"

    def _define_ledgers(self, study: str) -> None:
        for ledger in _ledger_names():
            self.backend.define_study(self._ledger(study, ledger))

    def _check_new(self, tag: str, kind: str, replace: bool = False) -> None:
        check_tag(tag)
        if replace:
            self._check_delete(f"replace {kind} {tag!r}")
        ledger = self._ledger(self.study, LEDGERS[kind])
        for run in self.backend.runs(ledger, metadata=_tag_metadata(tag, kind)):
            if run.status == "running" or (run.status == "succeeded" and not replace):
                raise TagExistsError(
                    f"Study {self.study!r} already has a {run.status} {kind} "
                    f"{tag!r} (run {run.run_id})."
                )

    def _guarded(
        self, kind: str, tag: str, replace: bool, write: Callable[[], Any]
    ) -> Any:
        """Check the tag, write, then settle the tag, holding the study's lock.

        ``before_store`` sees the item first and may reject it or add metadata;
        ``after_store`` and the item's lifecycle event follow a stored item.
        """
        item = StoreItem(kind, tag, dict(getattr(self._call, "pending", None) or {}))
        with self._lock(self.study):
            self._check_new(tag, kind, replace)
            self.before_store(item)
            self._call.extra = item.metadata
            try:
                stored = write()
            finally:
                self._call.extra = None
            ok = getattr(stored, "ok", True)
            if ok:
                self._settle(kind, tag, stored.run_id, replace)
        if ok:
            item.run_id = stored.run_id
            self.after_store(item)
            self.events.emit(STORED_EVENTS[kind], item=item)
        return stored

    def _settle(self, kind: str, tag: str, run_id: str, replace: bool) -> None:
        """Leave one live run with this tag, after a write.

        The check and the write aren't atomic in Acquirium, so another writer
        may have stored the same tag in between. Without ``replace`` the
        earliest run wins: if another live run started before this one, this
        one is deleted and the call fails. With ``replace`` the latest wins: the
        runs before this one are deleted, and if one started after it, this
        one is deleted and the call fails.
        """
        ledger = self._ledger(self.study, LEDGERS[kind])
        live = [
            run
            for run in self.backend.runs(ledger, metadata=_tag_metadata(tag, kind))
            if run.status in ("running", "succeeded")
        ]
        ids = [run.run_id for run in live]
        here = ids.index(run_id)
        before, after = live[:here], live[here + 1 :]
        lost = after if replace else before
        if lost:
            self.backend.delete_runs(ledger, [run_id])
            raise TagExistsError(
                f"Another write stored {kind} {tag!r} at the same time (run "
                f"{lost[0].run_id}); this one was removed."
            )
        if replace:
            self.backend.delete_runs(
                ledger, [run.run_id for run in before if run.status == "succeeded"]
            )

    @contextmanager
    def _with_metadata(
        self, extra: Mapping[str, Any], *, wins: bool = False
    ) -> Iterator[None]:
        """Add ``extra`` to the metadata of every item stored in this block.

        With ``wins``, ``extra`` replaces the orchestrator's own keys too (an
        import keeps a result's original ``flexorch_hooks``).
        """
        slot = "forced" if wins else "pending"
        previous = getattr(self._call, slot, None)
        setattr(self._call, slot, {**(previous or {}), **extra})
        try:
            yield
        finally:
            setattr(self._call, slot, previous)

    @contextmanager
    def _using_hooks(self, hooks: HookSet) -> Iterator[None]:
        """Record ``hooks`` (over the orchestrator's) with results stored here."""
        previous = getattr(self._call, "hooks", None)
        self._call.hooks = (previous or HookSet()).merged(hooks)
        try:
            yield
        finally:
            self._call.hooks = previous

    def _check_delete(self, what: str) -> None:
        if not self.allow_delete:
            raise StorageError(
                f"Can't {what}: this orchestrator was created with allow_delete=False."
            )

    @contextmanager
    def _lock(self, study: str) -> Iterator[None]:
        with self._locks_lock:
            lock = self._locks.setdefault(study, threading.RLock())
        with lock:
            yield

    def _latest(self, tag: str, kind: str, study: str | None = None) -> Any:
        study = study or self.study
        ledger = self._ledger(study, LEDGERS[kind])
        found = self.backend.runs(
            ledger, status="succeeded", metadata=_tag_metadata(tag, kind)
        )
        if not found:
            raise TagNotFoundError(f"Study {study!r} has no stored {kind} {tag!r}.")
        return found[-1]

    def _dataset_with(self, column: str) -> str:
        ledger = self._ledger(self.study, LEDGERS[DATA_KIND])
        stored = {
            run.metadata["flexorch_tag"]
            for run in self.backend.runs(
                ledger, status="succeeded", metadata={"flexorch_kind": DATA_KIND}
            )
        }
        found = sorted(
            {
                v.metadata.get("dataset")
                for v in self.backend.variables(ledger)
                if v.metadata.get("column") == column
            }
            & stored
        )
        if not found:
            raise TagNotFoundError(
                f"No dataset in study {self.study!r} has a column {column!r}."
            )
        if len(found) > 1:
            raise AmbiguousColumnError(
                f"Column {column!r} is in datasets {found}; name the dataset, e.g. "
                f"get_data({found[0]!r}, columns=[{column!r}])."
            )
        return found[0]

    def _metadata(self, tag: str, kind: str = RESULT_KIND) -> dict[str, Any]:
        """Return a run's metadata: its tag, plus what reproduces it."""
        metadata: dict[str, Any] = {
            **_tag_metadata(tag, kind),
            "flexorch_version": version("flex-orchestrator"),
            "flexpse_version": _version_of("flex-pse"),
            "acquirium_version": _version_of("acquirium"),
            "flexorch_user": self.user or _user(),
        }
        if kind == RESULT_KIND:
            hooks = self.hooks.merged(getattr(self._call, "hooks", None) or HookSet())
            metadata["flexorch_hooks"] = {
                kind: _hook_name(ref)
                for kind, ref in hooks.slots().items()
                if ref is not None
            } or None
        if self.connection is not None:
            metadata["flexorch_connection"] = self.connection.metadata()
        extra = getattr(self._call, "extra", None) or {}
        metadata = {**extra, **metadata}  # extras can't replace the orchestrator's
        metadata.update(getattr(self._call, "forced", None) or {})
        return {k: v for k, v in metadata.items() if v is not None}

    def _time_sets(self, model: Any) -> list[Any] | None:
        ref = self.hooks.time_sets
        if ref is None:
            return None
        try:
            hook = self.registry.resolve_ref(ref, "time_sets")
        except HookError:
            if ref == DEFAULT_HOOKS.time_sets:
                # flex-pse isn't installed. The exporter still finds a
                # TimeBlock by duck typing; only flex-pse's partial-horizon
                # Sets need the hook.
                return None
            raise
        return hook(model) or None


@dataclass(frozen=True)
class _Written:
    run_id: str


@dataclass(frozen=True)
class _SpecLink:
    """The model spec (and overrides) a result was built from."""

    tag: str
    spec_hash: str
    overrides: dict[str, Any]
    model_inputs: list[dict[str, Any]] | None = None

    def ref(self) -> dict[str, str]:
        return {"tag": self.tag, "spec_hash": self.spec_hash}

    def metadata(self) -> dict[str, str]:
        return {
            "spec_tag": self.tag,
            "spec_hash": self.spec_hash,
            "run_hash": run_hash(self.spec_hash, self.overrides),
        }


def _builtin_commands() -> list[Command]:
    # Imported here: both modules use the orchestrator's constants.
    from flexorchestrator.fitting.command import FIT_SURROGATE
    from flexorchestrator.study.runner import EXECUTE_STUDY

    return [EXECUTE_STUDY, FIT_SURROGATE]


def _ledger_names() -> list[str]:
    return list(dict.fromkeys(LEDGERS.values()))


def _check_outcome(outcome: Any) -> None:
    if outcome is not None and not isinstance(outcome, SolveOutcome):
        raise TypeError(
            f"outcome must be a SolveOutcome, not {type(outcome).__name__}."
        )


def _version_of(dist: str) -> str | None:
    try:
        return version(dist)
    except PackageNotFoundError:
        return None


def _user() -> str | None:
    """The user: ``FLEXORCH_USER``, else the login name."""
    try:
        return os.environ.get("FLEXORCH_USER") or getpass.getuser()
    except (KeyError, OSError):
        return None


def _hook_name(ref: Any) -> str:
    if isinstance(ref, str):
        return ref
    return hook_ref(ref) or f"<{getattr(ref, '__qualname__', type(ref).__name__)}>"


def _ledger_of(kind: str, allowed: Iterable[str] = LEDGERS) -> str:
    if kind not in allowed:
        raise ValueError(f"kind must be one of {sorted(allowed)}, not {kind!r}.")
    return LEDGERS[kind]


def _tag_metadata(tag: str, kind: str = RESULT_KIND) -> dict[str, str]:
    return {"flexorch_kind": kind, "flexorch_tag": tag}
