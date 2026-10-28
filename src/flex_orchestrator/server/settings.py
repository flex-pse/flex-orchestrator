"""The server's settings: the tenant stack's environment contract (PLAN.md D16).

::

    FLEXORCH_TENANT          the tenant this server serves (tokens must name it)
    FLEXORCH_OIDC_ISSUER     the identity provider (its JWKS is discovered)
    FLEXORCH_OIDC_AUDIENCE   the audience tokens must be issued for
    FLEXORCH_OIDC_JWKS_URL   the JWKS, if it isn't the issuer's discovered one
    FLEXORCH_API_TOKENS      static tokens, without an identity provider (D15)
    FLEXORCH_API_TOKENS_FILE more of them, one per line (deploy/tenant/api_token.sh)
    FLEXORCH_ACQUIRIUM_URL   the tenant's Acquirium (mode 3)
    FLEXORCH_PLUGINS         the plugins to load (D18)
    FLEXORCH_CORS_ORIGINS    browser origins allowed to call it, comma-separated
    FLEXORCH_IDLE_MINUTES    stop after this long with no requests (unset: never)
    FLEXORCH_ROOT_PATH       the prefix a gateway serves it under, e.g. /tenant_a

``FLEXORCH_API_TOKENS`` lists ``user:role:sha256:<hex>`` entries separated by
``;`` or newlines: the SHA-256 of each token, never the token
(:func:`hash_token`). A token is an opaque random string handed to one user.

Local mode (:meth:`Settings.local`, ``flex-orch serve --local``) is for one
person's app on their own machine: no tokens, and every request is that user,
as owner. It serves loopback only (see :mod:`flex_orchestrator.server.auth`).
"""

import getpass
import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from flex_orchestrator.server.auth import ROLES, Principal

TOKEN_PREFIX = "sha256:"


def hash_token(token: str) -> str:
    """Return how ``FLEXORCH_API_TOKENS`` stores ``token``: ``sha256:<hex>``.

    Example:
        >>> hash_token("s3cret")[:15]
        'sha256:7f2b7d7e'
    """
    return TOKEN_PREFIX + hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Settings:
    """What the app serves, and how it checks callers.

    Attributes:
        tenant: The tenant; every token's ``tenant`` claim must equal it.
        oidc_issuer: The identity provider's issuer URL; JWTs are checked
            against its keys. ``None`` takes static tokens only.
        oidc_audience: The ``aud`` JWTs must carry.
        jwks_url: Where the issuer's keys are; discovered from
            ``<issuer>/.well-known/openid-configuration`` if ``None``.
        jwks: The keys themselves (a JWKS dict), instead of fetching them:
            for tests and air-gapped stacks.
        api_tokens: ``{hash: Principal}`` from ``FLEXORCH_API_TOKENS``.
        local_user: Local mode: the user every request is, as owner, with no
            tokens; ``None`` checks tokens.
        cors_origins: Browser origins (``https://ui.example.org``) allowed to
            call the API from a page; in local mode, loopback origins are too.
        idle_minutes: Stop the server after this long with no requests and no
            job running (:mod:`~flex_orchestrator.server.idle`); ``None`` runs
            until stopped.
        root_path: The path prefix a gateway serves the API under, e.g.
            ``/tenant_a``; ``""`` at the root. The gateway strips it, and ``flex-orch
            serve`` gives it to uvicorn, which puts it back, so the schema's
            ``servers`` and the docs name it.
        plugins: The plugins to load (names or :class:`~flex_orchestrator.ext.Plugin`).
        orchestrator: Keyword arguments for the orchestrator (``backend=``,
            ``acquirium_url=``, ...); by default it connects as
            ``Orchestrator()`` does (``FLEXORCH_ACQUIRIUM_URL``).
        environ: Where the orchestrator reads its ``FLEXORCH_*`` settings.
    """

    tenant: str
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    jwks_url: str | None = None
    jwks: dict[str, Any] | None = None
    api_tokens: dict[str, Principal] = field(default_factory=dict)
    local_user: str | None = None
    cors_origins: tuple[str, ...] = ()
    idle_minutes: float | None = None
    root_path: str = ""
    plugins: tuple[Any, ...] = ()
    orchestrator: dict[str, Any] = field(default_factory=dict)
    environ: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        """Refuse settings that couldn't authenticate anyone."""
        if not self.tenant:
            raise ValueError("Settings need the tenant (FLEXORCH_TENANT).")
        if self.idle_minutes is not None and self.idle_minutes <= 0:
            raise ValueError("FLEXORCH_IDLE_MINUTES must be positive (unset: never).")
        if self.root_path and (
            not self.root_path.startswith("/") or self.root_path.endswith("/")
        ):
            raise ValueError(
                "FLEXORCH_ROOT_PATH is like /tenant_a: a leading /, no trailing one."
            )
        for origin in self.cors_origins:
            if origin == "*" or not origin.startswith(("http://", "https://")):
                raise ValueError(
                    "FLEXORCH_CORS_ORIGINS lists origins like "
                    f"https://ui.example.org, not {origin!r}."
                )
        if self.local_user is not None:
            if self.oidc_issuer or self.jwks or self.api_tokens:
                raise ValueError("Local mode takes no tokens or identity provider.")
            return
        if self.oidc_issuer is None and self.jwks is None and not self.api_tokens:
            raise ValueError(
                "No way to authenticate: set FLEXORCH_OIDC_ISSUER (with "
                "FLEXORCH_OIDC_AUDIENCE) or FLEXORCH_API_TOKENS."
            )
        if (self.oidc_issuer is not None or self.jwks is not None) and not (
            self.oidc_audience
        ):
            raise ValueError("JWTs need an audience (FLEXORCH_OIDC_AUDIENCE).")

    @classmethod
    def local(
        cls, user: str | None = None, *, tenant: str = "local", **kwargs: Any
    ) -> "Settings":
        """Settings for local mode: no tokens; every request is ``user``, as owner.

        Args:
            user: Who the requests are; the logged-in user by default.
            tenant: The tenant's name, in the app's title.
            **kwargs: Other settings (``orchestrator=``, ``plugins=``, ...).
        """
        return cls(tenant=tenant, local_user=user or getpass.getuser(), **kwargs)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Read the settings from ``environ`` (``os.environ`` by default).

        Raises:
            ValueError: If a setting is missing or malformed; the message names
                it (and never repeats a token).
        """
        env = os.environ if environ is None else environ
        tokens = env.get("FLEXORCH_API_TOKENS", "")
        if env.get("FLEXORCH_API_TOKENS_FILE"):
            try:
                with open(env["FLEXORCH_API_TOKENS_FILE"], encoding="utf-8") as file:
                    tokens += "\n" + file.read()
            except OSError as exc:
                raise ValueError(
                    f"Can't read FLEXORCH_API_TOKENS_FILE: {exc.strerror}"
                ) from None
        idle = env.get("FLEXORCH_IDLE_MINUTES", "").strip()
        try:
            idle_minutes = float(idle) if idle else None
        except ValueError:
            raise ValueError(
                "FLEXORCH_IDLE_MINUTES must be a number of minutes."
            ) from None
        origins = env.get("FLEXORCH_CORS_ORIGINS", "").split(",")
        return cls(
            tenant=env.get("FLEXORCH_TENANT", ""),
            cors_origins=tuple(o.strip() for o in origins if o.strip()),
            idle_minutes=idle_minutes,
            root_path=env.get("FLEXORCH_ROOT_PATH", "").strip(),
            oidc_issuer=env.get("FLEXORCH_OIDC_ISSUER") or None,
            oidc_audience=env.get("FLEXORCH_OIDC_AUDIENCE") or None,
            jwks_url=env.get("FLEXORCH_OIDC_JWKS_URL") or None,
            api_tokens=parse_api_tokens(tokens, env.get("FLEXORCH_TENANT", "")),
            environ=env,
        )


def parse_api_tokens(text: str, tenant: str) -> dict[str, Principal]:
    """Parse ``FLEXORCH_API_TOKENS`` into ``{hash: Principal}``.

    Raises:
        ValueError: For an entry that isn't ``user:role:sha256:<hex>``, or an
            unknown role.
    """
    found: dict[str, Principal] = {}
    for number, entry in enumerate(text.replace("\n", ";").split(";"), 1):
        entry = entry.strip()
        if not entry:
            continue
        user, _, rest = entry.partition(":")
        role, _, digest = rest.partition(":")
        if not user or role not in ROLES or not digest.startswith(TOKEN_PREFIX):
            raise ValueError(
                f"FLEXORCH_API_TOKENS entry {number} isn't user:role:sha256:<hex> "
                f"with a role of {list(ROLES)}."
            )
        found[digest] = Principal(user=user, tenant=tenant, role=role)
    return found
