import concurrent.futures
import errno
import os
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from datom.engine import AccessPath, Engine, ResultSink
from datom.errors import DbError
from datom.sql import Statement, parse_sql


# Produced by the original C++ CLI for two rows, including UTF-8 text.
LEGACY_FRAME = bytes.fromhex(
    "445458313300000002000000ba3b919c5d732c75"
    "01000000000000000600000009000000eab8b0eca1b461406578616d706c65"
    "02000000000000000300000001000000426f6262434d5431"
)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="datom-engine-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def open_engine(self):
        engine = Engine(self.directory)
        self.addCleanup(engine.close)
        return engine

    def sql(self, engine, text, sink=None):
        statements = parse_sql(text)
        self.assertEqual(len(statements), 1)
        return engine.execute(statements[0], sink)

    def assert_code(self, code, operation):
        with self.assertRaises(DbError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def test_schema_projection_and_access_paths(self):
        engine = self.open_engine()
        empty = self.sql(engine, "select * from users")
        self.assertEqual(empty.columns, ["id", "name", "email"])
        self.assertEqual(empty.rows, [])
        inserted = self.sql(engine, "insert into users(email,name) values "
                            "('ada@test','Ada'),('bob@test','Bob'),('other@test','Ada')")
        self.assertEqual(inserted.tag, "INSERT 0 3")
        self.assertEqual(inserted.access, AccessPath.NONE)
        indexed = self.sql(engine, "select email,id,name from users where id = +002")
        self.assertEqual(indexed.rows, [["bob@test", "2", "Bob"]])
        self.assertEqual(indexed.access, AccessPath.INDEX_LOOKUP)
        self.assertEqual(indexed.rows_examined, 1)
        scanned = self.sql(engine, "select email,id,name from users where email = 'bob@test'")
        self.assertEqual(scanned.rows, indexed.rows)
        self.assertEqual(scanned.rows_examined, 3)
        self.assertEqual(scanned.access, AccessPath.SEQUENTIAL_SCAN)
        self.assertEqual(self.sql(engine, "select id from users where name='Ada'").rows,
                         [["1"], ["3"]])
        for missing in ["0", "-1", "4", "-9223372036854775808"]:
            with self.subTest(missing=missing):
                result = self.sql(engine, f"select * from users where id={missing}")
                self.assertEqual(result.rows, [])
                self.assertEqual(result.rows_examined, 0)
                self.assertEqual(result.access, AccessPath.INDEX_LOOKUP)

    def test_invalid_statements_leave_file_and_sequence_unchanged(self):
        engine = self.open_engine()
        self.sql(engine, "insert into users values ('kept','e')")
        original = (self.directory / "users.tbl").read_bytes()
        cases = [
            ("42P01", "select * from absent"),
            ("42P01", "insert into absent values ('x','y')"),
            ("42703", "select unknown from users"),
            ("42703", "select * from users where x='x'"),
            ("42703", "insert into users(name,x) values ('a','b')"),
            ("23502", "insert into users(name) values ('a')"),
            ("42701", "insert into users(name,name) values ('a','b')"),
            ("428C9", "insert into users(id,name,email) values (8,'a','b')"),
            ("22P02", "select * from users where id='4x'"),
            ("22P02", "select * from users where id='+-4'"),
            ("22P02", "select * from users where id=' 4'"),
            ("22003", "select * from users where id=9223372036854775808"),
            ("22003", "select * from users where id=-9223372036854775809"),
            ("42601", "insert into users values ('lost','x'),('incomplete')"),
        ]
        for code, query in cases:
            with self.subTest(query=query):
                self.assert_code(code, lambda: self.sql(engine, query))
                self.assertEqual((self.directory / "users.tbl").read_bytes(), original)
        self.assert_code("42601", lambda: engine.execute(Statement("insert", "users")))
        self.assert_code("23502", lambda: engine.execute(
            Statement("insert", "users", values=[[None, "x"]])))
        self.assert_code("42804", lambda: engine.execute(
            Statement("insert", "users", values=[[3, "x"]])))
        self.assert_code("22021", lambda: engine.execute(
            Statement("insert", "users", values=[["\udcff", "x"]])))
        self.sql(engine, "insert into users values ('next','e')")
        self.assertEqual(self.sql(engine, "select name from users where id=2").rows, [["next"]])

    def test_table_isolation_locking_and_restart(self):
        engine = self.open_engine()
        self.assert_code("55006", lambda: Engine(self.directory))
        self.sql(engine, "insert into users values ('u1','e'),('u2','f')")
        self.sql(engine, "insert into products(sku,name) values ('s1','p1')")
        self.assert_code("42703", lambda: self.sql(engine, "select email from products"))
        engine.close()
        engine.close()
        self.assert_code("55000", lambda: self.sql(engine, "select * from users"))
        reopened = self.open_engine()
        self.assertEqual(self.sql(reopened, "select name,sku from products where id=1").rows,
                         [["p1", "s1"]])
        self.sql(reopened, "insert into users values ('u3','g')")
        self.sql(reopened, "insert into products values ('p2','s2')")
        self.assertEqual(self.sql(reopened, "select name from users where id=3").rows, [["u3"]])
        self.assertEqual(self.sql(reopened, "select name from products where id=2").rows, [["p2"]])
        self.assertEqual({path.name for path in self.directory.iterdir()},
                         {"users.tbl", "products.tbl"})

    def test_legacy_format_round_trip(self):
        (self.directory / "users.tbl").write_bytes(LEGACY_FRAME)
        with Engine(self.directory) as engine:
            self.assertEqual(self.sql(engine, "select * from users").rows,
                             [["1", "기존", "a@example"], ["2", "Bob", "b"]])
            self.sql(engine, "insert into users values ('new','next')")
        file_bytes = (self.directory / "users.tbl").read_bytes()
        self.assertTrue(file_bytes.startswith(LEGACY_FRAME))
        # Re-creating the same SQL through Python must produce the same bytes.
        other = self.directory / "other"
        with Engine(other) as engine:
            self.sql(engine, "insert into users values ('기존','a@example'),('Bob','b')")
        self.assertEqual((other / "users.tbl").read_bytes(), LEGACY_FRAME)

    def test_every_interrupted_frame_boundary(self):
        file = self.directory / "users.tbl"
        with Engine(self.directory) as engine:
            self.sql(engine, "insert into users values ('kept','a')")
            previous = file.read_bytes()
            self.sql(engine, "insert into users values ('lost-one','b'),('lost-two','c')")
            complete = file.read_bytes()
        for cut in range(len(previous), len(complete)):
            with self.subTest(cut=cut):
                file.write_bytes(complete[:cut])
                with Engine(self.directory) as engine:
                    self.assertEqual(self.sql(engine, "select name from users").rows, [["kept"]])
                    self.assertEqual(file.read_bytes(), previous)
                    self.sql(engine, "insert into users values ('next','d')")
                    self.assertEqual(self.sql(engine, "select name from users where id=2").rows,
                                     [["next"]])

    def test_complete_frame_corruption_is_never_truncated(self):
        file = self.directory / "users.tbl"
        for position in range(len(LEGACY_FRAME)):
            with self.subTest(position=position):
                corrupted = bytearray(LEGACY_FRAME)
                corrupted[position] ^= 1
                file.write_bytes(corrupted)
                self.assert_code("XX001", lambda: Engine(self.directory))
                self.assertEqual(file.read_bytes(), corrupted)

    def test_recovery_validates_ids_beyond_checksum(self):
        import datom.engine as storage
        file = self.directory / "users.tbl"
        malformed = bytearray(LEGACY_FRAME)
        struct.pack_into("<Q", malformed, 20, 2)
        struct.pack_into("<I", malformed, 12, storage._checksum(malformed[20:-4]))
        struct.pack_into("<I", malformed, 16, storage._checksum(malformed[:16]))
        file.write_bytes(malformed)
        self.assert_code("XX001", lambda: Engine(self.directory))
        self.assertEqual(file.read_bytes(), malformed)

    def test_short_reads_writes_and_interrupted_syscalls(self):
        engine = self.open_engine()
        original_pwrite = os.pwrite
        writes = 0

        def partial_write(fd, data, offset):
            nonlocal writes
            writes += 1
            if writes == 1:
                raise InterruptedError(errno.EINTR, "interrupted")
            return original_pwrite(fd, data[:7], offset)

        with patch("datom.engine.os.pwrite", side_effect=partial_write):
            self.sql(engine, "insert into users values ('first','a'),('second','b')")
        self.assertGreater(writes, 2)
        original_pread = os.pread
        reads = 0

        def partial_read(fd, size, offset):
            nonlocal reads
            reads += 1
            if reads == 1:
                raise InterruptedError(errno.EINTR, "interrupted")
            return original_pread(fd, min(size, 3), offset)

        with patch("datom.engine.os.pread", side_effect=partial_read):
            self.assertEqual(self.sql(engine, "select name from users").rows,
                             [["first"], ["second"]])
            self.assertEqual(self.sql(engine, "select name from users where id=2").rows,
                             [["second"]])

    def test_partial_write_failure_rolls_back_whole_batch(self):
        engine = self.open_engine()
        self.sql(engine, "insert into users values ('kept','a')")
        file = self.directory / "users.tbl"
        previous = file.read_bytes()
        original = os.pwrite
        calls = 0

        def fail_write(fd, data, offset):
            nonlocal calls
            calls += 1
            if calls == 1:
                return original(fd, data[:11], offset)
            raise OSError(errno.ENOSPC, "disk full")

        with patch("datom.engine.os.pwrite", side_effect=fail_write):
            self.assert_code("58030", lambda: self.sql(
                engine, "insert into users values ('lost','b'),('also lost','c')"))
        self.assertEqual(file.read_bytes(), previous)
        self.sql(engine, "insert into users values ('next','d')")
        self.assertEqual(self.sql(engine, "select name from users where id=2").rows, [["next"]])

    def test_failed_fsync_rolls_back_and_syncs_truncation(self):
        engine = self.open_engine()
        self.sql(engine, "insert into users values ('kept','a')")
        file = self.directory / "users.tbl"
        previous = file.read_bytes()
        original = os.fsync
        calls = 0

        def fail_once(fd):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError(errno.EIO, "sync failed")
            return original(fd)

        with patch("datom.engine.os.fsync", side_effect=fail_once):
            self.assert_code("58030", lambda: self.sql(engine, "insert into users values ('lost','b')"))
        self.assertEqual(calls, 2)
        self.assertEqual(file.read_bytes(), previous)
        self.assertEqual(self.sql(engine, "select * from users").rows, [["1", "kept", "a"]])

    def test_index_allocation_failure_restores_file_and_index(self):
        engine = self.open_engine()
        self.sql(engine, "insert into users values ('kept','a')")
        file = self.directory / "users.tbl"
        previous = file.read_bytes()
        index = engine._tables["users"].index
        original = index.insert

        def fail_second(key, offset):
            if key == 3:
                raise MemoryError("injected index allocation failure")
            original(key, offset)

        with patch.object(index, "insert", side_effect=fail_second):
            with self.assertRaises(MemoryError):
                self.sql(engine, "insert into users values ('lost','b'),('also lost','c')")
        self.assertEqual(file.read_bytes(), previous)
        self.assertEqual(self.sql(engine, "select * from users where id=2").rows, [])
        self.sql(engine, "insert into users values ('next','d')")
        self.assertEqual(self.sql(engine, "select name from users where id=2").rows, [["next"]])

    def test_failed_rollback_blocks_table_until_reopen(self):
        engine = self.open_engine()
        self.sql(engine, "insert into users values ('kept','a')")
        original = os.pwrite
        calls = 0

        def fail_write(fd, data, offset):
            nonlocal calls
            calls += 1
            if calls == 1:
                return original(fd, data[:7], offset)
            raise OSError(errno.ENOSPC, "disk full")

        with patch("datom.engine.os.pwrite", side_effect=fail_write), patch(
                "datom.engine.os.ftruncate", side_effect=OSError(errno.EIO, "truncate failed")):
            self.assert_code("58030", lambda: self.sql(engine, "insert into users values ('lost','b')"))
        self.assert_code("58030", lambda: self.sql(engine, "select * from users"))
        self.assert_code("58030", lambda: self.sql(engine, "insert into users values ('x','y')"))
        engine.close()
        reopened = self.open_engine()
        self.assertEqual(self.sql(reopened, "select name from users").rows, [["kept"]])

    def test_parallel_writers_and_consistent_statement_snapshots(self):
        engine = self.open_engine()
        start = threading.Barrier(5)

        def writer(number):
            start.wait(timeout=5)
            for _ in range(8):
                self.sql(engine, f"insert into users values ('w{number}','a'),('w{number}','b')")

        def reader():
            start.wait(timeout=5)
            for _ in range(32):
                rows = self.sql(engine, "select id from users").rows
                self.assertEqual(len(rows) % 2, 0)
                self.assertEqual(rows, [[str(i)] for i in range(1, len(rows) + 1)])

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            tasks = [pool.submit(writer, i) for i in range(4)] + [pool.submit(reader)]
            for task in tasks:
                task.result(timeout=15)
        rows = self.sql(engine, "select * from users").rows
        self.assertEqual(len(rows), 64)
        for index, row in enumerate(rows, 1):
            self.assertEqual(self.sql(engine, f"select * from users where id={index}").rows, [row])

    def test_stream_callbacks_release_lock_and_keep_snapshot(self):
        engine = self.open_engine()
        self.sql(engine, "insert into users values ('first','a'),('second','b')")
        rows = []
        columns = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            def on_columns(names):
                columns.extend(names)
                # Re-entrant writes from the same thread must also be supported.
                self.sql(engine, "insert into users values ('header insert','c')")

            def on_row(row):
                rows.append(row)
                if len(rows) == 1:
                    pool.submit(self.sql, engine,
                                "insert into users values ('row insert','d')").result(timeout=5)

            result = self.sql(engine, "select id from users", ResultSink(on_columns, on_row))
        self.assertEqual(columns, ["id"])
        self.assertEqual(rows, [["1"], ["2"]])
        self.assertEqual(result.rows, [])
        self.assertEqual(result.tag, "SELECT 2")
        self.assertEqual(result.rows_examined, 2)
        self.assertEqual(len(self.sql(engine, "select * from users").rows), 4)
        sink = ResultSink(
            lambda _: self.sql(engine, "insert into users values ('late','e')"),
            lambda _: self.fail("an index miss saw a later insert"))
        self.assertEqual(self.sql(engine, "select * from users where id=5", sink).tag, "SELECT 0")

    def test_close_waits_for_active_statement(self):
        engine = self.open_engine()
        self.sql(engine, "insert into users values ('first','a')")
        entered, release = threading.Event(), threading.Event()

        def on_row(row):
            entered.set()
            self.assertTrue(release.wait(timeout=5))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            query = pool.submit(self.sql, engine, "select * from users",
                                ResultSink(lambda _: None, on_row))
            self.assertTrue(entered.wait(timeout=5))
            close = pool.submit(engine.close)
            self.assertFalse(close.done())
            release.set()
            self.assertEqual(query.result(timeout=5).tag, "SELECT 1")
            close.result(timeout=5)
        self.assert_code("55000", lambda: self.sql(engine, "select * from users"))

    def test_projection_size_limit_before_row_callback(self):
        engine = self.open_engine()
        self.sql(engine, "insert into users values ('large','email')")
        seen = []
        with patch("datom.engine.MAX_PAYLOAD", 18):
            # Stored payload exceeds the lowered limit, so use indexed lookup
            # to isolate the projected output-size bound.
            self.assert_code("54000", lambda: self.sql(
                engine, "select name,name,name,name from users where id=1",
                ResultSink(lambda _: None, seen.append)))
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
