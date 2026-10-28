"""Connecting to Acquirium (PLAN.md D11): local, Postgres-backed, or remote.

The library connects to exactly one Acquirium. :func:`connect` picks the mode
from its arguments:

1. **local** (no ``db``, no ``acquirium_url``): a managed Acquirium with DuckDB
   in ``data_dir``. No credentials.
2. **postgres** (``db=``): a managed Acquirium whose timeseries and experiment
   ledger live in Postgres (TimescaleDB); the graph and artifact files stay in
   ``data_dir``, which must persist.
3. **remote** (``acquirium_url=``): a running Acquirium, which owns its own
   database secret.

A managed server lives as long as the first process that opened its
``data_dir``. If a long-lived one is running there (``flex-orch acquirium
start``, :mod:`flex_orchestrator.service`), modes 1 and 2 attach to it instead
and never stop it. ``FLEXORCH_START_ACQUIRIUM=0`` makes them fail rather than
start a managed one.

Database credentials never come from script code. In mode 2 they're taken from
``FLEXORCH_DB_PASSWORD_FILE``, then ``FLEXORCH_DB_USER`` /
``FLEXORCH_DB_PASSWORD``, then libpq's own ``PGUSER`` / ``PGPASSWORD`` /
``~/.pgpass``. The password is never written to disk or logged: the server
gets the DSN (without the password) as ``PG_DSN`` and the password as
``PGPASSWORD``, both only in its process environment, and run metadata only
ever sees the redacted ``user@host:port/db``.

``acquirium`` and ``psycopg`` are imported only when a connection is made.
"""

import os
import socket
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr

from flex_orchestrator.errors import DBAuthError, StorageError

DEFAULT_DATA_DIR = ".flexorch"
"""str: The data directory of a local (mode 1) Acquirium."""
CONFIG_NAME = "flexorch-acquirium.toml"
"""str: The Acquirium config written into a mode 2 ``data_dir``."""
BINDING_TABLE = "flexorch_server"
"""str: The table that binds a Postgres database to one managed ``data_dir``."""

Mode = Literal["local", "postgres", "remote"]

_env_lock = threading.Lock()


@dataclass(frozen=True)
class DBCredentials:
    """A Postgres login, for developers and tests; scripts use the environment.

    Attributes:
        user: The role; ``None`` leaves it to the URL or libpq (``PGUSER``).
        password: The password; ``None`` leaves it to libpq (``PGPASSWORD``,
            ``~/.pgpass``).
    """

    user: str | None = None
    password: SecretStr | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Keep a plain-string password as a :class:`~pydantic.SecretStr`."""
        if isinstance(self.password, str):
            object.__setattr__(self, "password", SecretStr(self.password))


@dataclass(frozen=True)
class Connection:
    """A connected Acquirium and how it was reached.

    Attributes:
        mode: ``"local"``, ``"postgres"`` or ``"remote"``.
        target: Where it is, safe to log: the data directory, the redacted
            ``user@host:port/db``, or the URL.
        acquirium: The Acquirium client.
        owned: Whether this process started the server. Informational:
            :meth:`close` calls ``aq.shutdown``, which stops only a server
            this process started.
    """

    mode: Mode
    target: str
    acquirium: Any = field(repr=False)
    owned: bool = False

    def backend(self) -> Any:
        """Return the :class:`~flex_orchestrator.backend.AcquiriumBackend` on it."""
        from flex_orchestrator.backend.acquirium import AcquiriumBackend

        return AcquiriumBackend(self.acquirium)

    def metadata(self) -> dict[str, str]:
        """Return what run metadata records about the connection (D10)."""
        return {"mode": self.mode, "target": self.target}

    def close(self) -> None:
        """Disconnect; a server this process started is stopped, its data kept."""
        import acquirium as aq

        aq.shutdown()


def connect(
    *,
    data_dir: str | Path | None = None,
    db: str | None = None,
    acquirium_url: str | None = None,
    db_credentials: DBCredentials | None = None,
    exact_only: bool = True,
    timeout: float = 600.0,
    environ: Mapping[str, str] | None = None,
) -> Connection:
    """Connect to Acquirium in the mode the arguments select.

    Args:
        data_dir: The managed server's directory (graph, artifacts and, in
            mode 1, DuckDB). Defaults to :data:`DEFAULT_DATA_DIR` in mode 1;
            mode 2 needs it.
        db: A Postgres URL or conninfo string **without a password**; selects
            mode 2.
        acquirium_url: A running Acquirium, e.g. ``"http://acquirium:8000"``;
            selects mode 3.
        db_credentials: An explicit login for mode 2, for developers and tests.
        exact_only: Start the managed server without text-matching indexes.
        timeout: Seconds to wait for the server.
        environ: Where the ``FLEXORCH_DB_*`` credentials and
            ``FLEXORCH_START_ACQUIRIUM`` are read from; ``os.environ`` by
            default. libpq's own ``PG*`` variables and ``~/.pgpass`` are always
            read from the process.

    Raises:
        ValueError: For arguments that mix modes, or a password in ``db``.
        DBAuthError: If Postgres refuses the login.
        StorageError: If a check before starting fails (see :func:`preflight`),
            or ``FLEXORCH_START_ACQUIRIUM=0`` and no long-lived server is
            running for ``data_dir``.

    Example:
        >>> connect(acquirium_url="http://acquirium:8000")  # doctest: +SKIP
    """
    if acquirium_url is not None:
        if db is not None or data_dir is not None or db_credentials is not None:
            raise ValueError(
                "acquirium_url connects to a running Acquirium, which owns its "
                "storage; don't combine it with db, data_dir or db_credentials."
            )
        import acquirium as aq

        client = aq.init(address=acquirium_url, timeout=timeout)
        return Connection("remote", acquirium_url, client)
    if db is None:
        if db_credentials is not None:
            raise ValueError("db_credentials need db=, the Postgres to log in to.")
        root = Path(data_dir or DEFAULT_DATA_DIR).expanduser().resolve()
        running = _long_lived(root, timeout, environ)
        if running is not None:
            return running
        if (root / CONFIG_NAME).exists():
            raise ValueError(
                f"{root} belongs to a Postgres-backed Acquirium; pass the same "
                "db= to use it."
            )
        import acquirium as aq

        client = aq.init(data_dir=root, exact_only=exact_only, timeout=timeout)
        return Connection("local", str(root), client, owned=True)
    if data_dir is None:
        raise ValueError(
            "db= needs data_dir=: the graph and artifact files live there and "
            "must persist next to the database."
        )
    root = Path(data_dir).expanduser().resolve()
    running = _long_lived(root, timeout, environ)
    if running is not None:
        return running
    if any(root.glob("*.duckdb")):
        raise ValueError(
            f"{root} holds a local DuckDB Acquirium; use another data_dir for "
            "Postgres."
        )
    credentials = db_credentials or credentials_from_env(environ)
    dsn = postgres_dsn(db, credentials.user)
    password = credentials.password
    target = redact(dsn)
    preflight(dsn, password, root)
    config = write_config(root, exact_only)
    with _env_lock:
        env = {"PG_DSN": dsn}
        if password is not None:
            env["PGPASSWORD"] = password.get_secret_value()
        client = _with_env(env, _init_config, config, timeout, password)
    return Connection("postgres", target, client, owned=True)


def _long_lived(
    root: Path, timeout: float, environ: Mapping[str, str] | None
) -> Connection | None:
    """Attach to the long-lived server for ``root``, if one is running.

    Raises:
        StorageError: If none is, and ``FLEXORCH_START_ACQUIRIUM`` forbids
            starting a managed one.
    """
    from flex_orchestrator.service import find

    service = find(root)
    if service is None:
        env = os.environ if environ is None else environ
        if env.get("FLEXORCH_START_ACQUIRIUM", "1").strip().lower() in _NO:
            raise StorageError(
                f"No Acquirium is running for {root}, and FLEXORCH_START_ACQUIRIUM "
                "forbids starting one: run `flex-orch --data-dir "
                f"{root} acquirium start` first."
            )
        return None
    import acquirium as aq

    client = aq.init(address=service.url, timeout=timeout)
    return Connection("local", str(root), client)


_NO = ("0", "false", "no", "off")


def credentials_from_env(environ: Mapping[str, str] | None = None) -> DBCredentials:
    """Read the Postgres login from the environment (D11).

    ``FLEXORCH_DB_PASSWORD_FILE`` (a secret file) wins over
    ``FLEXORCH_DB_PASSWORD``. ``FLEXORCH_DB_USER`` names the role. Whatever is
    left unset falls to libpq: the URL, ``PGUSER``, ``PGPASSWORD`` and
    ``~/.pgpass``.

    Raises:
        StorageError: If the secret file can't be read.
    """
    environ = os.environ if environ is None else environ
    user = environ.get("FLEXORCH_DB_USER") or None
    secret_file = environ.get("FLEXORCH_DB_PASSWORD_FILE")
    if secret_file:
        try:
            password = Path(secret_file).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise StorageError(
                f"Can't read FLEXORCH_DB_PASSWORD_FILE ({secret_file}): "
                f"{exc.strerror}."
            ) from None
        return DBCredentials(user, SecretStr(password))
    password = environ.get("FLEXORCH_DB_PASSWORD")
    return DBCredentials(user, SecretStr(password) if password else None)


def postgres_dsn(db: str, user: str | None = None) -> str:
    """Return the conninfo DSN for ``db``, with ``user`` if given.

    The DSN never holds a password; libpq gets that from ``PGPASSWORD``.

    Raises:
        ValueError: If ``db`` holds a password or isn't a valid DSN (the message
            doesn't repeat ``db``).
    """
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    try:
        params = conninfo_to_dict(db)
    except psycopg.ProgrammingError:
        raise ValueError("db isn't a valid Postgres URL or conninfo string.") from None
    if "password" in params:
        raise ValueError(
            "Don't put the password in db=; set FLEXORCH_DB_PASSWORD_FILE (or "
            "FLEXORCH_DB_PASSWORD, or libpq's PGPASSWORD / ~/.pgpass) instead."
        )
    return make_conninfo(db, **({"user": user} if user else {}))


def redact(dsn: str) -> str:
    """Return ``user@host:port/db`` for a DSN, without any secret.

    A DSN without a user shows libpq's: ``PGUSER`` from the process
    environment.
    """
    from psycopg.conninfo import conninfo_to_dict

    params = conninfo_to_dict(dsn)
    user = params.get("user") or os.environ.get("PGUSER", "")
    host = params.get("host") or "localhost"
    return f"{user}@{host}:{params.get('port') or 5432}/{params.get('dbname', '')}"


def preflight(dsn: str, password: SecretStr | None, data_dir: Path) -> None:
    """Check Postgres before starting a managed server on it (D11).

    Checks the login, that TimescaleDB is available, that the role can create
    tables, and binds the database to ``data_dir`` on this host: a second
    managed server with another ``data_dir`` is refused, since the graph and
    files that match the database live in the first one's.

    Raises:
        DBAuthError: If the login is refused.
        StorageError: If Postgres can't be reached or a check fails.
    """
    import psycopg

    where = redact(dsn)
    secret = password.get_secret_value() if password is not None else None
    try:
        conn = psycopg.connect(
            dsn,
            autocommit=True,
            connect_timeout=10,
            **({"password": secret} if secret is not None else {}),
        )
    except psycopg.OperationalError as exc:
        message = _scrub(str(exc).strip(), secret)
        if "authentication" in message.lower() or "password" in message.lower():
            raise DBAuthError(
                f"Postgres refused the login for {where}: {message} Check "
                "FLEXORCH_DB_USER and FLEXORCH_DB_PASSWORD_FILE."
            ) from None
        raise StorageError(f"Can't reach Postgres at {where}: {message}") from None
    with conn:
        if (
            conn.execute(
                "SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'"
            ).fetchone()
            is None
        ):
            raise StorageError(
                f"Postgres at {where} doesn't have the timescaledb extension; "
                "Acquirium stores timeseries in TimescaleDB."
            )
        if not conn.execute(
            "SELECT has_schema_privilege(current_schema(), 'CREATE')"
        ).fetchone()[0]:
            raise StorageError(
                f"The role at {where} can't create tables in its schema; "
                "Acquirium creates its own."
            )
        _bind(conn, where, f"{socket.gethostname()}:{data_dir}")


def write_config(data_dir: Path, exact_only: bool) -> Path:
    """Write the mode 2 Acquirium config into ``data_dir`` and return its path.

    It selects TimescaleDB and never sets ``recreate``; it holds no ``pg_dsn``
    (the DSN reaches the server only through its environment).
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / CONFIG_NAME
    path.write_text(
        "# Written by flex-orchestrator (PLAN.md D11). The database DSN and\n"
        "# password reach the server only through its environment.\n"
        "[server]\n"
        f"data_dir = {_toml_string(str(data_dir))}\n"
        'timeseries_backend = "timescale"\n'
        f"exact_only = {'true' if exact_only else 'false'}\n"
        "recreate = false\n",
        encoding="utf-8",
    )
    return path


def _bind(conn: Any, where: str, owner: str) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {BINDING_TABLE} ("
        " only_row boolean PRIMARY KEY DEFAULT true CHECK (only_row),"
        " data_dir text NOT NULL,"
        " bound_at timestamptz NOT NULL DEFAULT now())"
    )
    conn.execute(
        f"INSERT INTO {BINDING_TABLE} (data_dir) VALUES (%s) ON CONFLICT DO NOTHING",
        (owner,),
    )
    (bound,) = conn.execute(f"SELECT data_dir FROM {BINDING_TABLE}").fetchone()
    if bound != owner:
        raise StorageError(
            f"Postgres at {where} belongs to the managed Acquirium at {bound}; its "
            "graph and files are there. Use that data_dir, or connect to its "
            "server with acquirium_url=."
        )


def _init_config(config: Path, timeout: float, password: SecretStr | None) -> Any:
    import acquirium as aq

    try:
        return aq.init(config=config, timeout=timeout)
    except (RuntimeError, ValueError, TimeoutError) as exc:
        secret = password.get_secret_value() if password is not None else None
        raise StorageError(
            f"The Postgres-backed Acquirium didn't start: {_scrub(str(exc), secret)}"
        ) from None


def _with_env(env: Mapping[str, str], fn: Any, *args: Any) -> Any:
    """Call ``fn`` with ``env`` set, so only the server process inherits it."""
    previous = {name: os.environ.get(name) for name in env}
    os.environ.update(env)
    try:
        return fn(*args)
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _scrub(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret else text


def _toml_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
