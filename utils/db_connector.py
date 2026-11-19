"""
db_connector.py

Read-only connector for one database, picked by environment and kind.
The kind decides the engine:

    SQL         -> SQL Server  (the application database)
    CONNECTOR   -> PostgreSQL  (the connector database)

Environment variables, for each <ENV> (FYNANCIAL, SANCTUARY, ...) and kind:

    <ENV>_SQL_DB_HOST        <ENV>_CONNECTOR_DB_HOST
    <ENV>_SQL_DB_NAME        <ENV>_CONNECTOR_DB_NAME
    <ENV>_SQL_DB_USER        <ENV>_CONNECTOR_DB_USER
    <ENV>_SQL_DB_PASSWORD    <ENV>_CONNECTOR_DB_PASSWORD

Optional, same prefix:
    _PORT      default 1433 (SQL) / 5432 (CONNECTOR)
    _DRIVER    SQL only, default "ODBC Driver 18 for SQL Server"
    _SSLMODE   CONNECTOR only, default "require"
               (use "disable" if the server has no SSL, "verify-full" to check certs)

Usage:
    from db_connector import DBConnector

    app = DBConnector("FYNANCIAL", "SQL")             # kind defaults to "SQL"
    conn_db = DBConnector("FYNANCIAL", "CONNECTOR")
    conn_db.test()                                    # connectivity + permission check

    rows = conn_db.fetch_all("SELECT 1 AS x")                      # list of dicts
    rows = app.fetch_all("SELECT * FROM t WHERE id = ?", [5])       # SQL Server placeholder: ?
    rows = conn_db.fetch_all("SELECT * FROM t WHERE id = %s", [5])  # Postgres placeholder: %s

    with conn_db.connection() as c:                   # raw driver connection, always closed
        ...

CLI test:
    python db_connector.py FYNANCIAL SQL
    python db_connector.py FYNANCIAL CONNECTOR
    python db_connector.py --all          # tests every <ENV>_<KIND>_DB_HOST that is set

Read-only protections (strongest first):
  1. DATABASE LOGIN: use a login that only has read permissions. This is the only
     layer that works for every engine. test() warns if the login can write.
  2. Postgres: every session is a READ ONLY transaction, so the SERVER itself
     refuses writes. (SQL Server has no equivalent switch.)
  3. Never commits: every connection is rolled back and closed on exit.
  4. Statement guard: only a single SELECT / WITH...SELECT is accepted, and
     write/DDL/EXEC keywords are rejected. This is a safety net against
     mistakes, NOT a security boundary.
  5. Query timeout, so a runaway query can't hold things up.

Requires: uv add python-dotenv
          uv add pyodbc               (SQL; also needs "ODBC Driver 18 for SQL Server")
          uv add "psycopg[binary]"    (CONNECTOR)
Each driver is imported only when a database that uses it is connected to.
"""
from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from dotenv import load_dotenv

MSSQL = "mssql"
POSTGRES = "postgres"
KINDS = {"SQL": MSSQL, "CONNECTOR": POSTGRES}   # kind -> engine
DEFAULT_KIND = "SQL"
DEFAULT_PORTS = {MSSQL: 1433, POSTGRES: 5432}
DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"
DEFAULT_SSLMODE = "require"
REQUIRED_FIELDS = ("HOST", "NAME", "USER", "PASSWORD")


# --------------------------------------------------------------------------
# Read-only statement guard
# --------------------------------------------------------------------------
class ReadOnlyViolation(Exception):
    """Raised when a statement is not a plain read-only query."""


_COMMON_BLOCKED = (
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE",
    "DROP", "ALTER", "CREATE", "INTO",
    "EXEC", "EXECUTE", "GRANT", "REVOKE",
)
_ENGINE_BLOCKED = {
    MSSQL: (
        "DENY", "BACKUP", "RESTORE", "SHUTDOWN", "KILL", "DBCC", "RECONFIGURE",
        "OPENROWSET", "OPENQUERY", "OPENDATASOURCE", "BULK",
    ),
    # SELECT-able functions with side effects. Plain writes are already caught above
    # and, in Postgres, refused by the read-only transaction as well.
    POSTGRES: (
        "PG_TERMINATE_BACKEND", "PG_CANCEL_BACKEND", "PG_RELOAD_CONF", "SET_CONFIG",
        "LO_IMPORT", "LO_EXPORT", "LO_UNLINK", "LO_CREAT", "LO_PUT", r"DBLINK\w*",
    ),
}
_BLOCKED_RE = {
    engine: re.compile(r"\b(" + "|".join(_COMMON_BLOCKED + extra) + r")\b", re.IGNORECASE)
    for engine, extra in _ENGINE_BLOCKED.items()
}

# Comments, string literals and quoted identifiers are removed in one left-to-right
# pass, so keywords inside them don't cause false positives.
_IGNORABLE_RE = {
    # SQL Server also has [bracketed] identifiers.
    MSSQL: re.compile(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'|\[[^\]]*\]|\"[^\"]*\"", re.DOTALL),
    # Postgres: no [brackets] (those are array subscripts), but E'...' strings allow \' escapes.
    POSTGRES: re.compile(
        r"--[^\n]*|/\*.*?\*/|(?<![A-Za-z0-9_])[eE]'(?:[^'\\]|\\.|'')*'|'(?:[^']|'')*'|\"[^\"]*\"",
        re.DOTALL,
    ),
}


def assert_read_only(sql: str, dialect: str = MSSQL) -> None:
    """Raise ReadOnlyViolation unless sql is a single SELECT / WITH query."""
    if dialect not in _IGNORABLE_RE:
        raise ValueError(f"Unknown SQL dialect: {dialect!r}")

    cleaned = _IGNORABLE_RE[dialect].sub(" ", sql).strip().rstrip(";").strip()

    if not cleaned:
        raise ReadOnlyViolation("Empty statement.")
    if ";" in cleaned:
        raise ReadOnlyViolation("Only a single statement is allowed.")
    if not re.match(r"(select|with)\b", cleaned, re.IGNORECASE):
        raise ReadOnlyViolation("Statement must start with SELECT or WITH.")

    match = _BLOCKED_RE[dialect].search(cleaned)
    if match:
        raise ReadOnlyViolation(f"Blocked keyword in query: {match.group(1).upper()}")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class DBConfig:
    env: str
    kind: str
    host: str
    database: str
    user: str
    password: str = field(repr=False)  # keep out of logs/tracebacks
    engine: str = MSSQL
    port: int = 1433
    driver: str = DEFAULT_DRIVER        # SQL (mssql) only
    sslmode: str = DEFAULT_SSLMODE      # CONNECTOR (postgres) only
    trust_server_certificate: bool = True  # SQL (mssql) only
    login_timeout: int = 30
    query_timeout: int = 60

    @property
    def label(self) -> str:
        return f"{self.env}/{self.kind}"

    @classmethod
    def from_env(cls, env: str | None = None, kind: str = DEFAULT_KIND) -> "DBConfig":
        """Read <ENV>_<KIND>_DB_* variables. Falls back to ENVIRONMENT if env is None."""
        env = (env or os.environ.get("ENVIRONMENT") or "").strip().upper()
        if not env:
            raise ValueError("No environment given and ENVIRONMENT is not set.")

        kind = (kind or DEFAULT_KIND).strip().upper()
        engine = KINDS.get(kind)
        if engine is None:
            raise ValueError(f"Unknown database kind {kind!r}. Use one of: {', '.join(KINDS)}.")

        prefix = f"{env}_{kind}_DB_"
        values = {f: os.environ.get(prefix + f, "").strip() for f in REQUIRED_FIELDS}
        missing = [prefix + f for f, v in values.items() if not v]
        if missing:
            raise ValueError(f"Missing or empty environment variables: {', '.join(missing)}")

        return cls(
            env=env,
            kind=kind,
            host=values["HOST"],
            database=values["NAME"],
            user=values["USER"],
            password=values["PASSWORD"],
            engine=engine,
            port=int(os.environ.get(prefix + "PORT") or DEFAULT_PORTS[engine]),
            driver=os.environ.get(prefix + "DRIVER", DEFAULT_DRIVER),
            sslmode=os.environ.get(prefix + "SSLMODE", DEFAULT_SSLMODE),
        )

    @property
    def connection_string(self) -> str:
        """ODBC connection string (SQL Server only; Postgres uses keyword arguments)."""
        if self.engine != MSSQL:
            raise ValueError("connection_string only applies to SQL (SQL Server) databases.")
        # If the host value already includes a port ("host,1433"), don't add another.
        server = self.host if "," in self.host else f"{self.host},{self.port}"
        return (
            f"DRIVER={{{self.driver}}};"
            f"SERVER={server};"
            f"DATABASE={self.database};"
            f"UID={self.user};PWD={self.password};"
            f"Encrypt=yes;"
            f"TrustServerCertificate={'yes' if self.trust_server_certificate else 'no'};"
        )


_HOST_VAR_RE = re.compile(r"^(?P<env>.+)_(?P<kind>SQL|CONNECTOR)_DB_HOST$")


def discover() -> list[tuple[str, str]]:
    """Find every (ENV, KIND) that has a non-empty <ENV>_<KIND>_DB_HOST variable."""
    found = []
    for key, value in os.environ.items():
        match = _HOST_VAR_RE.match(key)
        if match and value.strip():
            found.append((match["env"], match["kind"]))
    return sorted(found)


# --------------------------------------------------------------------------
# Connector
# --------------------------------------------------------------------------
_MSSQL_PERMISSION_SQL = """
SELECT
    ISNULL(IS_SRVROLEMEMBER('sysadmin'), 0)                        AS is_sysadmin,
    ISNULL(IS_ROLEMEMBER('db_owner'), 0)                           AS is_db_owner,
    ISNULL(IS_ROLEMEMBER('db_datawriter'), 0)                      AS is_datawriter,
    ISNULL(IS_ROLEMEMBER('db_ddladmin'), 0)                        AS is_ddladmin,
    ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT'), 0)  AS can_insert,
    ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'UPDATE'), 0)  AS can_update,
    ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'DELETE'), 0)  AS can_delete,
    ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER'), 0)   AS can_alter
"""

_POSTGRES_PERMISSION_SQL = """
SELECT
    r.rolsuper                                                         AS is_superuser,
    has_database_privilege(current_database(), 'CREATE')               AS can_create_in_db,
    EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND (   has_table_privilege(c.oid, 'INSERT')
               OR has_table_privilege(c.oid, 'UPDATE')
               OR has_table_privilege(c.oid, 'DELETE')
               OR has_table_privilege(c.oid, 'TRUNCATE'))
    )                                                                  AS can_write_tables
FROM pg_roles r
WHERE r.rolname = current_user
"""

_MSSQL_IDENTITY_SQL = (
    "SELECT @@SERVERNAME AS server_name, DB_NAME() AS database_name, "
    "SUSER_SNAME() AS login_name, GETUTCDATE() AS utc_now"
)
_POSTGRES_IDENTITY_SQL = (
    "SELECT current_database() AS database_name, current_user AS login_name, "
    "current_setting('server_version') AS server_version, "
    "now() AT TIME ZONE 'utc' AS utc_now"
)


class DBConnector:
    """Creates short-lived, read-only-by-design connections for one database."""

    def __init__(self, env: str | None = None, kind: str = DEFAULT_KIND, *, load_env_file: bool = True):
        if load_env_file:
            load_dotenv()  # no-op if there's no .env (e.g. in GitHub Actions)
        self.config = DBConfig.from_env(env, kind)

    # -- opening connections -------------------------------------------------
    def _open_mssql(self) -> Any:
        import pyodbc  # imported lazily so Postgres-only setups don't need it

        cfg = self.config
        conn = pyodbc.connect(cfg.connection_string, timeout=cfg.login_timeout)
        conn.timeout = cfg.query_timeout  # per-query timeout (seconds)
        return conn

    def _open_postgres(self) -> Any:
        import psycopg  # imported lazily so SQL Server-only setups don't need it

        cfg = self.config
        conn = psycopg.connect(
            host=cfg.host,
            port=cfg.port,
            dbname=cfg.database,
            user=cfg.user,
            password=cfg.password,
            sslmode=cfg.sslmode,
            connect_timeout=cfg.login_timeout,
        )
        try:
            conn.read_only = True  # every transaction is BEGIN READ ONLY: the server refuses writes
            conn.execute(f"SET statement_timeout = {int(cfg.query_timeout) * 1000}")
        except Exception:
            conn.close()
            raise
        return conn

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """Open a connection that is ALWAYS rolled back and closed on exit.

        Nothing is ever committed. Use this instead of connecting with the driver directly.
        """
        conn = self._open_postgres() if self.config.engine == POSTGRES else self._open_mssql()
        try:
            yield conn
        finally:
            try:
                conn.rollback()
            finally:
                conn.close()

    # -- running queries -----------------------------------------------------
    def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Run a read-only query and return rows as a list of dicts."""
        assert_read_only(sql, self.config.engine)
        with self.connection() as conn:
            if self.config.engine == POSTGRES:
                from psycopg.rows import dict_row

                with conn.cursor(row_factory=dict_row) as cur:
                    # params=None keeps any literal % in the SQL (e.g. LIKE '%x%') untouched
                    cur.execute(sql, list(params) if params else None)
                    return list(cur.fetchall())

            cur = conn.cursor()
            cur.execute(sql, *params)
            columns = [c[0] for c in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def check_permissions(self) -> dict[str, bool]:
        """Report whether this login has any write-capable permissions."""
        sql = _POSTGRES_PERMISSION_SQL if self.config.engine == POSTGRES else _MSSQL_PERMISSION_SQL
        row = self.fetch_all(sql)[0]
        return {key: bool(value) for key, value in row.items()}

    def test(self) -> dict[str, Any]:
        """Verify connectivity, then warn if the login can write."""
        cfg = self.config
        print(
            f"[{cfg.label}] connecting to {cfg.host}:{cfg.port} / {cfg.database} "
            f"as {cfg.user} ({cfg.engine}) ..."
        )
        identity_sql = _POSTGRES_IDENTITY_SQL if cfg.engine == POSTGRES else _MSSQL_IDENTITY_SQL
        row = self.fetch_all(identity_sql)[0]
        print(f"[{cfg.label}] OK: {row}")

        risky = [name for name, has in self.check_permissions().items() if has]
        if risky:
            print(
                f"[{cfg.label}] WARNING: this login is NOT read-only "
                f"({', '.join(risky)}). Use a read-only login."
            )
        else:
            print(f"[{cfg.label}] Permissions look read-only.")
        return row


def main(argv: list[str]) -> int:
    load_dotenv()
    args = [a for a in argv if not a.startswith("--")]

    if "--all" in argv:
        targets: list[tuple[str | None, str]] = list(discover())
        if not targets:
            print("No <ENV>_<SQL|CONNECTOR>_DB_HOST variables are set.")
            return 1
    else:
        targets = [(args[0] if args else None, args[1] if len(args) > 1 else DEFAULT_KIND)]

    failed = 0
    for env, kind in targets:
        try:
            DBConnector(env, kind, load_env_file=False).test()
        except Exception as exc:  # noqa: BLE001 - this is a CLI smoke test
            failed += 1
            print(f"[{env or '?'}/{kind}] Connection test failed: {type(exc).__name__}: {exc}")
    if len(targets) > 1:
        print(f"\n{len(targets) - failed} of {len(targets)} database(s) OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))