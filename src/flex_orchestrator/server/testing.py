"""Test helpers for the HTTP API and plugin routes (PLAN.md D18).

::

    from fastapi.testclient import TestClient
    from flex_orchestrator.server.testing import make_test_app, token_for

    client = TestClient(make_test_app(plugins=[my_plugin]))
    headers = {"Authorization": f"Bearer {token_for('editor')}"}
    client.post("/commands/my_command", json={...}, headers=headers)

The app runs on a fresh memory backend with the fake hooks, and trusts one
RSA key made for the process; :func:`token_for` signs JWTs with it, so tokens
are checked exactly as a real identity provider's are.
"""

import functools
import time
import uuid
from typing import Any

TENANT = "test"
"""str: The test app's tenant."""
ISSUER = "https://idp.test/realms/flexorch"
AUDIENCE = "flex-orchestrator"
_KID = "flexorch-test"


@functools.cache
def _key() -> Any:
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks() -> dict[str, Any]:
    """The test key's public half, as a JWKS."""
    import json

    from jwt.algorithms import RSAAlgorithm

    public = json.loads(RSAAlgorithm.to_jwk(_key().public_key()))
    return {"keys": [{**public, "kid": _KID, "use": "sig", "alg": "RS256"}]}


def token_for(
    role: str = "editor",
    tenant: str = TENANT,
    *,
    user: str = "tester",
    expires_in: int = 3600,
    **claims: Any,
) -> str:
    """Return a JWT for ``role`` in ``tenant``, signed with the test key.

    Args:
        role: ``viewer``, ``editor`` or ``owner``.
        tenant: The ``tenant`` claim; another tenant's gives 403.
        user: The ``sub`` and ``preferred_username``.
        expires_in: Seconds until ``exp``; negative for an expired token.
        **claims: More claims, or replacements (``aud=...``).
    """
    import jwt

    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": user,
        "preferred_username": user,
        "iat": now,
        "exp": now + expires_in,
        "jti": uuid.uuid4().hex,
        "tenant": tenant,
        "role": role,
        **claims,
    }
    return jwt.encode(payload, _key(), algorithm="RS256", headers={"kid": _KID})


def make_test_app(
    plugins: Any = None, *, backend: Any = None, api_tokens: Any = None, **settings: Any
) -> Any:
    """Return an app for tests: a memory store, the fake hooks, the test key.

    Args:
        plugins: Plugins to load.
        backend: The store; a fresh :class:`~flex_orchestrator.backend.MemoryBackend`
            by default.
        api_tokens: Static tokens, ``{hash: Principal}``, as well.
        **settings: Other :class:`~flex_orchestrator.server.Settings` fields.
    """
    from flex_orchestrator.backend import MemoryBackend
    from flex_orchestrator.server.app import create_app
    from flex_orchestrator.server.settings import Settings
    from flex_orchestrator.testing.orchestrators import FAKE_HOOKS

    orchestrator = {
        "backend": backend if backend is not None else MemoryBackend(),
        "hooks": FAKE_HOOKS,
        **settings.pop("orchestrator", {}),
    }
    return create_app(
        Settings(
            tenant=settings.pop("tenant", TENANT),
            oidc_issuer=ISSUER,
            oidc_audience=AUDIENCE,
            jwks=jwks(),
            api_tokens=dict(api_tokens or {}),
            orchestrator=orchestrator,
            environ=settings.pop("environ", {}),
            **settings,
        ),
        plugins=plugins,
    )
