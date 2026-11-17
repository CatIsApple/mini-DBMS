"""HTTP API server with a fixed thread pool.

  python -m minidb.server -d data --port 8080 --workers 8

  POST /query    {"sql": "..."}  -> {"results": [...]}   (400 + {"error","type","completed"} on SQL errors)
  GET  /tables                    -> schema
  GET  /health                    -> pool status

Architecture: one accept thread -> bounded queue -> N worker threads, each handling one
connection at a time (HTTP/1.0, no keep-alive). Queue full -> immediate 503 (back-pressure).
All workers share one Database; per-table RW locks provide the concurrency control.
"""
import argparse
import json
import queue
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

from .engine import Database
from .errors import SQLError, StorageError

MAX_BODY = 8 * 1024 * 1024


class PoolHTTPServer(HTTPServer):
    daemon_threads = True

    def __init__(self, addr, db, workers=8, queue_size=64):
        if workers < 1 or queue_size < 1:
            raise ValueError("workers and queue_size must be >= 1")
        super().__init__(addr, Handler)
        self.db = db
        self.n_workers = workers
        self.jobs = queue.Queue(queue_size)
        self.threads = [threading.Thread(target=self._work, name=f"worker-{i}", daemon=True)
                        for i in range(workers)]
        for t in self.threads:
            t.start()

    def process_request(self, request, client_address):  # runs on the accept thread
        try:
            self.jobs.put_nowait((request, client_address))
        except queue.Full:
            try:
                body = b'{"error":"server busy","type":"Busy"}'
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                                b"Retry-After: 1\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
            except OSError:
                pass
            self.shutdown_request(request)

    def _work(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            request, client_address = job
            try:
                self.finish_request(request, client_address)
            except OSError:
                pass  # client vanished or timed out; nothing to report
            except Exception:
                self.handle_error(request, client_address)
            finally:
                self.shutdown_request(request)

    def server_close(self):
        super().server_close()
        for _ in self.threads:
            self.jobs.put(None)
        for t in self.threads:
            t.join(timeout=5)


class Handler(BaseHTTPRequestHandler):
    server_version = "MiniDB/1.0"
    timeout = 10  # a stalled client cannot pin a worker forever

    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, code, msg, typ="BadRequest", **extra):
        self._send(code, {"error": msg, "type": typ, **extra})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            s = self.server
            self._send(200, {"status": "ok", "workers": s.n_workers, "queued": s.jobs.qsize()})
        elif path == "/tables":
            self._send(200, {"tables": self.server.db.schema()})
        elif path == "/query":
            self._err(405, "use POST", "MethodNotAllowed")
        else:
            self._err(404, "not found", "NotFound")

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/query":
            return self._err(404, "not found", "NotFound")
        length = self.headers.get("Content-Length", "")
        if not length.isdigit():
            return self._err(411, "Content-Length header required", "LengthRequired")
        if int(length) > MAX_BODY:
            return self._err(413, f"body larger than {MAX_BODY} bytes", "PayloadTooLarge")
        raw = self.rfile.read(int(length))
        try:
            sql = json.loads(raw)["sql"]
            if not isinstance(sql, str):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return self._err(400, 'body must be JSON like {"sql": "..."}')
        try:
            results = self.server.db.execute(sql)
        except SQLError as e:
            return self._err(400, str(e), type(e).__name__, completed=e.completed)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return self._err(500, "internal error", "InternalError")
        self._send(200, {"results": [r.to_dict() for r in results]})

    def do_PUT(self):
        self._err(405, "method not allowed", "MethodNotAllowed")

    do_DELETE = do_PATCH = do_PUT


def main(argv=None):
    ap = argparse.ArgumentParser(prog="minidb-server")
    ap.add_argument("-d", "--data", default="data")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--queue", type=int, default=64, help="max waiting connections before 503")
    ap.add_argument("--fsync", action="store_true")
    a = ap.parse_args(argv)
    try:
        db = Database(a.data, fsync=a.fsync)
        srv = PoolHTTPServer((a.host, a.port), db, a.workers, a.queue)
    except (StorageError, OSError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"minidb server on http://{a.host}:{srv.server_address[1]}  ({a.workers} workers)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
