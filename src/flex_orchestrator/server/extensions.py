"""Plugin routes (PLAN.md D18): mounted under ``/ext/<plugin>``, never unauthenticated.

A plugin's ``routers`` are FastAPI ``APIRouter`` objects. Each is mounted at
``/ext/<plugin name>`` with :func:`~flex_orchestrator.server.deps.current_principal`
applied to the whole router, so a plugin route can't be reached without a
valid token for this tenant, even if its author forgot. A route that wants a
role adds ``Depends(require_role(...))`` itself; the default is any role.

The core prefixes are reserved; a plugin router whose own prefix claims one,
or two plugins' routes on the same path and method, fail at startup.
"""

from typing import Any

from fastapi import Depends, FastAPI

from flex_orchestrator.errors import PluginError
from flex_orchestrator.server.deps import current_principal

RESERVED = ("/studies", "/jobs", "/commands", "/health", "/ready", "/ext", "/docs")
"""tuple[str, ...]: Prefixes the core owns; a plugin router can't claim them."""


def mount(app: FastAPI, plugins: Any) -> list[str]:
    """Mount every plugin's routers; return the prefixes mounted.

    Raises:
        PluginError: If a router claims a reserved prefix, or two routes would
            answer the same path and method.
    """
    seen: dict[tuple[str, str], str] = {}
    mounted = []
    for plugin in plugins:
        for router in getattr(plugin, "routers", ()) or ():
            own = getattr(router, "prefix", "") or ""
            if any(own == p or own.startswith(p + "/") for p in RESERVED):
                raise PluginError(
                    f"Plugin {plugin.name!r} mounts a router at {own!r}, which the "
                    f"core reserves ({', '.join(RESERVED)}); its routes go under "
                    f"/ext/{plugin.name} by themselves."
                )
            prefix = f"/ext/{plugin.name}"
            for route in router.routes:
                for method in getattr(route, "methods", None) or ():
                    key = (prefix + route.path, method)
                    if key in seen:
                        raise PluginError(
                            f"Plugins {seen[key]!r} and {plugin.name!r} both answer "
                            f"{method} {key[0]}."
                        )
                    seen[key] = plugin.name
            app.include_router(
                router,
                prefix=prefix,
                dependencies=[Depends(current_principal)],
                tags=[f"plugin:{plugin.name}"],
                # The plugin's name in front, so a generated client's
                # function can't collide with a core route's.
                generate_unique_id_function=lambda route, name=plugin.name: (
                    f"{name}_{route.name}"
                ),
            )
            mounted.append(prefix + own)
    return mounted
