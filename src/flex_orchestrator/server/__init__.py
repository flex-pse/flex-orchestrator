"""The tenant's HTTP API (PLAN.md Phase 2, M7).

Install it with ``pip install flex-orchestrator[server]``.

::

    from flex_orchestrator.server import Settings, create_app

    app = create_app(Settings.from_env())        # or: flex-orch serve

Nothing outside this package imports it, or FastAPI: the library works
without it. Plugin routes use :mod:`flex_orchestrator.server.deps`.
"""

from flex_orchestrator.server.app import API_VERSION, create_app
from flex_orchestrator.server.auth import ROLES, Principal
from flex_orchestrator.server.settings import Settings, hash_token

__all__ = ["API_VERSION", "ROLES", "Principal", "Settings", "create_app", "hash_token"]
