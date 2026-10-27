"""Repo-wide pytest configuration: tier-marker enforcement and unit-tier guards.

Mirrors flex-pse's conftest: every test carries exactly one tier marker, a
``needs_<solver>`` marker skips (never fails) when that solver is missing, and
unit tests can't invoke a solver or open a network connection.
"""

import copy
import importlib.util

import pytest

TIER_MARKERS = {"unit", "component", "integration", "benchmark"}
BACKEND_TIERS = {"memory": "unit", "files": "unit", "acquirium": "component"}

HAS_FLEXPSE = importlib.util.find_spec("flexops") is not None


def pytest_collection_modifyitems(config, items):
    errors = []
    for item in items:
        tiers = TIER_MARKERS & {m.name for m in item.iter_markers()}
        if len(tiers) != 1:
            errors.append(
                f"{item.nodeid}: needs exactly one tier marker "
                f"({'/'.join(sorted(TIER_MARKERS))}), got {sorted(tiers) or 'none'}"
            )
    if errors:
        raise pytest.UsageError("\n".join(errors))

    # The benchmark tier is opt-in: it runs only when selected with -m.
    if "benchmark" not in (config.option.markexpr or ""):
        skip = pytest.mark.skip(reason="benchmark tier: run with -m benchmark")
        for item in items:
            if item.get_closest_marker("benchmark"):
                item.add_marker(skip)

    needs = {
        marker.name[len("needs_") :]
        for item in items
        for marker in item.iter_markers()
        if marker.name.startswith("needs_")
    }
    if not needs:
        return
    available = {}
    if HAS_FLEXPSE:
        from flexcore.solvers.registry import available_solvers

        available = available_solvers()
    for item in items:
        for marker in item.iter_markers():
            if marker.name.startswith("needs_"):
                name = marker.name[len("needs_") :]
                if name not in available:
                    item.add_marker(
                        pytest.mark.skip(reason=f"solver {name} not installed")
                    )


@pytest.fixture(autouse=True)
def _no_solver_in_unit_tier(request, monkeypatch):
    """Block solver invocation and network access during unit-tier tests."""
    if "unit" not in {m.name for m in request.node.iter_markers()}:
        return

    import socket

    def _forbidden(*args, **kwargs):
        raise RuntimeError("solver invocation is forbidden in unit-tier tests")

    if HAS_FLEXPSE:
        from flexcore.solvers import facade

        monkeypatch.setattr(facade.SolverFacade, "solve", _forbidden)

    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def _loopback(address):
        # An event loop's own socket pair (asyncio on Windows, so FastAPI's
        # TestClient) connects to itself; nothing else may.
        return isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1")

    def _no_network(self, address, *args, **kwargs):
        if _loopback(address):
            return connect(self, address, *args, **kwargs)
        raise RuntimeError("network access is forbidden in unit-tier tests")

    def _no_network_ex(self, address, *args, **kwargs):
        if _loopback(address):
            return connect_ex(self, address, *args, **kwargs)
        raise RuntimeError("network access is forbidden in unit-tier tests")

    monkeypatch.setattr(socket.socket, "connect", _no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _no_network_ex)


@pytest.fixture
def demo_spec():
    """A fresh copy of the shared flex-pse demo flowsheet spec."""
    from flex_orchestrator.testing import DEMO_SPEC

    return copy.deepcopy(DEMO_SPEC)


@pytest.fixture
def demo_model(demo_spec):
    """The flex-pse demo, built (not solved) with the default build hook."""
    pytest.importorskip("flexops")
    from flex_orchestrator.flexpse import build

    return build(demo_spec)


def _build(spec):
    pytest.importorskip("flexops")
    from flex_orchestrator.flexpse import build

    return build(copy.deepcopy(spec))


@pytest.fixture
def logic_model():
    """flex-pse's indexed RO trains ``p.ro[0..2]`` over ``p.trains = [0, 1, 2]``.

    ``p.trains`` holds valid time positions but isn't time (a decoy), and each
    train's ``startup`` / ``shutdown`` is indexed over every time point but the
    first, by an anonymous Set (a real subset of time).
    """
    from flex_orchestrator.testing import LOGIC_SPEC

    return _build(LOGIC_SPEC)


@pytest.fixture
def surrogate_model():
    """A pump surrogate's ``coefficient_vars`` and a battery with fixed dispatch."""
    from flex_orchestrator.testing import SURROGATE_DISPATCH_SPEC

    return _build(SURROGATE_DISPATCH_SPEC)


@pytest.fixture
def network_model():
    """Paths through a network of two plants."""
    from flex_orchestrator.testing import NETWORK_SPEC

    return _build(NETWORK_SPEC)


@pytest.fixture
def memory_backend():
    """An empty in-memory backend."""
    from flex_orchestrator.backend import MemoryBackend

    return MemoryBackend()


@pytest.fixture(scope="session")
def acquirium_dir(tmp_path_factory):
    """The session Acquirium's data directory."""
    return tmp_path_factory.mktemp("acquirium")


@pytest.fixture(scope="session")
def acquirium_local(acquirium_dir):
    """A local Acquirium (DuckDB) for the whole session; tests use fresh studies.

    Starting the server takes about half a minute, so it is started once.
    """
    aq = pytest.importorskip("acquirium")
    client = aq.init(data_dir=acquirium_dir, exact_only=True, timeout=300)
    yield client
    aq.shutdown()


def _make_backend(request, kind):
    if kind == "memory":
        return request.getfixturevalue("memory_backend")
    if kind == "files":
        from flex_orchestrator.backend import FileBackend

        return FileBackend(request.getfixturevalue("tmp_path_factory").mktemp("files"))
    from flex_orchestrator.backend import AcquiriumBackend

    return AcquiriumBackend(request.getfixturevalue("acquirium_local"))


@pytest.fixture(
    params=[
        pytest.param("memory", marks=pytest.mark.unit),
        pytest.param("files", marks=pytest.mark.unit),
        pytest.param("acquirium", marks=pytest.mark.component),
    ]
)
def backend(request):
    """Each backend in turn: memory and files (unit tier), Acquirium (component)."""
    return _make_backend(request, request.param)


def pytest_generate_tests(metafunc):
    """``orch_backend`` is memory and files; ``on_acquirium`` adds Acquirium.

    A test with no tier marker of its own gets one per backend: unit for
    memory and files, component for Acquirium.
    """
    if "orch_backend" not in metafunc.fixturenames:
        return
    kinds = ["memory", "files"]
    if metafunc.definition.get_closest_marker("on_acquirium"):
        kinds.append("acquirium")
    tiered = TIER_MARKERS & {m.name for m in metafunc.definition.iter_markers()}
    params = [
        pytest.param(
            kind,
            marks=() if tiered else getattr(pytest.mark, BACKEND_TIERS[kind]),
            id=kind,
        )
        for kind in kinds
    ]
    metafunc.parametrize("orch_backend", params, indirect=True)


@pytest.fixture
def orch_backend(request):
    """The backend ``orch`` runs on (see ``pytest_generate_tests``).

    Acquirium is slow to write to, so only tests marked ``on_acquirium`` use
    it: those about Acquirium itself, and one end-to-end test per feature.
    """
    return _make_backend(request, request.param)


@pytest.fixture
def study_name():
    """A study name no other test uses (the Acquirium server is shared)."""
    import uuid

    return f"study-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def orch(orch_backend, study_name):
    """An orchestrator on each ``orch_backend``, with a fresh study selected."""
    from flex_orchestrator.orchestrator import Orchestrator

    orchestrator = Orchestrator(orch_backend)
    orchestrator.create_study(study_name)
    return orchestrator
