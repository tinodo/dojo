"""Lab isolation, security and local preview; never connects to Azure."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from tests.test_api import POST, settings
from app.labs import LABS, MAX_ROWS, SqlRunner, LabRoom, LabUser, FakeRunner, UserError, is_waking, sql_error_reason, validate
from app.core import Store, sha256
from app.mirror import Mirror
from app.evidence import derive
from app.exam import HELP_EVENTS
from app.main import create_app
from app.packages import Packages

ROOT = Path(__file__).resolve().parents[1]


def paused_error():
    from mssql_python.connection import _raise_connection_error

    try:
        _raise_connection_error(RuntimeError(
            "SQLSTATE:HY000:[Microsoft][SQL Server]Database 'sandbox' on server 'example' "
            "is not currently available. Please retry the connection later."))
    except Exception as exc:
        return exc
    raise AssertionError("The driver did not raise for a paused database.")


class Cursor:
    description = None

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql):
        self.conn.statements.append(sql)
        if "AS wrong_type" in sql:
            self.description = [("wrong_type",)]

    def fetchmany(self, size):
        return [("0",)]

    def fetchall(self):
        return []

    def nextset(self):
        return False

    def close(self):
        pass


class Connection:
    def __init__(self):
        self.statements = []
        self.closed = False
        self.timeout = 0

    def cursor(self):
        return Cursor(self)

    def commit(self):
        pass

    def close(self):
        self.closed = True


class SqlBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.credential = object()
        with mock.patch("mssql_python.pooling") as pool:
            self.runner = SqlRunner("dojo-sql.database.windows.net", "sqldb-lab-dojo", self.credential)
            pool.assert_called_once_with(enabled=False)
        self.connections = []

        def connect():
            conn = Connection()
            self.connections.append(conn)
            return conn

        self.runner.connect = connect

    def test_preamble_is_first_and_connection_closed_not_admin_sql(self):
        self.runner.run("CREATE TABLE lab_vector.Products (Id int)", "lr_0123456789ab_vector")
        self.assertEqual(self.connections[0].statements, [
            "EXECUTE AS USER = N'lr_0123456789ab_vector' WITH NO REVERT;", "CREATE TABLE lab_vector.Products (Id int)"])
        self.assertEqual(self.connections[0].timeout, 20)
        self.assertTrue(self.connections[0].closed)
        self.runner.admin("SELECT 1")
        self.assertEqual(self.connections[1].statements, ["SELECT 1"])
        self.assertTrue(self.connections[1].closed)
        self.runner.run("SELECT 2", "lr_0123456789ab_vector")
        self.assertEqual(self.connections[2].statements[0], "EXECUTE AS USER = N'lr_0123456789ab_vector' WITH NO REVERT;")
        self.assertNotIn("SELECT 2", self.connections[1].statements)
        # Only a learner's own runner: never the retired shared one, dbo or a made-up name.
        for name in ("lab_runner", "dbo", "lr_0123456789ab_vector'; --", "lr_0123456789AB_vector"):
            with self.assertRaises(ValueError):
                self.runner.run("SELECT 3", name)
        self.assertEqual(len(self.connections), 3)

    def test_real_driver_parses_connection_before_requesting_token(self):
        class Marker(Exception):
            pass

        class Credential:
            def get_token(self, *args, **kwargs):
                raise Marker()

        runner = SqlRunner("dojo-sql.database.windows.net", "sqldb-lab-dojo", Credential())
        self.assertNotIn("Connection Timeout", runner.connection_string)
        with self.assertRaises(Exception) as caught:
            runner.connect()
        self.assertIsInstance(caught.exception.__cause__, Marker)

    def test_login_retries_only_paused_database_and_preserves_timeout(self):
        self.assertTrue(is_waking(paused_error()))
        self.assertNotIn("(40613)", str(paused_error()))

        runner = SqlRunner("dojo-sql.database.windows.net", "sqldb-lab-dojo", self.credential)
        with mock.patch.object(runner.driver, "connect",
                               side_effect=[paused_error(), paused_error(), Connection()]) as connect, \
                mock.patch("app.labs.time.sleep") as sleep:
            self.assertIsInstance(runner.connect(), Connection)
        self.assertEqual(connect.call_count, 3)
        self.assertEqual([call.kwargs["timeout"] for call in connect.call_args_list], [90, 90, 90])
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        with mock.patch.object(runner.driver, "connect",
                               side_effect=RuntimeError("another SQL error (2627)")) as connect:
            with self.assertRaisesRegex(RuntimeError, "2627"):
                runner.connect()
            connect.assert_called_once()
        self.assertFalse(is_waking(RuntimeError("another SQL error (2627)")))

    def test_timeout_is_a_normal_response_and_errors_log_category_not_sql(self):
        from mssql_python.exceptions import sqlstate_to_exception

        dangerous = "SELECT 1 /* private query text */"
        timeout = sqlstate_to_exception("HYT00", "[Microsoft][ODBC Driver][SQL Server]Timeout expired")
        self.assertEqual(timeout.driver_error, "Timeout expired")
        with mock.patch.object(self.runner, "execute", side_effect=timeout):
            with self.assertLogs("dojo.labs", "WARNING") as captured:
                result = self.runner.run(dangerous, "lr_0123456789ab_vector")
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["message"], "Your query ran longer than 20 seconds.")
        self.assertTrue(self.connections[-1].closed)
        self.assertNotIn(dangerous, "\n".join(captured.output))
        with mock.patch.object(self.runner, "execute",
                               side_effect=sqlstate_to_exception("42S22",
                                                                  "[Microsoft][ODBC Driver][SQL Server]Invalid column name 'timeout' (207)")):
            with self.assertLogs("dojo.labs", "WARNING") as captured:
                with self.assertRaisesRegex(UserError, "Invalid column name"):
                    self.runner.run(dangerous, "lr_0123456789ab_vector")
        self.assertIn("Column not found", "\n".join(captured.output))
        self.assertNotIn(dangerous, "\n".join(captured.output))

    def test_log_reason_prefers_valid_sqlstate_and_never_includes_query_text(self):
        from types import SimpleNamespace

        self.assertEqual(sql_error_reason(SimpleNamespace(sqlstate="42S22")), "SQLSTATE 42S22")
        self.assertEqual(sql_error_reason(SimpleNamespace(driver_error="Column not found")),
                         "Column not found")
        self.assertEqual(sql_error_reason(SimpleNamespace(driver_error="Invalid SELECT private query")),
                         "database error")
        self.assertEqual(sql_error_reason(SimpleNamespace(driver_error="SELECT * FROM hidden;")),
                         "database error")

    def test_reset_is_limited_to_fixed_schema(self):
        self.runner.reset("lab_search")
        temporal, drops = (c.statements[0] for c in self.connections)
        self.assertEqual(len(self.connections), 2)
        self.assertIn("t.temporal_type=2", temporal)
        self.assertIn("SET (SYSTEM_VERSIONING = OFF)", temporal)
        self.assertIn("s.name='lab_search'", temporal)
        self.assertIn("hs.name='lab_search'", temporal)
        self.assertIn("JOIN sys.tables h ON h.object_id=t.history_table_id", temporal)
        # A reset touches one schema; the dbo clean-up is the owner's repair only (ADR 0009).
        self.assertNotIn("dbo", temporal + drops)
        self.assertNotIn("@external_history", temporal + drops)
        self.assertIn("sys.edge_constraints", drops)
        self.assertIn("sys.foreign_keys", drops)
        self.assertIn("DROP TABLE", drops)
        self.assertIn("DROP SEQUENCE", drops)
        self.assertIn("o.type='SO'", drops)
        self.assertIn("QUOTENAME(o.name)", drops)
        self.assertNotIn("lab_vector", temporal + drops)
        self.assertTrue(all(c.closed for c in self.connections))
        self.runner.reset("lab_search_0123456789ab")
        self.assertIn("s.name='lab_search_0123456789ab'", self.connections[-1].statements[0])
        for schema in ("dbo", "dojo_meta", "lab_search_0123456789ab'--", "lab_other", "sys"):
            with self.assertRaises(ValueError):
                self.runner.reset(schema)
        with self.assertRaises(ValueError):
            self.runner.drop("lab_search", "lab_search_owner", "lr_0123456789ab_search")   # the owner's legacy schema

    def test_repair_is_the_only_dbo_cleanup(self):
        self.runner.repair()
        sql = self.connections[0].statements[0]
        self.assertIn("FROM @external_history", sql)
        self.assertIn("DROP TABLE [dbo].", sql)

    def test_migration_gives_the_owner_runners_and_retires_the_shared_one(self):
        room = LabRoom(self.runner, False)
        room.prepare("search")
        sql = [c.statements[0] for c in self.connections]
        s = room.owner_user().surrogate
        self.assertRegex(s, r"^[0-9a-f]{12}$")
        self.assertIn("default_schema_name=N'lab_vector'", sql[0])   # recovered from the database first
        owners = next(x for x in sql if "lab_guard" in x and "CREATE SCHEMA" in x)
        for lab in LABS.values():
            schema = lab["schema"]
            self.assertIn(f"CREATE SCHEMA [{schema}] AUTHORIZATION [{schema}_owner]", owners)
            self.assertIn(f"ALTER AUTHORIZATION ON SCHEMA::[{schema}] TO [{schema}_owner]", owners)
        self.assertIn("GRANT VIEW DEFINITION TO [lab_guard]", owners)
        runners = next(x for x in sql if f"lr_{s}_vector" in x and f"lr_{s}_hybrid" in x)
        for lab, spec in LABS.items():
            self.assertIn(f"CREATE USER [lr_{s}_{lab}] WITHOUT LOGIN WITH DEFAULT_SCHEMA = [{spec['schema']}]", runners)
            self.assertIn(f"GRANT ALTER, SELECT, INSERT, UPDATE, DELETE ON SCHEMA::[{spec['schema']}] TO [lr_{s}_{lab}]", runners)
        retire = next(x for x in sql if "FROM [lab_runner]" in x)
        for schema in ("lab_vector", "lab_search", "lab_hybrid"):
            self.assertIn(f"REVOKE ALTER, SELECT, INSERT, UPDATE, DELETE ON SCHEMA::[{schema}] FROM [lab_runner]", retire)
        self.assertIn("REVOKE CREATE TABLE FROM [lab_runner]", retire)
        self.assertIn("EXEC(N'DROP USER [lab_runner]')", retire)
        self.assertIn("NOT EXISTS (SELECT 1 FROM sys.objects WHERE principal_id=DATABASE_PRINCIPAL_ID(N'lab_runner'))", retire)
        self.assertFalse(any("DROP TABLE" in x or "DROP SCHEMA" in x for x in sql))   # no table is touched
        n = len(self.connections)
        room.prepare("search")   # once per process; afterwards only this lab's runner and fixture
        self.assertFalse(any("lab_guard" in c.statements[0] or "lab_runner" in c.statements[0] for c in self.connections[n:]))
        guard = next(x for x in sql if "dojo_lab_history_guard" in x)
        for part in ("CREATE OR ALTER TRIGGER [dojo_lab_history_guard]", "FOR CREATE_TABLE, ALTER_TABLE",
                     "EXECUTE AS ''lab_guard''", "EVENTDATA().value", "t.schema_id<>h.schema_id",
                     "ROLLBACK TRANSACTION", "A history table must be in the same schema as its table."):
            self.assertIn(part, guard)
        setup = next(x for x in sql if "AS wrong_type" in x)
        self.assertIn("IF OBJECT_ID(N'lab_search.Products') IS NULL EXEC(N'CREATE TABLE", setup)
        self.assertIn("AND OBJECT_ID(N'lab_search.Products', N'U') IS NULL SELECT 1 AS wrong_type", setup)
        self.assertIn("N''Shoe''", setup)
        self.assertNotIn(" IS NULL BEGIN CREATE TABLE", setup)
        self.assertFalse(any("EXECUTE AS USER" in statement
                             for conn in self.connections for statement in conn.statements))

    def test_member_prepare_names_only_their_own_schema(self):
        from app.labs import LabUser
        room = LabRoom(self.runner, False)
        room.migrated = True
        room._owner = LabUser("aaaaaaaaaaaa", legacy=True)
        room.prepare("search", LabUser("0123456789ab"))
        sql = "\n".join(c.statements[0] for c in self.connections)
        self.assertIn("CREATE USER [lab_search_0123456789ab_owner] WITHOUT LOGIN", sql)
        self.assertIn("CREATE SCHEMA [lab_search_0123456789ab] AUTHORIZATION [lab_search_0123456789ab_owner]", sql)
        self.assertIn("CREATE USER [lr_0123456789ab_search] WITHOUT LOGIN WITH DEFAULT_SCHEMA = [lab_search_0123456789ab]", sql)
        self.assertIn("ON SCHEMA::[lab_search_0123456789ab] TO [lr_0123456789ab_search]", sql)
        self.assertIn("OBJECT_ID(N'lab_search_0123456789ab.Products')", sql)
        for other in ("[lab_search]", "[lab_vector", "aaaaaaaaaaaa", "dbo"):
            self.assertNotIn(other, sql)

    def test_reset_drops_objects_before_recreating_fixture_without_seed_gate(self):
        room = LabRoom(self.runner, False)
        room.operation("search", "reset")
        sql = [c.statements[0] for c in self.connections]
        off = next(i for i, x in enumerate(sql) if "SYSTEM_VERSIONING = OFF" in x)
        drop = next(i for i, x in enumerate(sql) if "sys.foreign_keys" in x)
        seeds = [i for i, x in enumerate(sql) if "OBJECT_ID(N'lab_search.Products'" in x]
        self.assertEqual(len(seeds), 1)
        self.assertLess(off, drop)
        self.assertLess(drop, seeds[0])
        self.assertIn("s.name='lab_search'", sql[drop])
        self.assertNotIn("lab_vector", sql[drop])
        self.assertIn("IS NULL EXEC(N'CREATE TABLE", sql[seeds[0]])

    def test_non_table_fixture_name_asks_for_reset_and_reset_skips_seed_first(self):
        room = LabRoom(self.runner, False)
        with mock.patch.object(self.runner, "admin", return_value={"rows": [["1"]]}) as admin:
            with self.assertRaisesRegex(UserError, "Press Reset this lab"):
                room.seed("search")
        self.assertIn("OBJECT_ID(N'lab_search.Products') IS NOT NULL", admin.call_args.args[0])

    def test_limits_and_one_run_at_a_time(self):
        for sql in ("", "a" * 8001, "GO", "SELECT 1;\nGO\n", "SELECT 1;" * 21):
            with self.assertRaises(UserError):
                validate(sql)
        self.assertEqual(MAX_ROWS, 200)
        room = LabRoom(FakeRunner(), True)
        room.lock.acquire()
        try:
            with self.assertRaises(UserError):
                room.operation("vector", "run", sql="SELECT 1")
        finally:
            room.lock.release()

    def test_results_are_capped_before_serialization(self):
        class ResultCursor(Cursor):
            description = [("value",)]

            def fetchmany(self, size):
                self.conn.requested = size
                return [(i,) for i in range(size)]

        class ResultConnection(Connection):
            def cursor(self):
                return ResultCursor(self)

        conn = ResultConnection()
        result = SqlRunner.execute(conn, "SELECT value FROM lab_vector.Products")
        self.assertEqual(conn.requested, 201)
        self.assertEqual(len(result["rows"]), 200)
        self.assertTrue(result["truncated"])


@unittest.skipUnless(os.environ.get("DOJO_LAB_LOCALDB_TEST") == "1", "requires a dedicated LocalDB instance")
class LocalDbIsolationTests(unittest.TestCase):
    """The lab rules on a real SQL Server: a dedicated LocalDB instance (AGENTS.md rule 11), never
    MSSQLLocalDB. DOJO_LAB_LOCALDB names the instance (default dojo-lab-test) and DOJO_LAB_LOCALDB_DB the
    database (default dojo_lab_test); the database is dropped and created again for every test."""
    A, B = LabUser("aaaaaaaaaaa1"), LabUser("bbbbbbbbbbb2")

    def setUp(self):
        import mssql_python

        instance = os.environ.get("DOJO_LAB_LOCALDB", "dojo-lab-test")
        if instance.lower() == "mssqllocaldb":
            self.skipTest("never the shared MSSQLLocalDB instance")
        database = os.environ.get("DOJO_LAB_LOCALDB_DB", "dojo_lab_test")
        server = f"Server=(localdb)\\{instance};Trusted_Connection=Yes;Encrypt=Optional;"
        try:
            master = mssql_python.connect(server + "Database=master;", autocommit=True, timeout=30)
        except Exception as exc:
            self.skipTest(f"dedicated LocalDB is not available: {type(exc).__name__}")
        try:
            cur = master.cursor()
            cur.execute(f"""IF DB_ID(N'{database}') IS NOT NULL BEGIN
                ALTER DATABASE [{database}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{database}]; END;
                CREATE DATABASE [{database}];""")
        finally:
            master.close()
        self.runner = SqlRunner("sandbox.database.windows.net", "dojolabtest", object())
        self.runner.connect = lambda: mssql_python.connect(server + f"Database={database};", timeout=10)
        # LocalDB may lack the vector type: plain fixtures with the same names.
        patch = mock.patch.dict(LABS, {lab: {**spec, "setup": (
            "CREATE TABLE {schema}.Products (Id int NOT NULL PRIMARY KEY, Name nvarchar(40) NOT NULL); "
            "INSERT INTO {schema}.Products (Id, Name) VALUES (1, N'Shoe'), (2, N'Sock');",)}
            for lab, spec in LABS.items()})
        patch.start()
        self.addCleanup(patch.stop)
        self.store = Store(Path(tempfile.mkdtemp(prefix="dojo-labdb-")))
        self.room = LabRoom(self.runner, False, self.store)

    def sql(self, statement: str):
        return self.runner.admin(statement)["rows"]

    def as_(self, who: LabUser, lab: str, statement: str):
        return self.runner.run(statement, who.names(lab)[2])

    @staticmethod
    def temporal(schema: str, name: str, history: str) -> str:
        return (f"CREATE TABLE {schema}.{name} (Id int NOT NULL PRIMARY KEY, "
                "SysStart datetime2 GENERATED ALWAYS AS ROW START, "
                "SysEnd datetime2 GENERATED ALWAYS AS ROW END, "
                f"PERIOD FOR SYSTEM_TIME (SysStart, SysEnd)) WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = {history}));")

    def test_migration_from_the_shared_runner_keeps_the_owner_tables_and_is_idempotent(self):
        # The database as PR 3 and earlier left it: legacy schemas and lab_runner with its grants.
        for schema in ("lab_vector", "lab_search", "lab_hybrid"):
            self.runner.admin(f"CREATE USER [{schema}_owner] WITHOUT LOGIN; EXEC(N'CREATE SCHEMA [{schema}] AUTHORIZATION [{schema}_owner]')")
        self.runner.admin("""CREATE USER [lab_runner] WITHOUT LOGIN; GRANT CREATE TABLE TO [lab_runner];
            GRANT ALTER, SELECT, INSERT, UPDATE, DELETE ON SCHEMA::[lab_vector] TO [lab_runner];
            GRANT ALTER, SELECT, INSERT, UPDATE, DELETE ON SCHEMA::[lab_search] TO [lab_runner];
            CREATE TABLE lab_vector.Products (Id int NOT NULL PRIMARY KEY, Name nvarchar(40) NOT NULL);
            INSERT INTO lab_vector.Products VALUES (7, N'Mine');
            CREATE TABLE lab_vector.MyWork (Id int);""")
        self.room.operation("vector", "check")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM sys.database_principals WHERE name = N'lab_runner'"), [["0"]])
        self.assertEqual(self.sql("SELECT Name FROM lab_vector.Products"), [["Mine"]])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM lab_vector.MyWork"), [["0"]])
        owner = self.room.owner_user()
        self.assertEqual(self.as_(owner, "vector", "SELECT Name FROM Products")["rows"], [["Mine"]])
        # A second process, and a lost team/owner.json: the same surrogate, from the database.
        again = LabRoom(self.runner, False, Store(Path(tempfile.mkdtemp(prefix="dojo-labdb-"))))
        self.assertEqual(again.owner_user().surrogate, owner.surrogate)
        again.operation("vector", "run", sql="INSERT INTO Products VALUES (8, N'More')")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM lab_vector.Products"), [["2"]])
        self.assertEqual(again.probe()["ok"], True)

    def test_a_member_reaches_only_their_own_schema(self):
        owner = self.room.owner_user()
        for who in (self.A, self.B, None):
            for lab in LABS:
                self.room.prepare(lab, who)
        Mirror(self.runner).read()   # dojo_meta exists
        self.runner.admin("CREATE TABLE dbo.Keep (Id int)")
        b = self.B.schema("search")
        self.assertEqual(self.as_(self.A, "search", "SELECT COUNT(*) FROM Products")["rows"], [["2"]])
        self.as_(self.A, "search", "CREATE TABLE Answers (Id int); INSERT INTO Answers VALUES (1);")
        self.assertEqual(self.sql(f"SELECT COUNT(*) FROM {self.A.schema('search')}.Answers"), [["1"]])
        for statement in (f"SELECT * FROM {b}.Products", f"INSERT INTO {b}.Products VALUES (9, N'x')",
                          f"DELETE FROM {b}.Products", f"DROP TABLE {b}.Products", f"CREATE TABLE {b}.Mine (Id int)",
                          f"ALTER TABLE {b}.Products ADD Extra int", "SELECT * FROM lab_search.Products",
                          f"SELECT * FROM {self.A.schema('vector')}.Products",   # their own other lab: another runner
                          "CREATE TABLE dbo.Mine (Id int)", "SELECT * FROM dbo.Keep", "SELECT * FROM dojo_meta.state",
                          "CREATE VIEW Peek AS SELECT 1 AS x", "CREATE PROCEDURE Peek AS SELECT 1",
                          f"CREATE SCHEMA mine", "EXECUTE AS USER = N'dbo'", f"EXECUTE AS USER = N'{self.B.names('search')[2]}'"):
            with self.subTest(statement=statement):
                with self.assertRaises(UserError):
                    self.as_(self.A, "search", statement)
        self.assertEqual(self.sql(f"SELECT COUNT(*) FROM {b}.Products"), [["2"]])
        # No metadata of other learners' tables, and no permission anywhere but here.
        seen = self.as_(self.A, "search", """SELECT COUNT(*) FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id
            WHERE s.name <> SCHEMA_NAME()""")["rows"]
        self.assertEqual(seen, [["0"]])
        perms = self.as_(self.A, "search", f"""SELECT COALESCE(HAS_PERMS_BY_NAME(N'{b}', N'SCHEMA', N'SELECT'), 0),
            COALESCE(HAS_PERMS_BY_NAME(N'lab_search', N'SCHEMA', N'SELECT'), 0),
            COALESCE(HAS_PERMS_BY_NAME(N'dbo', N'SCHEMA', N'ALTER'), 0),
            COALESCE(HAS_PERMS_BY_NAME(N'dojo_meta', N'SCHEMA', N'SELECT'), 0),
            COALESCE(HAS_PERMS_BY_NAME(N'{self.A.schema('search')}', N'SCHEMA', N'ALTER'), 0)""")["rows"]
        self.assertEqual(perms, [["0", "0", "0", "0", "1"]])
        self.assertEqual(self.as_(owner, "search", "SELECT COUNT(*) FROM Products")["rows"], [["2"]])

    def test_history_guard_and_reset_scope(self):
        for who in (self.A, self.B):
            self.room.prepare("search", who)
        a, b = self.A.schema("search"), self.B.schema("search")
        for history in ("dbo.Escaped", f"{b}.Escaped"):
            with self.subTest(history=history), self.assertRaises(UserError):
                self.as_(self.A, "search", self.temporal(a, "T1", history))
        self.as_(self.A, "search", self.temporal(a, "Mine", f"{a}.MineHistory"))
        self.as_(self.B, "search", self.temporal(b, "Theirs", f"{b}.TheirsHistory"))
        self.as_(self.A, "search", "CREATE TABLE Extra (Id int)")
        self.runner.admin("CREATE TABLE dbo.Keep (Id int)")
        self.room.operation("search", "reset", self.A)
        self.assertEqual(self.sql(f"""SELECT t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id
            WHERE s.name = N'{a}' ORDER BY t.name"""), [["Products"]])
        self.assertEqual(self.sql(f"""SELECT t.name, t.temporal_type FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id
            WHERE s.name = N'{b}' AND t.name = N'Theirs'"""), [["Theirs", "2"]])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM sys.tables WHERE name = N'Keep'"), [["1"]])

    def test_repair_is_the_owner_dbo_cleanup(self):
        self.room.prepare("search")
        self.runner.admin("DISABLE TRIGGER [dojo_lab_history_guard] ON DATABASE")
        try:
            self.runner.admin(self.temporal("lab_search", "ToDbo", "dbo.SecretHist"))
            self.runner.admin(self.temporal("lab_search", "ToVector", "lab_vector.EscapedHist"))
        finally:
            self.runner.admin("ENABLE TRIGGER [dojo_lab_history_guard] ON DATABASE")
        self.room.operation("search", "reset")   # the owner's reset: his schema only
        self.assertEqual(self.sql("SELECT COUNT(*) FROM sys.tables WHERE name IN (N'SecretHist', N'EscapedHist')"), [["2"]])
        self.room.repair()
        self.room.operation("vector", "reset")
        self.room.operation("search", "reset")
        self.assertEqual(self.sql("""SELECT COUNT(*) FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id
            WHERE s.name='dbo' OR t.temporal_type<>0 OR t.name IN ('ToDbo','ToVector','EscapedHist')"""), [["0"]])

    def test_the_reconciler_drops_a_deleted_member_and_nothing_else(self):
        owner = self.room.owner_user()
        for who in (self.A, self.B, None):
            for lab in LABS:
                self.room.prepare(lab, who)
        self.as_(self.A, "vector", "CREATE TABLE Work (Id int)")
        self.as_(self.A, "search", self.temporal(self.A.schema("search"), "Hist", f"{self.A.schema('search')}.HistH"))
        self.runner.admin("CREATE USER [lab_hybrid_cccccccccccc_owner] WITHOUT LOGIN; "
                          "EXEC(N'CREATE SCHEMA [lab_hybrid_cccccccccccc] AUTHORIZATION [lab_hybrid_cccccccccccc_owner]')")
        out = self.room.reconcile({sha256(self.A.surrogate)}, {self.B.surrogate, owner.surrogate})
        self.assertEqual(out, {"dropped": 1, "unknown": ["lab_hybrid_cccccccccccc"]})
        left = {r[0] for r in self.sql("SELECT name FROM sys.schemas WHERE name LIKE N'lab[_]%'")}
        self.assertFalse({s for s in left if self.A.surrogate in s})
        self.assertTrue({"lab_vector", "lab_search", "lab_hybrid", self.B.schema("vector"), "lab_hybrid_cccccccccccc"} <= left)
        users = {r[0] for r in self.sql("SELECT name FROM sys.database_principals")}
        self.assertFalse({u for u in users if self.A.surrogate in u})
        self.assertIn(self.B.names("vector")[2], users)
        self.assertEqual(self.room.reconcile({sha256(self.A.surrogate)}, {self.B.surrogate, owner.surrogate})["dropped"], 0)

    def test_the_mirror_round_trip(self):
        m = Mirror(self.runner)
        self.assertEqual(m.read(), {"epoch": 0, "paused": False, "tombstones": [], "changes": [], "retired": []})
        h, g = sha256("one"), sha256("two")
        doc = {"epoch": 5, "paused": True,
               "tombstones": [{"key": h, "lab": g, "at": "2026-09-01"}, {"key": g, "lab": h, "at": "2026-01-01"}],
               "changes": [{"member": h, "epoch": 4, "status": "removed", "reason": "left", "date": "2026-09-01"},
                           {"member": g, "epoch": 5, "status": "active", "reason": "rebound", "date": "2026-09-02"}],
               "retired": [{"principal": g, "at": "2026-01-01"}]}
        m.write(doc, "2026-08-01")
        m.write(doc, "2026-08-01")   # idempotent
        got = m.read()
        self.assertEqual((got["epoch"], got["paused"]), (5, True))
        self.assertEqual(got["tombstones"], [{"key": h, "lab": g, "at": "2026-09-01"}])   # older than the cutoff: gone
        self.assertEqual(sorted(got["changes"], key=lambda c: c["epoch"]), doc["changes"])
        self.assertEqual(got["retired"], [{"principal": g, "at": "2026-01-01"}])   # still listed on a row: kept
        self.room.prepare("vector", self.A)
        with self.assertRaises(UserError):
            self.as_(self.A, "vector", "SELECT * FROM dojo_meta.tombstones")


class LabRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(settings(os.environ["DOJO_DATA_DIR"]))
        cls.client = TestClient(cls.app)

    def test_owner_header_and_origin_guard(self):
        self.assertEqual(self.client.post("/api/labs/vector/run", json={"sql": "SELECT 1"}).status_code, 403)
        self.assertEqual(self.client.post("/api/labs/vector/check").status_code, 403)
        self.assertEqual(self.client.post("/api/labs/vector/reset", headers={**POST, "Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/labs/vector/hint", json={"index": 0}).status_code, 403)
        self.assertEqual(self.client.get("/api/labs/unknown").status_code, 404)
        self.assertEqual(self.client.post("/api/labs/vector/run", json={"sql": "DROP TABLE lab_vector.Products"},
                                          headers=POST).status_code, 409)

    def test_paused_database_is_not_a_failed_check_or_a_generic_query_error(self):
        with mock.patch.object(self.app.state.labs, "operation", side_effect=paused_error()):
            with self.assertLogs("dojo", "WARNING") as captured:
                check = self.client.post("/api/labs/vector/check", headers=POST)
                run = self.client.post("/api/labs/vector/run", json={"sql": "SELECT 1"}, headers=POST)
        self.assertEqual(check.status_code, 503)
        self.assertIn("Waking the database, about a minute", check.json()["detail"])
        self.assertEqual(run.status_code, 503)
        self.assertIn("Waking the database, about a minute", run.json()["detail"])
        self.assertIn("database waking", "\n".join(captured.output))
        self.assertNotIn("SELECT 1", "\n".join(captured.output))
        with mock.patch.object(self.app.state.labs, "operation", return_value={
            "columns": [], "rows": [], "truncated": False, "timed_out": True,
            "message": "Your query ran longer than 20 seconds.",
        }):
            timed = self.client.post("/api/labs/vector/run", json={"sql": "SELECT 1"}, headers=POST)
        self.assertEqual(timed.status_code, 200)
        self.assertTrue(timed.json()["timed_out"])

    def test_reset_reports_sql_error_and_fixture_collision_requests_reset(self):
        from mssql_python import ProgrammingError

        error = ProgrammingError("General error",
                                 "[Microsoft][ODBC Driver][SQL Server]Cannot drop a system-versioned table (13552)")
        with mock.patch.object(self.app.state.labs, "operation", side_effect=error):
            with self.assertLogs("dojo", "WARNING") as captured:
                reset = self.client.post("/api/labs/vector/reset", headers=POST)
        self.assertEqual(reset.status_code, 422)
        self.assertIn("Cannot drop a system-versioned table", reset.json()["detail"])
        self.assertIn("ProgrammingError; General error", "\n".join(captured.output))
        self.assertNotIn("Cannot drop a system-versioned table", "\n".join(captured.output))

        with mock.patch.object(self.app.state.labs, "operation",
                               side_effect=UserError("The fixture name is used by a different object. Press Reset this lab.")):
            for action in ("run", "check"):
                response = self.client.post(f"/api/labs/vector/{action}",
                                            headers=POST, json={"sql": "SELECT 1"} if action == "run" else None)
                self.assertEqual(response.status_code, 422)
                self.assertIn("Press Reset this lab", response.json()["detail"])

    def test_fake_run_check_hints_reset_and_no_pips(self):
        c = self.client
        before = c.get("/api/skills/dp-800/d3.g2.s3").json()["evidence"]["checks"]
        lab = c.get("/api/labs/vector").json()
        self.assertTrue(lab["local"])
        self.assertEqual(lab["skill"], "d3.g2.s3")
        self.assertEqual(lab["hint_count"], 2)
        self.assertNotIn("hints", lab)
        self.assertEqual(c.post("/api/labs/vector/run", json={"sql": "SELECT 1"}, headers=POST).json()["rows"][0][0],
                         "Local demo, no database")
        self.assertIn("hint", c.post("/api/labs/vector/hint", json={"index": 0}, headers=POST).json())
        result = c.post("/api/labs/vector/check", headers=POST).json()
        self.assertFalse(result["passed"])
        self.assertNotIn("conditions", result)
        self.assertEqual(before, c.get("/api/skills/dp-800/d3.g2.s3").json()["evidence"]["checks"])
        self.assertEqual(c.post("/api/labs/vector/reset", headers=POST).status_code, 200)
        self.assertEqual(c.get("/api/diag").json()["labs"]["probe"].startswith("Use POST"), True)
        self.assertEqual(c.post("/api/diag/lab", headers=POST).json()["database_checked"], False)

    def test_fake_pattern_can_preview_success_without_filling_a_pip(self):
        c = self.client
        c.post("/api/labs/search/reset", headers=POST)
        sql = """SELECT TOP (2) Id, VECTOR_DISTANCE('cosine', Embedding, '[1,0,0]') AS distance
                 INTO lab_search.Answers FROM lab_search.Products ORDER BY distance;"""
        self.assertEqual(c.post("/api/labs/search/run", json={"sql": sql}, headers=POST).status_code, 200)
        result = c.post("/api/labs/search/check", headers=POST).json()
        self.assertTrue(result["passed"])
        self.assertIn("not a database check", result["message"])
        self.assertEqual(c.get("/api/skills/dp-800/d3.g2.s7").json()["labs"], [])
        self.assertFalse(c.get("/api/skills/dp-800/d3.g2.s7").json()["evidence"]["checks"]["unaided"])

    def test_detail_records_teaching_without_leaking_hint_text(self):
        learner = self.app.state.preparer.owner
        store = self.app.state.dojo.store
        before = len(store.events(learner.key))
        result = self.client.get("/api/labs/vector")
        self.assertEqual(result.status_code, 200)
        self.assertNotIn("hints", result.json())
        self.assertEqual(result.json()["hint_count"], 2)
        event = store.events(learner.key)[before]
        self.assertEqual((event["type"], event["data"]["skill"]), ("lab.opened", "d3.g2.s3"))

    def test_catalog_and_doc_sources_cover_real_skills(self):
        skills = {s["id"] for d in self.client.get("/api/packages/dp-800").json()["domains"]
                  for g in d["groups"] for s in g["skills"]}
        for lab in LABS.values():
            self.assertIn(lab["skill"], skills)
            self.assertTrue(lab["sources"])
            self.assertIn("SELECT", lab["verify"])


class InfrastructureTests(unittest.TestCase):
    def test_bicep_free_private_and_identity_boundaries(self):
        b = (ROOT / "infra" / "main.bicep").read_text("utf-8")
        server = b.split("resource sqlServer 'Microsoft.Sql/servers@", 1)[1].split("resource labDatabase ", 1)[0]
        self.assertIn("publicNetworkAccess: 'Disabled'", server)
        self.assertRegex(server, r"administrators:\s*\{[^{}]*azureADOnlyAuthentication: true")
        self.assertIn("sid: appIdentity.properties.clientId", server)
        self.assertIn("principalType: 'Application'", server)
        self.assertIn("login: appIdentity.name", server)
        for choice in ("useFreeLimit: true",
                       "freeLimitExhaustionBehavior: 'AutoPause'", "vnetRouteAllEnabled: false",
                       "privatelink${environment().suffixes.sqlServerHostname}", "groupIds: ['sqlServer']",
                       "Microsoft.Web/serverFarms", "virtualNetworkSubnetId: integrationSubnet.id"):
            self.assertIn(choice, b)
        self.assertNotIn("Microsoft.Authorization/roleAssignments", b)

    def test_lab_ui_never_aborts_an_inflight_sql_write(self):
        script = (ROOT / "app" / "static" / "app.js").read_text("utf-8")
        lab = script.split("async function viewLab(id) {", 1)[1].split("// ---------------------------------------------------------------- router", 1)[0]
        self.assertNotIn("AbortController", lab)
        self.assertNotIn("signal: controller.signal", lab)
        self.assertIn("const deadline = Date.now() + 120000", lab)
        self.assertIn("if (lab.local || e.status !== 503 || !e.message.startsWith(\"Waking the database\")) throw e;", lab)
        self.assertIn("await sleep(10000)", lab)


class LabTeachingTests(unittest.TestCase):
    def test_every_lab_interaction_restarts_later_clock(self):
        pkg = Packages(ROOT / "app" / "packages").get("dp-800")
        at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        skill = "d3.g2.s3"

        def event(minutes, event_type, **extra):
            return {"at": (at + timedelta(minutes=minutes)).isoformat(), "type": event_type,
                    "data": {"package": "dp-800", "skill": skill, **extra}}

        for kind in ("lab.opened", "lab.hint", "lab.ran", "lab.checked"):
            with self.subTest(kind=kind):
                events = [event(0, "lesson.viewed", lesson="l1"), event(21 * 60, kind, lab="vector"),
                          event(21 * 60 + 4, "item.issued", item="i1", mode="check", kind="scenario"),
                          event(21 * 60 + 5, "answer.judged", item="i1", mode="check", met=2, total=2)]
                rec = derive(events, pkg)[skill]
                self.assertEqual(rec["attempts"][0]["minutes_since_teaching"], 5)
                self.assertFalse(rec["checks"]["later"])
                self.assertNotEqual(rec["state"], "met_unaided_later")
                self.assertIn(kind, HELP_EVENTS)
