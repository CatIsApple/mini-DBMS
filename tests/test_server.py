"""Exercise real TCP scheduling independently of SQL/protocol parsing."""
from contextlib import contextmanager
import select
import socket
import threading
import time
import unittest
from unittest.mock import patch

from datom.pgwire import SessionPhase
from datom.server import Server


def read_byte(conn, timeout=3):
    if not select.select([conn], [], [], timeout)[0]:
        raise TimeoutError("test message did not arrive")
    return conn.recv(1)


class ServerTests(unittest.TestCase):
    @contextmanager
    def running(self, handler, workers=2, queue_size=2, idle_timeout=60):
        failures = []
        with patch("datom.server.serve_message", side_effect=handler):
            with Server(None, port=0, workers=workers, queue_size=queue_size,
                        idle_timeout=idle_timeout) as server:
                def run():
                    try:
                        server.serve_forever()
                    except BaseException as error:
                        failures.append(error)

                thread = threading.Thread(target=run, name="test-reactor")
                thread.start()
                try:
                    yield server
                finally:
                    server.stop()
                    thread.join(4)
                    self.assertFalse(thread.is_alive(), "server did not stop")
                    self.assertFalse(failures, failures)

    @staticmethod
    def connect(server):
        return socket.create_connection(server.address[:2], timeout=3)

    def test_idle_sessions_do_not_occupy_workers_and_requests_overlap(self):
        together = threading.Barrier(2, timeout=3)
        thread_names = set()
        guard = threading.Lock()

        def handler(conn, _engine, _phase, _timeout):
            value = read_byte(conn)
            if not value:
                return None
            with guard:
                thread_names.add(threading.current_thread().name)
            together.wait()
            conn.sendall(value)
            return SessionPhase.READY

        with self.running(handler) as server:
            idle = [self.connect(server) for _ in range(8)]
            try:
                with self.connect(server) as first, self.connect(server) as second:
                    first.sendall(b"a")
                    second.sendall(b"b")
                    self.assertEqual(first.recv(1), b"a")
                    self.assertEqual(second.recv(1), b"b")
                self.assertEqual(len(thread_names), 2)
                self.assertTrue(all(name.startswith("datom-worker-") for name in thread_names))
            finally:
                for conn in idle:
                    conn.close()

    def test_bounded_queue_preserves_message_order(self):
        active = set()
        guard = threading.Lock()
        overlaps = []

        def handler(conn, _engine, phase, _timeout):
            value = read_byte(conn)
            if not value:
                return None
            with guard:
                if conn in active:
                    overlaps.append(conn)
                active.add(conn)
            time.sleep(0.005)
            conn.sendall(value)
            with guard:
                active.remove(conn)
            return SessionPhase.READY

        with self.running(handler, workers=2, queue_size=1) as server:
            clients = [self.connect(server) for _ in range(8)]
            try:
                for conn in clients:
                    conn.sendall(b"abcd")
                for conn in clients:
                    received = bytearray()
                    while len(received) < 4:
                        chunk = conn.recv(4 - len(received))
                        self.assertTrue(chunk)
                        received.extend(chunk)
                    self.assertEqual(received, b"abcd")
                self.assertEqual(overlaps, [])
            finally:
                for conn in clients:
                    conn.close()

    def test_shutdown_interrupts_a_worker_waiting_for_partial_input(self):
        entered = threading.Event()

        def handler(conn, _engine, _phase, _timeout):
            self.assertEqual(read_byte(conn), b"a")
            entered.set()
            self.assertEqual(read_byte(conn, timeout=10), b"")
            return None

        with self.running(handler, workers=1, queue_size=1) as server:
            with self.connect(server) as conn:
                conn.sendall(b"a")
                self.assertTrue(entered.wait(3))
                started = time.monotonic()
                server.stop()
                self.assertEqual(conn.recv(1), b"")
                self.assertLess(time.monotonic() - started, 2)

    def test_worker_start_failure_joins_started_threads(self):
        original_start = threading.Thread.start
        started = []

        def start(thread):
            if thread.name == "datom-worker-1":
                raise RuntimeError("injected thread creation failure")
            original_start(thread)
            started.append(thread)

        with Server(None, port=0, workers=2) as server:
            with patch.object(threading.Thread, "start", start):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    server.serve_forever()
            self.assertTrue(started)
            self.assertTrue(all(not thread.is_alive() for thread in started))
            self.assertEqual(server.listener.fileno(), -1)

    def test_idle_session_is_closed(self):
        def handler(*_):
            raise AssertionError("idle connection received a worker")

        with self.running(handler, idle_timeout=0.1) as server:
            with self.connect(server) as conn:
                self.assertEqual(conn.recv(1), b"")


if __name__ == "__main__":
    unittest.main()
