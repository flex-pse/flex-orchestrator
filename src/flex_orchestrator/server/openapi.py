"""The API's OpenAPI schema, as a file a UI's client is generated from (D16, M8).

``flex-orch openapi --output docs/schemas/openapi.json`` writes the core
routes' schema: no plugins, a placeholder tenant. The copy in the repository
is the contract with the UI; a test fails when the routes change and it
doesn't. A tenant's own schema, with its plugins' routes, is at its
``/openapi.json``.
"""

import json
from typing import Any

SCHEMA_PATH = "docs/schemas/openapi.json"
"""str: Where the repository keeps it."""


def core_schema() -> dict[str, Any]:
    """Return the OpenAPI schema of the core routes."""
    from flex_orchestrator.backend.memory import MemoryBackend
    from flex_orchestrator.server.app import create_app
    from flex_orchestrator.server.auth import Principal
    from flex_orchestrator.server.settings import Settings

    # A deployed tenant's API (bearer tokens); the token is a placeholder.
    settings = Settings(
        tenant="tenant",
        api_tokens={"sha256:none": Principal("nobody", "tenant", "viewer")},
        orchestrator={"backend": MemoryBackend()},
        environ={},
    )
    return create_app(settings).openapi()


def dumps(schema: dict[str, Any]) -> str:
    """The schema as the file holds it: sorted keys, two-space indents."""
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"
