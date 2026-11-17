import http.client
import json
import socket
import threading
import time

from minidb.server import PoolHTTPServer
from tests.helpers import DBTestCase


class ServerTest(DBTestCase):
    workers, queue_size = 4, 16

    def setUp(self):
        super().setUp()
        self.srv = PoolHTTPServer(("127.0.0.1", 0), self.db, self.workers, self.queue_size)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, method, path, body=None, raw=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = raw if raw is not None else (json.dumps(body) if body is not None else None)
        c.request(method, path, body=data, headers=headers or {})
        r = c.getresponse()
        out = json.loads(r.read())
        c.close()
        return r.status, out

    def sql(self, text):
        return self.call("POST", "/query", {"sql": text})

    def test_health_and_tables(self):
        s, b = self.call("GET", "/health")
        self.assertEqual((s, b["status"], b["workers"]), (200, "ok", self.workers))
        s, b = self.call("GET", "/tables")
        self.assertEqual(b["tables"]["users"], {"id": "INT", "name": "TEXT", "age": "INT", "score": "FLOAT"})

    def test_insert_and_select(self):
        s, b = self.sql("INSERT INTO users (name, age) VALUES ('한글', 3), ('b', 4)")
        self.assertEqual(s, 200)
        self.assertEqual(b["results"][0], {"kind": "insert", "affected": 2, "first_id": 1, "last_id": 2})
        s, b = self.sql("SELECT name FROM users WHERE id = 1; SELECT COUNT(*) FROM users")
        self.assertEqual(b["results"][0]["rows"], [["한글"]])
        self.assertIn("INDEX LOOKUP", b["results"][0]["plan"])
        self.assertEqual(b["results"][1]["rows"], [[2]])

    def test_sql_errors_are_400(self):
        s, b = self.sql("SELEC 1")
        self.assertEqual((s, b["type"]), (400, "ParseError"))
        s, b = self.sql("INSERT INTO users (age) VALUES (1); SELECT * FROM nope")
        self.assertEqual((s, b["type"], b["completed"]), (400, "ExecutionError", 1))

    def test_bad_requests(self):
        self.assertEqual(self.call("POST", "/query", raw="not json")[0], 400)
        self.assertEqual(self.call("POST", "/query", {"nosql": 1})[0], 400)
        self.assertEqual(self.call("POST", "/query", {"sql": 5})[0], 400)
        self.assertEqual(self.call("POST", "/query", raw="[1]")[0], 400)
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("POST", "/nope", {})[0], 404)
        self.assertEqual(self.call("GET", "/query")[0], 405)
        self.assertEqual(self.call("DELETE", "/query")[0], 405)
        s, _ = self.call("POST", "/query", headers={"Content-Length": str(9 * 1024 * 1024)})
        self.assertEqual(s, 413)

    def test_concurrent_inserts_unique_ids_and_consistent_index(self):
        threads_n, per = 16, 40
        ids, errors = [], []

        def work(t):
            try:
                for i in range(per):
                    s, b = self.sql(f"INSERT INTO users (name, age) VALUES ('t{t}', {i})")
                    assert s == 200, b
                    ids.append(b["results"][0]["first_id"])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=work, args=(t,)) for t in range(threads_n)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        total = threads_n * per
        self.assertEqual(sorted(ids), list(range(1, total + 1)))
        _, b = self.sql("SELECT COUNT(*) FROM users")
        self.assertEqual(b["results"][0]["rows"], [[total]])
        for i in (1, total // 2, total):  # index agrees with file
            _, b = self.sql(f"SELECT id FROM users WHERE id = {i}")
            self.assertEqual(b["results"][0]["rows"], [[i]])
        self.db.tables["users"].index.check()

    def test_readers_during_writes_never_see_torn_rows(self):
        stop, bad = threading.Event(), []

        def writer():
            for i in range(150):
                self.sql(f"INSERT INTO users (name, age) VALUES ('w', {i}), ('w', {i})")
            stop.set()

        def reader():
            while not stop.is_set():
                s, b = self.sql("SELECT * FROM users WHERE name = 'w'")
                if s != 200 or any(len(r) != 4 or r[1] != "w" for r in b["results"][0]["rows"]):
                    bad.append((s, b))
                s, b = self.sql("SELECT * FROM users WHERE id > 5 LIMIT 20")
                if s != 200:
                    bad.append((s, b))

        ts = [threading.Thread(target=reader) for _ in range(3)] + [threading.Thread(target=writer)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(bad, [])

    def test_requests_run_in_parallel_on_pool(self):
        # a stalled client occupies one worker; others must still be served
        stalled = socket.create_connection(("127.0.0.1", self.port))
        self.addCleanup(stalled.close)
        stalled.sendall(b"POST /query HTTP/1.0\r\nContent-Length: 100\r\n\r\n")
        time.sleep(0.2)
        t0 = time.time()
        self.assertEqual(self.call("GET", "/health")[0], 200)
        self.assertLess(time.time() - t0, 2)

    def test_malformed_http_does_not_kill_server(self):
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall(b"\x00\xff garbage\r\n\r\n")
        s.close()
        self.assertEqual(self.call("GET", "/health")[0], 200)


class BackPressureTest(ServerTest):
    workers, queue_size = 1, 1

    def test_queue_full_returns_503(self):
        hold = []
        for _ in range(2):  # 1st pins the only worker, 2nd fills the queue
            c = socket.create_connection(("127.0.0.1", self.port))
            hold.append(c)
            time.sleep(0.3)
        c3 = socket.create_connection(("127.0.0.1", self.port))
        hold.append(c3)
        c3.settimeout(5)
        self.assertIn(b"503", c3.recv(4096).split(b"\r\n")[0])
        for c in hold:
            c.close()
        time.sleep(0.3)
        self.assertEqual(self.call("GET", "/health")[0], 200)  # recovers

    # inherited tests assume >1 worker; skip the ones that would deadlock on 1 worker
    test_requests_run_in_parallel_on_pool = None
    test_concurrent_inserts_unique_ids_and_consistent_index = None
    test_readers_during_writes_never_see_torn_rows = None
