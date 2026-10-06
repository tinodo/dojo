"""Small, isolated Azure SQL exercises. SQL is checked in the database, not from a screenshot.

Each learner has their own schema per lab, owned by its own user without login, and their SQL runs as
their own runner (ADR 0003, ADR 0009 "Labs"). SQL Server, not app code, keeps learners apart. Schema and
user names come from a random lab surrogate, never from a learner key, tid or oid. The owner keeps
today's schema names (lab_vector, lab_search, lab_hybrid) and his tables; only his runners are new.
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from .core import UserError, sha256

log = logging.getLogger("dojo.labs")

VECTOR_SOURCE = "https://learn.microsoft.com/en-us/sql/t-sql/data-types/vector-data-type?view=azuresqldb-current"
DISTANCE_SOURCE = "https://learn.microsoft.com/en-us/sql/t-sql/functions/vector-distance-transact-sql?view=azuresqldb-current"
JSON_SOURCE = "https://learn.microsoft.com/en-us/sql/t-sql/functions/json-value-transact-sql?view=azuresqldb-current"

# Lab text uses unqualified names: the runner's default schema decides where they go. The verify and
# setup SQL are templates; "{schema}" is replaced with the learner's schema name, which comes from the
# membership row and this table, never from a request.
LABS = {
    "vector": {
        "title": "Make a vector product table", "skill": "d3.g2.s3", "schema": "lab_vector",
        "goal": "Create a Products table with Id int, Name nvarchar(80), and Embedding vector(3). Add two products with different vectors.",
        "why": "DP-800 asks you to design vector data, including its type and size.",
        "steps": ["Create a table in this lab's schema.", "Add a VECTOR(3) column.", "Insert at least two named products with distinct three-number vectors.", "Check the table in the database."],
        "starter": "CREATE TABLE Products (Id int PRIMARY KEY, Name nvarchar(80) NOT NULL, Embedding vector(3) NOT NULL);",
        "hints": ["A vector literal looks like '[1,0,0]'.", "Use INSERT INTO Products (Id, Name, Embedding) VALUES (1, N'Shoe', '[1,0,0]'), (2, N'Boot', '[0,1,0]');"],
        "sources": [{"title": "Vector data type", "url": VECTOR_SOURCE}],
        "setup": (),
        "verify": """SELECT CASE WHEN EXISTS (
            SELECT 1 FROM sys.columns c JOIN sys.tables t ON c.object_id=t.object_id
            JOIN sys.schemas s ON t.schema_id=s.schema_id
            WHERE s.name='{schema}' AND t.name='Products' AND c.name='Embedding'
              AND TYPE_NAME(c.user_type_id)='vector' AND c.vector_dimensions=3
        ) AND OBJECT_ID(N'{schema}.Products', N'U') IS NOT NULL
        AND (SELECT COUNT(*) FROM {schema}.Products) >= 2
        AND (SELECT COUNT(DISTINCT CAST(Embedding AS nvarchar(200))) FROM {schema}.Products) >= 2
        THEN 1 ELSE 0 END AS passed""",
    },
    "search": {
        "title": "Find the nearest product", "skill": "d3.g2.s7", "schema": "lab_search",
        "goal": "Save the two closest products to the query vector '[1,0,0]' in a table named Answers. Keep Id and distance columns.",
        "why": "DP-800 covers exact KNN search with VECTOR_DISTANCE. These vectors are tiny and hand-made.",
        "steps": ["Look at the Products table.", "Use VECTOR_DISTANCE with the cosine metric.", "Order by distance and select TOP (2) into Answers.", "Check the order and distances in the database."],
        "starter": "SELECT Id, Name, VECTOR_DISTANCE('cosine', Embedding, CAST('[1,0,0]' AS vector(3))) AS distance FROM Products;",
        "hints": ["Use SELECT TOP (2) ... INTO Answers FROM Products ORDER BY distance.", "The closest product is Shoe, Id 1. Do not just copy its Id; save distances computed by SQL."],
        "sources": [{"title": "VECTOR_DISTANCE", "url": DISTANCE_SOURCE}],
        "setup": ("""CREATE TABLE {schema}.Products (Id int PRIMARY KEY, Name nvarchar(80), Embedding vector(3));
            INSERT INTO {schema}.Products VALUES (1, N'Shoe', '[1,0,0]'), (2, N'Trainer', '[0.8,0.6,0]'), (3, N'Jacket', '[0,1,0]')""",),
        "verify": """SELECT CASE WHEN OBJECT_ID(N'{schema}.Answers', N'U') IS NOT NULL
        AND (SELECT COUNT(*) FROM {schema}.Answers) = 2
        AND (SELECT COUNT(DISTINCT Id) FROM {schema}.Answers) = 2
        AND NOT EXISTS (
            SELECT Id FROM {schema}.Answers EXCEPT SELECT Id FROM
            (SELECT TOP (2) Id FROM {schema}.Products
             ORDER BY VECTOR_DISTANCE('cosine', Embedding, CAST('[1,0,0]' AS vector(3)))) AS closest
        )
        AND NOT EXISTS (
            SELECT 1 FROM {schema}.Answers a JOIN {schema}.Products p ON p.Id=a.Id
            WHERE ABS(a.distance - VECTOR_DISTANCE('cosine', p.Embedding, CAST('[1,0,0]' AS vector(3)))) > 0.001
        ) THEN 1 ELSE 0 END AS passed""",
    },
    "hybrid": {
        "title": "Filter semantic product search", "skill": "d3.g2.s8", "schema": "lab_hybrid",
        "goal": "Find the nearest in-stock shoes for '[1,0,0]'. Save Id and distance in a table named Picks. Use the JSON category and stock filter.",
        "why": "DP-800 covers hybrid search: combine structured or JSON filters with vector similarity.",
        "steps": ["Read the JSON metadata in the Products table.", "Filter JSON_VALUE(Metadata, '$.category') to shoes and stock to 1.", "Order by VECTOR_DISTANCE and save TOP (2) in Picks.", "Check the result in the database."],
        "starter": "SELECT Id, Metadata, VECTOR_DISTANCE('cosine', Embedding, CAST('[1,0,0]' AS vector(3))) AS distance FROM Products;",
        "hints": ["JSON_VALUE(Metadata, '$.category') = N'shoes' and JSON_VALUE(Metadata, '$.stock') = N'1'.", "Use SELECT TOP (2) Id, ... AS distance INTO Picks; the correct Ids are 2 and 3."],
        "sources": [{"title": "JSON_VALUE", "url": JSON_SOURCE}, {"title": "VECTOR_DISTANCE", "url": DISTANCE_SOURCE}],
        "setup": ("""CREATE TABLE {schema}.Products (Id int PRIMARY KEY, Metadata nvarchar(200), Embedding vector(3));
            INSERT INTO {schema}.Products VALUES
            (1, N'{"category":"shoes","stock":0}', '[1,0,0]'),
            (2, N'{"category":"shoes","stock":1}', '[0.9,0.1,0]'),
            (3, N'{"category":"shoes","stock":1}', '[0.7,0.3,0]'),
            (4, N'{"category":"jacket","stock":1}', '[0.95,0.05,0]')""",),
        "verify": """SELECT CASE WHEN OBJECT_ID(N'{schema}.Picks', N'U') IS NOT NULL
        AND (SELECT COUNT(*) FROM {schema}.Picks) = 2
        AND (SELECT COUNT(DISTINCT Id) FROM {schema}.Picks) = 2
        AND NOT EXISTS (SELECT Id FROM {schema}.Picks EXCEPT SELECT Id FROM {schema}.Products WHERE Id IN (2,3))
        AND NOT EXISTS (
            SELECT 1 FROM {schema}.Picks a JOIN {schema}.Products p ON p.Id=a.Id
            WHERE ABS(a.distance - VECTOR_DISTANCE('cosine', p.Embedding, CAST('[1,0,0]' AS vector(3)))) > 0.001
        ) THEN 1 ELSE 0 END AS passed""",
    },
}
MAX_SQL = 8000
MAX_ROWS = 200
MAX_BATCH = 20
TIMEOUT = 20

SURROGATE = re.compile(r"[0-9a-f]{12}")
LAB_NAMES = "|".join(LABS)
RUNNER = re.compile(rf"lr_([0-9a-f]{{12}})_({LAB_NAMES})")
MEMBER_SCHEMA = re.compile(rf"lab_({LAB_NAMES})_([0-9a-f]{{12}})")
LEGACY_SCHEMAS = tuple(spec["schema"] for spec in LABS.values())


def template(sql: str, schema: str) -> str:
    """Put the learner's schema into lab SQL. Plain replacement: the JSON fixtures contain braces."""
    return sql.replace("{schema}", schema)


def valid_schema(schema: str) -> bool:
    return schema in LEGACY_SCHEMAS or bool(MEMBER_SCHEMA.fullmatch(schema))


@dataclass(frozen=True)
class LabUser:
    """Whose lab schemas an operation uses. `legacy` is the owner: today's schema names, his own runners."""
    surrogate: str
    legacy: bool = False

    def __post_init__(self):
        if not SURROGATE.fullmatch(self.surrogate or ""):
            raise ValueError("A lab surrogate is 12 lowercase hex characters.")

    def schema(self, lab: str) -> str:
        return LABS[lab]["schema"] if self.legacy else f"lab_{lab}_{self.surrogate}"

    def names(self, lab: str) -> tuple[str, str, str]:
        """(schema, schema owner, runner) for one lab."""
        schema = self.schema(lab)
        return schema, f"{schema}_owner", f"lr_{self.surrogate}_{lab}"


def new_surrogate() -> str:
    return secrets.token_hex(6)


def is_waking(exc: Exception) -> bool:
    return bool(re.search(r"is not currently available|retry the connection later", str(exc), re.I))


def is_query_timeout(exc: Exception) -> bool:
    return getattr(exc, "driver_error", None) == "Timeout expired"


def sql_error_reason(exc: Exception) -> str:
    if is_waking(exc):
        return "database waking"
    if is_query_timeout(exc):
        return "query timeout"
    state = getattr(exc, "sqlstate", None)
    if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state):
        return f"SQLSTATE {state}"
    label = getattr(exc, "driver_error", None)
    if label in ("General error", "Syntax error or access violation", "Column not found",
                 "Client unable to establish connection", "Connection operation failed"):
        return label
    return "database error"


def validate(sql: str) -> str:
    if not sql.strip() or len(sql.encode("utf-8")) > MAX_SQL:
        raise UserError("Enter SQL up to 8 KB.")
    if re.search(r"(?im)^\s*GO(?:\s+\d+)?\s*$", sql) or sql.count(";") > MAX_BATCH:
        raise UserError("Use one SQL batch, up to 20 statements. GO is not supported.")
    return sql


class SqlRunner:
    """Runs lab SQL in the private Azure SQL database as the app's identity. Each learner's SQL runs as
    their own database user in their own schema, so SQL Server keeps learners apart."""
    def __init__(self, server: str, database: str, credential: Any):
        if not re.fullmatch(r"[a-z0-9-]+\.database\.windows\.net", server) or not re.fullmatch(r"[a-zA-Z0-9-]+", database):
            raise ValueError("A private Azure SQL server and database must be configured.")
        import mssql_python
        mssql_python.pooling(enabled=False)
        self.driver = mssql_python
        self.connection_string = f"Server={server};Database={database};Encrypt=Strict;"
        self.credential = credential

    def connect(self):
        for attempt in range(3):
            try:
                return self.driver.connect(self.connection_string, token_provider=self.credential, timeout=90)
            except Exception as exc:
                if not is_waking(exc) or attempt == 2:
                    raise
                time.sleep(attempt + 1)

    @staticmethod
    def execute(conn, sql: str):
        conn.timeout = TIMEOUT
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            while cursor.description is None and cursor.nextset():
                pass
            if cursor.description is None:
                return {"columns": [], "rows": [], "truncated": False, "message": "SQL completed."}
            columns = [c[0] for c in cursor.description]
            rows = cursor.fetchmany(MAX_ROWS + 1)
            return {"columns": columns, "rows": [[str(v) if v is not None else None for v in row] for row in rows[:MAX_ROWS]],
                    "truncated": len(rows) > MAX_ROWS, "message": "Results from the database."}
        finally:
            cursor.close()

    def admin(self, sql: str):
        with closing(self.connect()) as conn:
            result = self.execute(conn, sql)
            conn.commit()
            return result

    def fetch_all(self, statements: list[str]) -> list[list[list]]:
        """Admin statements on one connection and in one transaction, every row of each result. For Dojo's
        own small tables (the dojo_meta mirror, the catalog), not for learner SQL."""
        with closing(self.connect()) as conn:
            conn.timeout = TIMEOUT
            out = []
            for sql in statements:
                cursor = conn.cursor()
                try:
                    cursor.execute(sql)
                    while cursor.description is None and cursor.nextset():
                        pass
                    out.append([list(row) for row in cursor.fetchall()] if cursor.description is not None else [])
                finally:
                    cursor.close()
            conn.commit()
            return out

    def run(self, sql: str, runner: str):
        if not RUNNER.fullmatch(runner):
            raise ValueError("Not a lab runner.")
        with closing(self.connect()) as conn:
            conn.timeout = TIMEOUT
            # No statement supplied by the learner runs before this irreversible context change.
            cursor = conn.cursor()
            try:
                cursor.execute(f"EXECUTE AS USER = N'{runner}' WITH NO REVERT;")
            finally:
                cursor.close()
            try:
                result = self.execute(conn, sql)
            except Exception as exc:
                # SQL errors are not failed checks. No answer or result is put in the Record.
                log.warning("learner SQL failed: %s; %s", type(exc).__name__, sql_error_reason(exc))
                if is_query_timeout(exc):
                    return {"columns": [], "rows": [], "truncated": False,
                            "message": "Your query ran longer than 20 seconds.", "timed_out": True}
                raise UserError(f"SQL error: {str(exc)[:500]}") from None
            conn.commit()
            return result

    def check(self, sql: str):
        return self.admin(sql)["rows"][0][0] == "1"

    def reset(self, schema: str):
        """Empty one learner's schema for one lab, and nothing else (ADR 0009). Versioning is turned off only
        for temporal tables in this schema or with their history in it; then only this schema's objects go."""
        # The schema name comes only from LABS and the membership row, never from a request body.
        if not valid_schema(schema):
            raise ValueError("Not a lab schema.")
        self.admin(f"""DECLARE @sql nvarchar(max) = N'';
        SELECT @sql += N'ALTER TABLE ' + QUOTENAME(s.name) + N'.' + QUOTENAME(t.name)
            + N' SET (SYSTEM_VERSIONING = OFF);'
        FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id
        LEFT JOIN sys.tables h ON h.object_id=t.history_table_id
        LEFT JOIN sys.schemas hs ON hs.schema_id=h.schema_id
        WHERE t.temporal_type=2 AND (s.name='{schema}' OR hs.name='{schema}');
        EXEC sys.sp_executesql @sql;""")
        self.admin(f"""DECLARE @sql nvarchar(max) = N'';
        SELECT @sql += N'ALTER TABLE ' + QUOTENAME('{schema}') + N'.' + QUOTENAME(t.name)
            + N' DROP CONSTRAINT ' + QUOTENAME(e.name) + N';'
        FROM sys.edge_constraints e JOIN sys.tables t ON t.object_id=e.parent_object_id
        JOIN sys.schemas s ON s.schema_id=t.schema_id WHERE s.name='{schema}';
        SELECT @sql += N'ALTER TABLE ' + QUOTENAME('{schema}') + N'.' + QUOTENAME(t.name)
            + N' DROP CONSTRAINT ' + QUOTENAME(fk.name) + N';'
        FROM sys.foreign_keys fk JOIN sys.tables t ON t.object_id=fk.parent_object_id
        JOIN sys.schemas s ON s.schema_id=t.schema_id WHERE s.name='{schema}';
        SELECT @sql += N'DROP VIEW ' + QUOTENAME('{schema}') + N'.' + QUOTENAME(o.name) + N';'
        FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id
        WHERE s.name='{schema}' AND o.type='V';
        SELECT @sql += N'DROP TABLE ' + QUOTENAME('{schema}') + N'.' + QUOTENAME(o.name) + N';'
        FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id
        WHERE s.name='{schema}' AND o.type='U';
        SELECT @sql += N'DROP SEQUENCE ' + QUOTENAME('{schema}') + N'.' + QUOTENAME(o.name) + N';'
        FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id
        WHERE s.name='{schema}' AND o.type='SO';
        SELECT @sql += N'DROP SYNONYM ' + QUOTENAME('{schema}') + N'.' + QUOTENAME(o.name) + N';'
        FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id
        WHERE s.name='{schema}' AND o.type='SN';
        SELECT @sql += N'DROP PROCEDURE ' + QUOTENAME('{schema}') + N'.' + QUOTENAME(o.name) + N';'
        FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id
        WHERE s.name='{schema}' AND o.type='P';
        SELECT @sql += N'DROP FUNCTION ' + QUOTENAME('{schema}') + N'.' + QUOTENAME(o.name) + N';'
        FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id
        WHERE s.name='{schema}' AND o.type IN ('FN','IF','TF');
        EXEC sys.sp_executesql @sql;""")

    def repair(self):
        """The owner's repair from the time of one shared runner (Studio, /api/diag/lab/repair): turn off
        versioning where a history table sits in another schema or in dbo, drop those escaped history
        tables, and drop every table in dbo. It reaches the whole database, so no member operation runs it."""
        self.admin("""DECLARE @external_history TABLE (schema_name sysname, table_name sysname);
        INSERT INTO @external_history
        SELECT hs.name, h.name FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id=t.schema_id
        JOIN sys.tables h ON h.object_id=t.history_table_id
        JOIN sys.schemas hs ON hs.schema_id=h.schema_id
        WHERE t.temporal_type=2 AND hs.schema_id<>s.schema_id AND hs.name<>'dbo';
        DECLARE @sql nvarchar(max) = N'';
        SELECT @sql += N'ALTER TABLE ' + QUOTENAME(s.name) + N'.' + QUOTENAME(t.name)
            + N' SET (SYSTEM_VERSIONING = OFF);'
        FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id
        JOIN sys.tables h ON h.object_id=t.history_table_id
        JOIN sys.schemas hs ON hs.schema_id=h.schema_id
        WHERE t.temporal_type=2 AND (hs.schema_id<>s.schema_id OR hs.name='dbo' OR s.name='dbo');
        EXEC sys.sp_executesql @sql;
        SET @sql = N'';
        SELECT @sql += N'DROP TABLE ' + QUOTENAME(schema_name) + N'.' + QUOTENAME(table_name) + N';'
        FROM @external_history;
        EXEC sys.sp_executesql @sql;
        SET @sql = N'';
        SELECT @sql += N'DROP TABLE [dbo].' + QUOTENAME(t.name) + N';'
        FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id WHERE s.name='dbo';
        EXEC sys.sp_executesql @sql;""")

    def drop(self, schema: str, owner: str, runner: str):
        """A member's schema for one lab, its owner and its runner: emptied, then gone."""
        if not MEMBER_SCHEMA.fullmatch(schema) or owner != f"{schema}_owner" or not RUNNER.fullmatch(runner):
            raise ValueError("Only a member's lab schema can be dropped.")
        if self.admin(f"SELECT CASE WHEN SCHEMA_ID(N'{schema}') IS NULL THEN 0 ELSE 1 END")["rows"][0][0] == "1":
            self.reset(schema)
        self.admin(f"""IF SCHEMA_ID(N'{schema}') IS NOT NULL EXEC(N'DROP SCHEMA [{schema}]');
        IF DATABASE_PRINCIPAL_ID(N'{runner}') IS NOT NULL EXEC(N'DROP USER [{runner}]');
        IF DATABASE_PRINCIPAL_ID(N'{owner}') IS NOT NULL EXEC(N'DROP USER [{owner}]');""")


class FakeRunner:
    """Local-only preview; does not simulate SQL authorization or claim database evidence. Each learner's
    runner has its own runs, so one learner's demo never checks another's."""
    def __init__(self):
        self.runs: dict[str, list[str]] = {}

    def prepare(self, lab: str):
        return None

    def run(self, sql: str, runner: str):
        self.runs.setdefault(runner, []).append(sql)
        return {"columns": ["mode", "SQL bytes"], "rows": [["Local demo, no database", str(len(sql.encode("utf-8")))]],
                "truncated": False, "message": "Demo only. No SQL was executed."}

    def check(self, lab: str, runner: str):
        sql = "\n".join(self.runs.get(runner, [])).lower()
        # Names may be written plain or qualified with the learner's own schema.
        sql = re.sub(r"\[?lab_[a-z0-9_]+\]?\.", "", sql)
        checks = {
            "vector": ("create table products", "vector(3)", "insert into products"),
            "search": ("into answers", "vector_distance", "top", "order by"),
            "hybrid": ("into picks", "vector_distance", "json_value", "top", "order by"),
        }
        return all(part in sql for part in checks[lab])

    def reset(self, runner: str):
        self.runs.pop(runner, None)

    def forget(self, surrogate: str):
        for lab in LABS:
            self.runs.pop(f"lr_{surrogate}_{lab}", None)


class LabRoom:
    """`store` holds team/owner.json, where the owner's lab surrogate lives (ADR 0009)."""
    def __init__(self, runner: SqlRunner | FakeRunner, local: bool, store: Any = None):
        self.runner, self.local, self.store = runner, local, store
        self.lock = threading.Lock()
        self.migrated = False             # the owner's runners exist and lab_runner is retired (this process)
        self.unknown: list[str] = []      # member-style schemas no member, owner or tombstone accounts for
        self._owner: LabUser | None = None

    # ---------------------------------------------------------------- whose schemas

    def owner_user(self) -> LabUser:
        """The owner's lab surrogate, from team/owner.json. If that file is gone (an old /home restore) the
        database names it: the runner whose default schema is lab_vector. Only if there is none is a new
        one drawn. Keyed to nothing: not the tid, oid or learner key."""
        if self._owner is not None:
            return self._owner
        doc = (self.store.read("team", "owner", default=None) or {}) if self.store else {}
        s = doc.get("lab_surrogate") if isinstance(doc, dict) else None
        if not (isinstance(s, str) and SURROGATE.fullmatch(s)):
            s = (None if self.local else self._owner_runner_in_database()) or new_surrogate()
            if self.store is not None:
                with self.store.lock:
                    doc = self.store.read("team", "owner", default=None) or {}
                    if isinstance(doc.get("lab_surrogate"), str) and SURROGATE.fullmatch(doc["lab_surrogate"]):
                        s = doc["lab_surrogate"]
                    else:
                        self.store.write("team", "owner", value={**doc, "lab_surrogate": s})
        self._owner = LabUser(s, legacy=True)
        return self._owner

    def _owner_runner_in_database(self) -> str | None:
        rows = self.runner.fetch_all(["""SELECT name FROM sys.database_principals
            WHERE type='S' AND default_schema_name=N'lab_vector' AND name LIKE N'lr[_]%[_]vector' ORDER BY name"""])[0]
        for (name,) in rows:
            m = RUNNER.fullmatch(str(name))
            if m and m.group(2) == "vector":
                return m.group(1)
        return None

    def detail(self, lab: str, who: LabUser | None = None, shared: bool = False) -> dict:
        spec = LABS[lab]
        schema = spec["schema"] if who is None or who.legacy else who.schema(lab)
        return {"id": lab, **{k: v for k, v in spec.items() if k not in ("verify", "setup", "hints", "schema")},
                "schema": schema, "hint_count": len(spec["hints"]), "local": self.local, "shared": shared,
                "limits": {"seconds": TIMEOUT, "rows": MAX_ROWS, "sql_bytes": MAX_SQL}}

    # ---------------------------------------------------------------- setting up

    def migrate(self):
        """Idempotent; runs before the first lab operation of each process. The owner's legacy schemas
        and their owners as before, the history guard, a runner per legacy schema for the owner, and the
        shared lab_runner retired. No table is touched."""
        if self.migrated or self.local:
            return
        owner = self.owner_user()
        owners = "".join(
            f"""IF DATABASE_PRINCIPAL_ID(N'{spec['schema']}_owner') IS NULL
                EXEC(N'CREATE USER [{spec['schema']}_owner] WITHOUT LOGIN');
            IF SCHEMA_ID(N'{spec['schema']}') IS NULL
                EXEC(N'CREATE SCHEMA [{spec['schema']}] AUTHORIZATION [{spec['schema']}_owner]');
            ELSE IF EXISTS (SELECT 1 FROM sys.schemas WHERE name=N'{spec['schema']}'
                            AND principal_id <> DATABASE_PRINCIPAL_ID(N'{spec['schema']}_owner'))
                EXEC(N'ALTER AUTHORIZATION ON SCHEMA::[{spec['schema']}] TO [{spec['schema']}_owner]');"""
            for spec in LABS.values())
        self.runner.admin(f"""{owners}
        IF DATABASE_PRINCIPAL_ID(N'lab_guard') IS NULL EXEC(N'CREATE USER [lab_guard] WITHOUT LOGIN');
        GRANT VIEW DEFINITION TO [lab_guard];
        """)
        self.runner.admin("""EXEC(N'CREATE OR ALTER TRIGGER [dojo_lab_history_guard] ON DATABASE
            WITH EXECUTE AS ''lab_guard'' FOR CREATE_TABLE, ALTER_TABLE AS
            BEGIN
                SET NOCOUNT ON;
                DECLARE @schema sysname = EVENTDATA().value(''(/EVENT_INSTANCE/SchemaName)[1]'', ''sysname'');
                DECLARE @table sysname = EVENTDATA().value(''(/EVENT_INSTANCE/ObjectName)[1]'', ''sysname'');
                IF EXISTS (
                    SELECT 1 FROM sys.tables t
                    JOIN sys.schemas s ON s.schema_id=t.schema_id
                    JOIN sys.tables h ON h.object_id=t.history_table_id
                    WHERE t.temporal_type=2 AND t.schema_id<>h.schema_id
                    AND s.name=@schema AND t.name=@table
                )
                BEGIN
                    RAISERROR(''A history table must be in the same schema as its table.'', 16, 1);
                    ROLLBACK TRANSACTION;
                END
            END')""")
        self.runner.admin("".join(self._runner_sql(owner, lab) for lab in LABS))
        revokes = "".join(f"IF SCHEMA_ID(N'{s}') IS NOT NULL EXEC(N'REVOKE ALTER, SELECT, INSERT, UPDATE, DELETE ON SCHEMA::[{s}] FROM [lab_runner]');"
                          for s in LEGACY_SCHEMAS)
        self.runner.admin(f"""IF DATABASE_PRINCIPAL_ID(N'lab_runner') IS NOT NULL
        BEGIN
            EXEC(N'REVOKE CREATE TABLE FROM [lab_runner]');
            {revokes}
            IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE principal_id=DATABASE_PRINCIPAL_ID(N'lab_runner'))
               AND NOT EXISTS (SELECT 1 FROM sys.objects WHERE principal_id=DATABASE_PRINCIPAL_ID(N'lab_runner'))
                EXEC(N'DROP USER [lab_runner]');
        END""")
        self.migrated = True
        log.info("lab database ready: the owner's runners exist and the shared runner is retired")

    @staticmethod
    def _runner_sql(who: LabUser, lab: str) -> str:
        """One learner's schema for one lab, its owner, its runner and the runner's grants. Idempotent."""
        schema, owner, runner = who.names(lab)
        return f"""IF DATABASE_PRINCIPAL_ID(N'{owner}') IS NULL EXEC(N'CREATE USER [{owner}] WITHOUT LOGIN');
        IF SCHEMA_ID(N'{schema}') IS NULL EXEC(N'CREATE SCHEMA [{schema}] AUTHORIZATION [{owner}]');
        ELSE IF EXISTS (SELECT 1 FROM sys.schemas WHERE name=N'{schema}' AND principal_id <> DATABASE_PRINCIPAL_ID(N'{owner}'))
            EXEC(N'ALTER AUTHORIZATION ON SCHEMA::[{schema}] TO [{owner}]');
        IF DATABASE_PRINCIPAL_ID(N'{runner}') IS NULL
            EXEC(N'CREATE USER [{runner}] WITHOUT LOGIN WITH DEFAULT_SCHEMA = [{schema}]');
        ELSE IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name=N'{runner}' AND default_schema_name=N'{schema}')
            EXEC(N'ALTER USER [{runner}] WITH DEFAULT_SCHEMA = [{schema}]');
        EXEC(N'GRANT CREATE TABLE TO [{runner}]');
        EXEC(N'GRANT ALTER, SELECT, INSERT, UPDATE, DELETE ON SCHEMA::[{schema}] TO [{runner}]');
        """

    def prepare(self, lab: str, who: LabUser | None = None, seed: bool = True):
        if self.local:
            self.runner.prepare(lab)
            return
        self.migrate()
        who = who or self.owner_user()
        self.runner.admin(self._runner_sql(who, lab))
        if seed:
            self.seed(lab, who)

    def seed(self, lab: str, who: LabUser | None = None):
        schema = (who or self.owner_user()).schema(lab)
        for statement in LABS[lab]["setup"]:
            table = f"{schema}.Products"
            escaped = template(statement, schema).replace("'", "''")
            result = self.runner.admin(f"""IF OBJECT_ID(N'{table}') IS NOT NULL
                AND OBJECT_ID(N'{table}', N'U') IS NULL SELECT 1 AS wrong_type;
            ELSE BEGIN
                IF OBJECT_ID(N'{table}') IS NULL EXEC(N'{escaped}');
                SELECT 0 AS wrong_type;
            END""")
            if result["rows"][0][0] == "1":
                raise UserError("The fixture name is used by a different object. Press Reset this lab.")

    # ---------------------------------------------------------------- what a learner does

    def operation(self, lab: str, kind: str, who: LabUser | None = None, sql: str = "") -> dict:
        """`who` None is the owner. One lab operation at a time for all learners (ADR 0009)."""
        if lab not in LABS:
            raise KeyError(lab)
        if kind == "run":
            validate(sql)
        if not self.lock.acquire(blocking=False):
            raise UserError("A lab query is already running. Wait for it to finish.")
        try:
            who = who or self.owner_user()
            schema, _, runner = who.names(lab)
            self.prepare(lab, who, seed=kind != "reset")
            if kind == "run":
                return self.runner.run(sql, runner)
            if kind == "check":
                if self.local:
                    passed = self.runner.check(lab, runner)
                else:
                    target = "Products" if lab == "vector" else ("Answers" if lab == "search" else "Picks")
                    required = ("Id", "distance") if lab != "vector" else ("Embedding",)
                    columns = " OR ".join(f"COL_LENGTH(N'{schema}.{target}', '{column}') IS NULL" for column in required)
                    verify = template(LABS[lab]["verify"], schema).replace("'", "''")
                    passed = self.runner.check(
                        f"IF OBJECT_ID(N'{schema}.{target}', N'U') IS NULL OR {columns} SELECT 0 AS passed "
                        f"ELSE EXEC sys.sp_executesql N'{verify}'")
                return {"passed": passed, "message": "Local demo matched a few SQL words, not a database check." if self.local else
                        ("Verified in the database." if passed else "Not yet. Check your table or saved results.")}
            if kind == "reset":
                self.runner.reset(runner if self.local else schema)
                if not self.local:
                    self.seed(lab, who)
                return {"message": "Only this lab was reset to its starting state."}
            raise ValueError(kind)
        finally:
            self.lock.release()

    # ---------------------------------------------------------------- leaving, and the reconciler

    def forget(self, surrogate: str | None):
        """A member's local demo runs go with them. In the database, the reconciler drops their schemas."""
        if self.local and surrogate and SURROGATE.fullmatch(surrogate):
            self.runner.forget(surrogate)

    def reconcile(self, tombstoned: set[str], known: set[str]) -> dict:
        """Drop member schemas, and their users, whose surrogate is tombstoned (sha256 in `tombstoned`).
        A member-style schema nobody accounts for is reported, never dropped. The owner's legacy names
        never match the pattern, so they are never touched. Runs only when the mirror syncs, so it never
        wakes the database on its own."""
        if self.local:
            return {"dropped": 0, "unknown": []}
        if not self.lock.acquire(timeout=60):
            return {"dropped": 0, "unknown": self.unknown, "busy": True}
        try:
            self.migrate()
            schemas, users = self.runner.fetch_all([
                "SELECT name FROM sys.schemas WHERE name LIKE N'lab[_]%'",
                "SELECT name FROM sys.database_principals WHERE type='S' AND name LIKE N'lr[_]%'"])
            seen: dict[str, set[str]] = {}
            for (name,) in schemas:
                m = MEMBER_SCHEMA.fullmatch(str(name))
                if m:
                    seen.setdefault(m.group(2), set()).add(m.group(1))
            for (name,) in users:
                m = RUNNER.fullmatch(str(name))
                if m:
                    seen.setdefault(m.group(1), set())
            dropped, unknown = 0, []
            for s, labs in sorted(seen.items()):
                if sha256(s) in tombstoned:
                    who = LabUser(s)
                    for lab in LABS:
                        self.runner.drop(*who.names(lab))
                    dropped += 1
                elif s not in known:
                    unknown += [f"lab_{lab}_{s}" for lab in sorted(labs)] or [f"lr_{s}_*"]
            self.unknown = unknown
            if dropped:
                log.warning("dropped the lab schemas of %d deleted learner(s)", dropped)
            if unknown:
                log.warning("%d lab schema(s) belong to no known learner; left in place", len(unknown))
            return {"dropped": dropped, "unknown": unknown}
        finally:
            self.lock.release()

    # ---------------------------------------------------------------- the owner's checks

    def probe(self) -> dict:
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "message": "A lab query is running."}
        try:
            if self.local:
                return {"ok": True, "mode": "local demo", "database_checked": False}
            result = self.runner.admin("SELECT DB_NAME() AS database_name, USER_NAME() AS principal")
            who = self.owner_user()
            self.prepare("vector", who)
            _, _, runner = who.names("vector")
            restricted = self.runner.run(
                """SELECT USER_NAME() AS principal,
                HAS_PERMS_BY_NAME(N'lab_vector', N'SCHEMA', N'ALTER') AS allowed,
                COALESCE(HAS_PERMS_BY_NAME(N'lab_search', N'SCHEMA', N'ALTER'), 0) AS other_lab_allowed,
                COALESCE(HAS_PERMS_BY_NAME(N'lab_hybrid', N'SCHEMA', N'ALTER'), 0) AS third_lab_allowed,
                COALESCE(HAS_PERMS_BY_NAME(N'dbo', N'SCHEMA', N'ALTER'), 0) AS dbo_allowed,
                COALESCE(HAS_PERMS_BY_NAME(N'dojo_meta', N'SCHEMA', N'SELECT'), 0) AS meta_allowed""", runner)
            row = restricted["rows"][0]
            safe = row[0] == runner and row[1] == "1" and row[2] == row[3] == row[4] == row[5] == "0"
            return {"ok": safe, "mode": "private Azure SQL", "database_checked": True,
                    "database": result["rows"][0][0], "admin": result["rows"][0][1],
                    "runner": row[0], "runner_allowed": safe, "unknown_schemas": len(self.unknown)}
        finally:
            self.lock.release()

    def repair(self) -> dict:
        if self.local:
            return {"ok": True, "message": "Local demo: nothing to repair."}
        if not self.lock.acquire(timeout=30):
            raise UserError("A lab query is running. Try again in a moment.")
        try:
            self.runner.repair()
            return {"ok": True, "message": "Escaped history tables and every table in dbo were removed."}
        finally:
            self.lock.release()

