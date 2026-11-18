"""Wire contract checks independent of persistent storage and the TCP accept loop."""

import fcntl
import os
import resource
import socket
import struct
import threading
import time
import unittest
from types import SimpleNamespace

from datom.errors import DbError
from datom.pgwire import MAX_REQUEST_SIZE, PROTOCOL_V3, SessionPhase, serve_message


U32 = struct.Struct("!I")


def frame(kind, payload=b""):
    return kind + U32.pack(len(payload) + 4) + payload


def startup(parameters=b"user\0tester\0database\0datom\0\0", version=PROTOCOL_V3):
    return frame(b"", U32.pack(version) + parameters)


def read_exact(conn, size):
    result = bytearray()
    while len(result) < size:
        part = conn.recv(size - len(result))
        if not part:
            raise EOFError("truncated reply")
        result.extend(part)
    return bytes(result)


def read_frame(conn):
    kind = read_exact(conn, 1)
    length, = U32.unpack(read_exact(conn, 4))
    if not 4 <= length <= 64 * 1024 * 1024:
        raise AssertionError("invalid backend frame length")
    return kind, read_exact(conn, length - 4)


def reply(conn):
    result = []
    while True:
        message = read_frame(conn)
        result.append(message)
        if message[0] == b"Z":
            return result


class FakeEngine:
    def __init__(self):
        self.statements = []
        self.rows = [["42", "김;O'Brien"]]

    def execute(self, statement, sink=None):
        self.statements.append(statement)
        if statement.table == "missing":
            raise DbError("42P01", "unknown table: missing")
        if statement.kind == "insert":
            return SimpleNamespace(tag="INSERT 0 " + str(len(statement.values)))
        sink.columns(["id", "name"])
        for row in self.rows:
            sink.row(row)
        return SimpleNamespace(tag="SELECT " + str(len(self.rows)))


class WireTests(unittest.TestCase):
    def setUp(self):
        self.server, self.client = socket.socketpair()
        self.server.setblocking(False)
        self.client.settimeout(3)
        self.addCleanup(self.server.close)
        self.addCleanup(self.client.close)
        self.engine = FakeEngine()
        self.phase = SessionPhase.STARTUP

    def process(self, data, timeout=0.5):
        self.client.sendall(data)
        self.phase = serve_message(self.server, self.engine, self.phase, timeout)
        return self.phase

    def authenticate(self):
        self.assertEqual(self.process(startup()), SessionPhase.READY)
        messages = reply(self.client)
        self.assertEqual(messages[0], (b"R", b"\0\0\0\0"))
        self.assertEqual(messages[-1], (b"Z", b"I"))
        self.assertEqual(self.server.gettimeout(), 0.0)

    def assert_error(self, message, code, fatal=False):
        self.assertEqual(message[0], b"E")
        fields = message[1].split(b"\0")
        self.assertIn(b"C" + code, fields)
        self.assertIn(b"SFATAL" if fatal else b"SERROR", fields)

    def test_encryption_decline_and_newer_protocol_negotiation(self):
        for request in (80877103, 80877104):
            self.assertEqual(self.process(U32.pack(8) + U32.pack(request)), SessionPhase.STARTUP)
            self.assertEqual(read_exact(self.client, 1), b"N")
        params = b"user\0tester\0database\0datom\0_pq_.extra\0value\0\0"
        self.assertEqual(self.process(startup(params, PROTOCOL_V3 + 2)), SessionPhase.READY)
        messages = reply(self.client)
        self.assertEqual(messages[0], (b"v", struct.pack("!II", PROTOCOL_V3, 1) + b"_pq_.extra\0"))
        self.assertEqual(messages[1], (b"R", b"\0\0\0\0"))

    def test_startup_defaults_database_to_user(self):
        self.assertEqual(self.process(startup(b"user\0datom\0\0")), SessionPhase.READY)
        self.assertEqual(reply(self.client)[-1], (b"Z", b"I"))

    def test_wrong_database_fails_without_authentication(self):
        self.assertIsNone(self.process(startup(b"user\0tester\0\0")))
        self.assert_error(read_frame(self.client), b"3D000", fatal=True)
        self.assertEqual(self.engine.statements, [])

    def test_malformed_startup_fields(self):
        self.assertIsNone(self.process(startup(b"user\0tester\0database\0datom\0")))
        self.assert_error(read_frame(self.client), b"08P01", fatal=True)

    def test_incomplete_startup_length(self):
        self.assertIsNone(self.process(U32.pack(3)))
        self.assert_error(read_frame(self.client), b"08P01", fatal=True)

    def test_fragmented_startup_and_query_frames(self):
        def fragmented(data):
            outcome = []
            task = threading.Thread(target=lambda: outcome.append(
                serve_message(self.server, self.engine, self.phase, 1.0)))
            task.start()
            try:
                for part in (data[:2], data[2:5], data[5:-1], data[-1:]):
                    self.client.sendall(part)
                    time.sleep(0.005)
            finally:
                task.join(2)
            self.assertFalse(task.is_alive())
            self.phase = outcome[0]
            return reply(self.client)

        self.assertEqual(fragmented(startup())[-1], (b"Z", b"I"))
        messages = fragmented(frame(b"Q", b"SELECT id, name FROM users WHERE id=42\0"))
        self.assertEqual([kind for kind, _ in messages], [b"T", b"D", b"C", b"Z"])
        expected_name = "김;O'Brien".encode()
        self.assertEqual(messages[1][1], b"\0\2" + U32.pack(2) + b"42" + U32.pack(len(expected_name)) + expected_name)
        description = messages[0][1]
        self.assertEqual(description[:2], b"\0\2")
        self.assertEqual(struct.unpack_from("!IhIhih", description, 5), (0, 0, 20, 8, -1, 0))
        self.assertEqual(messages[-2], (b"C", b"SELECT 1\0"))

    def test_one_call_consumes_exactly_one_queued_message(self):
        self.authenticate()
        self.process(frame(b"Q", b";\0") + frame(b"Q", b"SELECT * FROM users\0"))
        self.assertEqual(reply(self.client), [(b"I", b""), (b"Z", b"I")])
        self.assertEqual(self.engine.statements, [])
        self.phase = serve_message(self.server, self.engine, self.phase)
        self.assertEqual(reply(self.client)[-2], (b"C", b"SELECT 1\0"))

    def test_query_error_does_not_break_next_query(self):
        self.authenticate()
        for sql, code in ((b"SELECT * FROM missing", b"42P01"), (b"SELECT FROM", b"42601"),
                          (b"SELECT '\xff' FROM users", b"22021")):
            self.assertEqual(self.process(frame(b"Q", sql + b"\0")), SessionPhase.READY)
            messages = reply(self.client)
            self.assert_error(messages[0], code)
            self.assertEqual(messages[-1], (b"Z", b"I"))
        self.process(frame(b"Q", b"INSERT INTO users VALUES ('ok','x')\0"))
        self.assertEqual(reply(self.client), [(b"C", b"INSERT 0 1\0"), (b"Z", b"I")])

    def test_multiple_statements_rejected_before_execution(self):
        self.authenticate()
        self.process(frame(b"Q", b"INSERT INTO users VALUES ('x','y'); SELECT * FROM users\0"))
        self.assert_error(reply(self.client)[0], b"0A000")
        self.assertEqual(self.engine.statements, [])

    def test_extended_error_discards_until_sync(self):
        self.authenticate()
        self.assertEqual(self.process(frame(b"P", b"")), SessionPhase.AWAIT_SYNC)
        self.assert_error(read_frame(self.client), b"0A000")
        self.process(frame(b"Q", b"INSERT INTO users VALUES ('x','y')\0"))
        self.assertEqual(self.engine.statements, [])
        self.assertEqual(self.process(frame(b"S")), SessionPhase.READY)
        self.assertEqual(reply(self.client), [(b"Z", b"I")])
        self.process(frame(b"Q", b"SELECT * FROM users\0"))
        self.assertEqual(reply(self.client)[-2], (b"C", b"SELECT 1\0"))

    def test_trailing_query_payload_is_fatal(self):
        self.authenticate()
        self.assertIsNone(self.process(frame(b"Q", b"SELECT * FROM users\0trailing")))
        self.assert_error(read_frame(self.client), b"08P01", fatal=True)

    def test_oversized_frame_rejected_before_body_read(self):
        self.authenticate()
        self.assertIsNone(self.process(b"Q" + U32.pack(MAX_REQUEST_SIZE + 1)))
        self.assert_error(read_frame(self.client), b"08P01", fatal=True)

    def test_terminate_and_peer_disconnect_release_session(self):
        self.authenticate()
        self.assertIsNone(self.process(frame(b"X")))
        self.client.close()
        self.assertIsNone(serve_message(self.server, self.engine, SessionPhase.STARTUP, 0.1))

    def test_wait_accepts_socket_descriptors_above_select_limit(self):
        if resource.getrlimit(resource.RLIMIT_NOFILE)[0] <= 1050:
            self.skipTest("open file limit is too small for a high-descriptor socket")
        try:
            descriptor = fcntl.fcntl(self.server.fileno(), fcntl.F_DUPFD, 1050)
        except OSError as error:
            self.skipTest("cannot allocate a high-descriptor socket: " + str(error))
        try:
            high_socket = socket.socket(fileno=descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        self.addCleanup(high_socket.close)
        self.server.close()
        self.server = high_socket
        self.server.setblocking(False)
        sender = threading.Timer(0.02, lambda: self.client.sendall(startup()))
        sender.start()
        try:
            self.phase = serve_message(self.server, self.engine, self.phase, 1.0)
        finally:
            sender.join()
        self.assertEqual(self.phase, SessionPhase.READY)
        self.assertEqual(reply(self.client)[-1], (b"Z", b"I"))

    def test_trickled_bytes_do_not_restart_input_deadline(self):
        self.authenticate()
        self.client.sendall(b"Q" + U32.pack(100) + b"x")
        stopped = threading.Event()

        def trickle():
            for _ in range(20):
                if stopped.wait(0.03):
                    return
                self.client.sendall(b"x")

        sender = threading.Thread(target=trickle)
        sender.start()
        start = time.monotonic()
        try:
            outcome = serve_message(self.server, self.engine, self.phase, 0.15)
        finally:
            elapsed = time.monotonic() - start
            stopped.set()
            sender.join()
        self.assertIsNone(outcome)
        self.assertLess(elapsed, 0.45)
        self.assertEqual(self.server.gettimeout(), 0.0)

    def test_write_inactivity_timeout_and_continuous_progress(self):
        self.authenticate()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        self.engine.rows = [["42", "x" * 4096]] * 32
        request = frame(b"Q", b"SELECT * FROM users\0")
        self.client.sendall(request)
        outcome = []
        task = threading.Thread(target=lambda: outcome.append(
            serve_message(self.server, self.engine, self.phase, 0.15)))
        start = time.monotonic()
        task.start()
        try:
            received = []
            while not received or received[-1][0] != b"Z":
                received.append(read_frame(self.client))
                time.sleep(0.01)
        finally:
            task.join(2)
        self.assertFalse(task.is_alive())
        self.assertEqual(outcome, [SessionPhase.READY])
        self.assertGreater(time.monotonic() - start, 0.15)
        self.assertEqual(sum(kind == b"D" for kind, _ in received), 32)
        self.assertIsNone(self.process(request, timeout=0.1))


if __name__ == "__main__":
    unittest.main()
