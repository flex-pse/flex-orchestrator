"""Connection modes and database credentials (PLAN.md D11).

Acquirium and Postgres are faked here (unit tier), except one component test
that attaches to the session's local Acquirium.
"""

import os
from pathlib import Path
from urllib.parse import quote

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr

from flex_orchestrator import connection
from flex_orchestrator.backend import MemoryBackend
from flex_orchestrator.connection import (
    BINDING_TABLE,
    CONFIG_NAME,
    Connection,
    DBCredentials,
    connect,
    credentials_from_env,
    postgres_dsn,
    preflight,
    redact,
)
from flex_orchestrator.errors import DBAuthError, StorageError
from flex_orchestrator.orchestrator import Orchestrator

pytestmark = pytest.mark.unit

SECRET = "s3cr@t:/% 'pw"
DB = "postgresql://db-host:5433/tenant_a"


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakePostgres:
    """Answers the preflight queries; keeps the binding row across connects."""

    def __init__(self, timescale=True, create=True):
        self.timescale = timescale
        self.create = create
        self.bound = None
        self.kwargs = None

    def connect(self, dsn, **kwargs):
        self.kwargs = {"dsn": dsn, **kwargs}
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if "pg_available_extensions" in sql:
            return FakeResult([(1,)] if self.timescale else [])
        if "has_schema_privilege" in sql:
            return FakeResult([(self.create,)])
        if sql.startswith("INSERT"):
            assert BINDING_TABLE in sql
            if self.bound is None:
                self.bound = params[0]
        if sql.startswith("SELECT data_dir"):
            return FakeResult([(self.bound,)])
        return FakeResult([])


@pytest.fixture
def postgres(monkeypatch):
    fake = FakePostgres()
    monkeypatch.setattr(psycopg, "connect", fake.connect)
    return fake


@pytest.fixture
def aq(monkeypatch):
    """A fake ``acquirium.init``; records each call and the environment then."""
    acquirium = pytest.importorskip("acquirium")
    calls = []

    def init(**kwargs):
        calls.append(
            {
                **kwargs,
                "env": {k: os.environ.get(k) for k in ("PG_DSN", "PGPASSWORD")},
            }
        )
        return "client"

    monkeypatch.setattr(acquirium, "init", init)
    monkeypatch.setattr(acquirium, "shutdown", lambda: calls.append("shutdown"))
    return calls


@pytest.fixture
def clean_env(monkeypatch):
    for name in (
        "FLEXORCH_DB_USER",
        "FLEXORCH_DB_PASSWORD",
        "FLEXORCH_DB_PASSWORD_FILE",
        "PG_DSN",
        "PGPASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)


# Mode selection


def test_local_is_the_default(aq, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    conn = connect()
    assert (conn.mode, conn.owned) == ("local", True)
    assert aq[0]["data_dir"] == tmp_path / ".flexorch"
    assert aq[0]["exact_only"] is True
    assert conn.target == str(tmp_path / ".flexorch")


def test_remote(aq):
    conn = connect(acquirium_url="http://acquirium:8000", timeout=5)
    assert (conn.mode, conn.target, conn.owned) == (
        "remote",
        "http://acquirium:8000",
        False,
    )
    assert aq[0]["address"] == "http://acquirium:8000"


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"acquirium_url": "http://a:1", "db": DB}, "don't combine"),
        ({"acquirium_url": "http://a:1", "data_dir": "x"}, "don't combine"),
        ({"db_credentials": DBCredentials("u", "p")}, "need db="),
        ({"db": DB}, "needs data_dir="),
    ],
)
def test_conflicting_arguments(aq, kwargs, message):
    with pytest.raises(ValueError, match=message):
        connect(**kwargs)
    assert aq == []


def test_local_refuses_a_postgres_data_dir(aq, tmp_path):
    (tmp_path / CONFIG_NAME).write_text("")
    with pytest.raises(ValueError, match="Postgres-backed"):
        connect(data_dir=tmp_path)


def test_postgres_refuses_a_duckdb_data_dir(aq, tmp_path):
    (tmp_path / "timeseries.duckdb").write_text("")
    with pytest.raises(ValueError, match="DuckDB"):
        connect(db=DB, data_dir=tmp_path)


def test_close_shuts_acquirium_down(aq):
    Connection("local", "x", "client", owned=True).close()
    assert aq == ["shutdown"]


# Credentials


def test_credentials_from_a_secret_file_win(tmp_path):
    secret = tmp_path / "pw"
    secret.write_text(SECRET + "\n")
    creds = credentials_from_env(
        {
            "FLEXORCH_DB_PASSWORD_FILE": str(secret),
            "FLEXORCH_DB_PASSWORD": "other",
            "FLEXORCH_DB_USER": "flex",
        }
    )
    assert creds.user == "flex"
    assert creds.password.get_secret_value() == SECRET


def test_credentials_from_env_variables():
    creds = credentials_from_env({"FLEXORCH_DB_PASSWORD": SECRET})
    assert creds.user is None
    assert creds.password.get_secret_value() == SECRET


def test_no_credentials_leaves_them_to_libpq():
    assert credentials_from_env({}) == DBCredentials()


def test_an_unreadable_secret_file(tmp_path):
    with pytest.raises(StorageError, match="Can't read FLEXORCH_DB_PASSWORD_FILE"):
        credentials_from_env({"FLEXORCH_DB_PASSWORD_FILE": str(tmp_path / "no")})


def test_credentials_keep_the_password_secret():
    creds = DBCredentials("u", SECRET)
    assert isinstance(creds.password, SecretStr)
    assert SECRET not in repr(creds)


# DSN


def test_a_password_in_db_is_refused():
    with pytest.raises(ValueError, match="Don't put the password") as info:
        postgres_dsn(f"postgresql://u:{quote(SECRET, safe='')}@h/db")
    assert "s3cr" not in str(info.value)
    with pytest.raises(ValueError, match="Don't put the password"):
        postgres_dsn("host=h password=x")


def test_an_invalid_db():
    with pytest.raises(ValueError, match="isn't a valid"):
        postgres_dsn("not a dsn")


def test_the_user_is_escaped_into_the_dsn():
    dsn = postgres_dsn(DB, "fl ex@'x")
    assert conninfo_to_dict(dsn) == {
        "host": "db-host",
        "port": "5433",
        "dbname": "tenant_a",
        "user": "fl ex@'x",
    }


def test_redact(monkeypatch):
    monkeypatch.delenv("PGUSER", raising=False)
    assert redact(postgres_dsn(DB, "flex")) == "flex@db-host:5433/tenant_a"
    assert redact("dbname=d") == "@localhost:5432/d"


# Checks before starting


def test_preflight_passes_and_binds_the_data_dir(postgres, tmp_path):
    preflight(postgres_dsn(DB), SecretStr(SECRET), tmp_path)
    assert postgres.kwargs["password"] == SECRET
    assert postgres.bound.endswith(str(tmp_path))
    preflight(postgres_dsn(DB), None, tmp_path)  # the same data_dir again
    assert "password" not in postgres.kwargs


def test_a_second_managed_server_is_refused(postgres, tmp_path):
    preflight(postgres_dsn(DB), None, tmp_path / "a")
    with pytest.raises(StorageError, match="belongs to the managed Acquirium"):
        preflight(postgres_dsn(DB), None, tmp_path / "b")


def test_no_timescaledb(postgres, tmp_path):
    postgres.timescale = False
    with pytest.raises(StorageError, match="timescaledb"):
        preflight(postgres_dsn(DB), None, tmp_path)


def test_no_create_privilege(postgres, tmp_path):
    postgres.create = False
    with pytest.raises(StorageError, match="can't create tables"):
        preflight(postgres_dsn(DB), None, tmp_path)


def refuse(message):
    def connect(dsn, **kwargs):
        raise psycopg.OperationalError(message)

    return connect


def test_a_wrong_password_is_a_db_auth_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        psycopg,
        "connect",
        refuse(f'FATAL: password authentication failed for user "u" ({SECRET})'),
    )
    with pytest.raises(DBAuthError, match="refused the login for") as info:
        preflight(postgres_dsn(DB, "u"), SecretStr(SECRET), tmp_path)
    assert SECRET not in str(info.value)
    assert info.value.__suppress_context__


def test_an_unreachable_server(monkeypatch, tmp_path):
    monkeypatch.setattr(psycopg, "connect", refuse("could not connect to server"))
    with pytest.raises(StorageError, match="Can't reach Postgres at"):
        preflight(postgres_dsn(DB), None, tmp_path)


# Mode 2 end to end, with the secret checked everywhere


def test_postgres_mode_keeps_the_password_out_of_everything(
    aq, postgres, clean_env, monkeypatch, tmp_path
):
    secret_file = tmp_path / "pw"
    secret_file.write_text(SECRET)
    monkeypatch.setenv("FLEXORCH_DB_PASSWORD_FILE", str(secret_file))
    monkeypatch.setenv("FLEXORCH_DB_USER", "flex")
    monkeypatch.setenv("PG_DSN", "before")
    data_dir = tmp_path / "acq"

    conn = connect(db=DB, data_dir=data_dir)

    assert (conn.mode, conn.target) == ("postgres", "flex@db-host:5433/tenant_a")
    (call,) = aq
    # The server got the DSN and the password only through its environment.
    assert call["config"] == data_dir / CONFIG_NAME
    assert call["env"]["PGPASSWORD"] == SECRET
    assert SECRET not in call["env"]["PG_DSN"]
    assert conninfo_to_dict(call["env"]["PG_DSN"])["user"] == "flex"
    # ... and this process's environment is back as it was.
    assert os.environ["PG_DSN"] == "before"
    assert "PGPASSWORD" not in os.environ
    config = (data_dir / CONFIG_NAME).read_text()
    assert 'timeseries_backend = "timescale"' in config
    assert "recreate = false" in config
    assert "pg_dsn" not in config
    assert SECRET not in config
    assert SECRET not in repr(conn) + str(conn.metadata())
    assert not any(SECRET in p.read_text() for p in Path(data_dir).rglob("*.toml"))


def test_postgres_mode_with_explicit_credentials(aq, postgres, clean_env, tmp_path):
    connect(db=DB, data_dir=tmp_path, db_credentials=DBCredentials("dev", "pw"))
    assert aq[0]["env"] == {
        "PG_DSN": postgres_dsn(DB, "dev"),
        "PGPASSWORD": "pw",
    }
    assert "PG_DSN" not in os.environ


def test_postgres_mode_without_a_password(aq, postgres, clean_env, tmp_path):
    connect(db=DB, data_dir=tmp_path)
    assert aq[0]["env"]["PGPASSWORD"] is None


def test_a_server_that_does_not_start(postgres, clean_env, monkeypatch, tmp_path):
    acquirium = pytest.importorskip("acquirium")

    def init(**kwargs):
        raise RuntimeError(f"Local Acquirium server exited ({SECRET})")

    monkeypatch.setattr(acquirium, "init", init)
    with pytest.raises(StorageError, match="didn't start") as info:
        connect(db=DB, data_dir=tmp_path, db_credentials=DBCredentials("u", SECRET))
    assert SECRET not in str(info.value)
    assert "PGPASSWORD" not in os.environ


def test_the_config_escapes_the_data_dir(tmp_path):
    odd = tmp_path / "it's a dir"  # and Windows backslashes
    path = connection.write_config(odd, exact_only=False)
    import tomllib

    server = tomllib.loads(path.read_text())["server"]
    assert server["data_dir"] == str(odd)
    assert server["exact_only"] is False


# The orchestrator


class FakeConnection:
    mode = "local"
    target = "/srv/acq"

    def __init__(self):
        self.closed = False

    def backend(self):
        return MemoryBackend()

    def metadata(self):
        return {"mode": self.mode, "target": self.target}

    def close(self):
        self.closed = True


def test_the_orchestrator_connects_without_a_backend(monkeypatch):
    fake = FakeConnection()
    seen = {}

    def fake_connect(**kwargs):
        seen.update(kwargs)
        return fake

    monkeypatch.setattr("flex_orchestrator.orchestrator.connect", fake_connect)
    with Orchestrator(db=DB, data_dir="/srv/acq") as orch:
        assert orch.connection is fake
        assert isinstance(orch.backend, MemoryBackend)
    assert fake.closed
    assert seen == {
        "data_dir": "/srv/acq",
        "db": DB,
        "acquirium_url": None,
        "db_credentials": None,
        "environ": None,
    }


def test_a_tenant_stack_points_the_orchestrator_at_its_acquirium(monkeypatch):
    # D16: the stack sets FLEXORCH_ACQUIRIUM_URL; Orchestrator() then uses mode 3.
    seen = []

    class Fake:
        def backend(self):
            return MemoryBackend()

        def close(self):
            pass

    def fake_connect(**kwargs):
        seen.append(kwargs["acquirium_url"])
        return Fake()

    monkeypatch.setattr("flex_orchestrator.orchestrator.connect", fake_connect)
    stack = {"FLEXORCH_ACQUIRIUM_URL": "http://acquirium:8000"}
    Orchestrator(environ=stack)
    Orchestrator(environ={})
    Orchestrator(data_dir="here", environ=stack)  # an explicit store wins
    assert seen == ["http://acquirium:8000", None, None]


def test_a_backend_and_connection_arguments_conflict():
    with pytest.raises(ValueError, match="either a backend or connection"):
        Orchestrator(MemoryBackend(), db=DB)


def test_close_leaves_a_given_backend_alone():
    Orchestrator(MemoryBackend()).close()


def test_metadata_leaves_out_what_it_cannot_find(monkeypatch):
    import getpass

    from flex_orchestrator import orchestrator

    def no_user():
        raise OSError("no user")

    monkeypatch.delenv("FLEXORCH_USER", raising=False)
    monkeypatch.setattr(getpass, "getuser", no_user)
    assert orchestrator._user() is None
    assert orchestrator._version_of("no-such-distribution") is None
