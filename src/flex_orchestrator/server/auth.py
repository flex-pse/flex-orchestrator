"""Who is calling, and may they (PLAN.md D15).

Every request carries ``Authorization: Bearer <token>``. The token is either a
JWT from the tenant's identity provider, checked against its keys (JWKS) for
signature, ``iss``, ``aud`` and ``exp``, whose ``tenant`` claim must be this
server's tenant and whose ``role`` claim says what the caller may do; or, with
no identity provider, one of ``FLEXORCH_API_TOKENS`` (compared by hash).

Roles rank ``viewer`` < ``editor`` < ``owner``: viewers read, editors store and
run, owners delete. The orchestrator stores no users and no passwords.

In local mode (``Settings.local_user``) there are no tokens: every request is
that user, as owner. So that nothing else can make those requests, each must
come from this machine (a loopback client), name a loopback ``Host`` (no DNS
rebinding), and, if a browser sent it, come from a loopback page's ``Origin``
(no other website can call it).
"""

import hashlib
import hmac
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

Role = Literal["viewer", "editor", "owner"]
ROLES: tuple[str, ...] = ("viewer", "editor", "owner")
"""tuple[str, ...]: The roles, least first."""

_LOOPBACK = ("127.0.0.1", "::1", "localhost")
_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]


@dataclass(frozen=True)
class Principal:
    """The caller of a request.

    Attributes:
        user: Who: the token's ``sub`` (or ``preferred_username``), or the
            static token's user.
        tenant: The tenant the token is for.
        role: ``viewer``, ``editor`` or ``owner``.
    """

    user: str
    tenant: str
    role: str

    def allows(self, role: str) -> bool:
        """Whether this caller's role is at least ``role``."""
        return ROLES.index(self.role) >= ROLES.index(role)


class AuthError(Exception):
    """A request that can't be served.

    Attributes:
        status: 401 (no or bad credentials) or 403 (valid, but not allowed).
    """

    def __init__(self, status: int, message: str) -> None:
        """Keep the status with the message."""
        super().__init__(message)
        self.status = status


class Authenticator:
    """Turns a bearer token into a :class:`Principal`, or refuses it.

    Args:
        settings: The server's :class:`~flex_orchestrator.server.Settings`.
    """

    def __init__(self, settings: Any) -> None:
        """Set up the static tokens and, lazily, the JWKS client."""
        self.settings = settings
        self._jwks_client: Any = None
        self._lock = threading.Lock()

    def authenticate(
        self,
        header: str | None,
        client: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Principal:
        """Return the caller of a request with this ``Authorization`` header.

        Args:
            header: The ``Authorization`` header.
            client: The client's address (local mode only).
            headers: The request's headers, for ``Host`` and ``Origin`` (local
                mode only).

        Raises:
            AuthError: 401 for a missing, malformed, unknown or invalid token;
                403 for a valid token for another tenant, or with no known role,
                or, in local mode, a request from anywhere but this machine.
        """
        if self.settings.local_user is not None:
            return self._local(client, headers or {})
        scheme, _, token = (header or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthError(401, "Send Authorization: Bearer <token>.")
        token = token.strip()
        static = self._static(token)
        if static is not None:
            return static
        if self.settings.oidc_issuer is None and self.settings.jwks is None:
            raise AuthError(401, "The token isn't valid here.")
        return self._jwt(token)

    def _local(self, client: str | None, headers: Mapping[str, str]) -> Principal:
        host = _hostname(headers.get("host", ""))
        origin = headers.get("origin")
        if client not in _LOOPBACK or host not in _LOOPBACK:
            raise AuthError(403, "Local mode serves this machine only.")
        if origin is not None and _hostname(origin.partition("://")[2]) not in (
            _LOOPBACK
        ):
            raise AuthError(403, "Local mode serves pages on this machine only.")
        return Principal(self.settings.local_user, self.settings.tenant, "owner")

    def _static(self, token: str) -> Principal | None:
        digest = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
        for known, principal in self.settings.api_tokens.items():
            if hmac.compare_digest(known, digest):
                return principal
        return None

    def _jwt(self, token: str) -> Principal:
        import jwt

        try:
            key = self._signing_key(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=_ALGORITHMS,
                audience=self.settings.oidc_audience,
                issuer=self.settings.oidc_issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(401, f"The token isn't valid: {exc}") from None
        tenant = claims.get("tenant")
        if tenant != self.settings.tenant:
            raise AuthError(403, "The token is for another tenant.")
        role = claims.get("role")
        if role not in ROLES:
            raise AuthError(403, f"The token's role must be one of {list(ROLES)}.")
        user = claims.get("preferred_username") or claims["sub"]
        return Principal(user=str(user), tenant=tenant, role=role)

    def _signing_key(self, token: str) -> Any:
        import jwt

        if self.settings.jwks is not None:
            header = jwt.get_unverified_header(token)
            keys = jwt.PyJWKSet.from_dict(self.settings.jwks).keys
            for key in keys:
                if header.get("kid") in (None, key.key_id):
                    return key.key
            raise jwt.InvalidKeyError("No key matches the token's kid.")
        with self._lock:
            if self._jwks_client is None:
                self._jwks_client = jwt.PyJWKClient(self._jwks_url(), cache_keys=True)
        return self._jwks_client.get_signing_key_from_jwt(token).key

    def _jwks_url(self) -> str:
        if self.settings.jwks_url:
            return self.settings.jwks_url
        import json
        import urllib.request

        issuer = self.settings.oidc_issuer.rstrip("/")
        url = f"{issuer}/.well-known/openid-configuration"
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
            return json.load(response)["jwks_uri"]


def _hostname(authority: str) -> str:
    """``host`` from ``host[:port]`` or ``[v6]:port``, lowercased."""
    authority = authority.rstrip("/").lower()
    if authority.startswith("["):
        return authority[1:].partition("]")[0]
    return authority.rpartition(":")[0] if authority.count(":") == 1 else authority
