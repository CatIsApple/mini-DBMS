"""Readiness-based TCP server with a fixed pool of message workers."""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, field
import errno
import queue
import selectors
import signal
import socket
import sys
import threading
import time

from .engine import Engine
from .errors import DbError
from .pgwire import SessionPhase, serve_message


@dataclass(eq=False)
class Client:
    socket: socket.socket
    phase: SessionPhase = SessionPhase.STARTUP
    last_activity: float = field(default_factory=time.monotonic)
    state: str = "idle"


class WorkerPool:
    """Workers exchange jobs/results through queues; the reactor owns Client state."""

    def __init__(self, engine: Engine, count: int, capacity: int, wake, io_timeout: float):
        self.engine = engine
        self.jobs = queue.Queue(maxsize=capacity)
        self.completed = queue.SimpleQueue()
        self.wake = wake
        self.io_timeout = io_timeout
        self.stopping = threading.Event()
        self.threads = []
        try:
            for number in range(count):
                thread = threading.Thread(target=self._run, name=f"datom-worker-{number}")
                thread.start()
                self.threads.append(thread)
        except BaseException:
            self.close()
            raise

    def _run(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            self.wake()  # There is room for another ready connection in the queue.
            if self.stopping.is_set():
                continue
            client, phase = job
            try:
                next_phase = serve_message(client.socket, self.engine, phase, self.io_timeout)
            except Exception as error:
                # The protocol handles ordinary SQL and connection errors itself.
                print(f"connection failed: {error}", file=sys.stderr, flush=True)
                next_phase = None
            self.completed.put((client, next_phase))
            self.wake()

    def submit(self, client: Client) -> bool:
        try:
            self.jobs.put_nowait((client, client.phase))
            return True
        except queue.Full:
            return False

    def close(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        while True:
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                break
        for _ in self.threads:
            self.jobs.put(None)
        for thread in self.threads:
            thread.join()


def _listen(host: str, port: int) -> socket.socket:
    last_error = None
    for family, kind, protocol, _, address in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM):
        listener = socket.socket(family, kind, protocol)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(address)
            listener.listen()
            listener.setblocking(False)
            return listener
        except OSError as error:
            last_error = error
            listener.close()
    raise last_error or OSError("no suitable listen address")


class Server:
    """Call stop() from another thread; serve_forever() owns cleanup."""

    def __init__(self, engine: Engine, host="127.0.0.1", port=5433,
                 workers=4, queue_size=64, idle_timeout=60.0, io_timeout=10.0):
        if not 1 <= workers <= 256 or not 1 <= queue_size <= 65535:
            raise ValueError("invalid worker or queue count")
        if idle_timeout <= 0 or io_timeout <= 0:
            raise ValueError("timeouts must be positive")
        self.engine = engine
        self.worker_count = workers
        self.queue_size = queue_size
        self.limit = max(256, workers + queue_size)
        self.idle_timeout = idle_timeout
        self.io_timeout = io_timeout
        self.stopping = threading.Event()
        self.signal_stopped = False
        self.clients = {}
        self.pending = deque()
        self.pool = None
        self.selector = selectors.DefaultSelector()
        self.listener = None
        self.wake_reader = None
        self.wake_writer = None
        self.closed = False
        self.accept_after = None
        try:
            self.listener = _listen(host, port)
            self.address = self.listener.getsockname()
            self.wake_reader, self.wake_writer = socket.socketpair()
            self.wake_reader.setblocking(False)
            self.wake_writer.setblocking(False)
            self.selector.register(self.listener, selectors.EVENT_READ, "listen")
            self.selector.register(self.wake_reader, selectors.EVENT_READ, "wake")
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _wake(self):
        if self.wake_writer is None:
            return
        try:
            self.wake_writer.send(b"\0")
        except (BlockingIOError, OSError):
            pass  # A full socket already has a wakeup waiting.

    def stop(self):
        self.stopping.set()
        self._wake()

    def handle_signal(self, *_):
        # Python runs this handler on the reactor thread. Avoid acquiring locks
        # that its interrupted code might already hold.
        self.signal_stopped = True
        self._wake()

    def _should_stop(self):
        return self.signal_stopped or self.stopping.is_set()

    def _drain_wakeups(self):
        try:
            while self.wake_reader.recv(4096):
                pass
        except BlockingIOError:
            pass

    def _drop(self, client: Client):
        if client.socket not in self.clients:
            return
        self.clients.pop(client.socket)
        if client.state == "idle":
            self.selector.unregister(client.socket)
        client.socket.close()

    def _accept(self):
        for _ in range(64):
            try:
                conn, _ = self.listener.accept()
            except BlockingIOError:
                return
            except OSError as error:
                if error.errno in (errno.ECONNABORTED, errno.EINTR):
                    continue
                if error.errno in (errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM):
                    self.selector.unregister(self.listener)
                    self.accept_after = time.monotonic() + 1.0
                    return
                raise
            if len(self.clients) >= self.limit:
                conn.close()
                continue
            try:
                conn.setblocking(False)
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                client = Client(conn)
                self.selector.register(conn, selectors.EVENT_READ, client)
                self.clients[conn] = client
            except BaseException:
                conn.close()
                raise

    def _finish(self):
        while True:
            try:
                client, phase = self.pool.completed.get_nowait()
            except queue.Empty:
                return
            if phase is None:
                self._drop(client)
            else:
                client.phase = phase
                client.last_activity = time.monotonic()
                client.state = "idle"
                self.selector.register(client.socket, selectors.EVENT_READ, client)

    def _dispatch(self):
        while self.pending:
            client = self.pending[0]
            if not self.pool.submit(client):
                return
            self.pending.popleft()
            client.state = "working"

    def serve_forever(self):
        if self.closed:
            raise RuntimeError("server is closed")
        try:
            self.pool = WorkerPool(self.engine, self.worker_count, self.queue_size,
                                   self._wake, self.io_timeout)
            host, port = self.address[:2]
            endpoint = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
            print(f"listening on {endpoint}", flush=True)
            while not self._should_stop():
                self._finish()
                self._dispatch()
                now = time.monotonic()
                if self.accept_after is not None and now >= self.accept_after:
                    self.selector.register(self.listener, selectors.EVENT_READ, "listen")
                    self.accept_after = None
                for client in list(self.clients.values()):
                    if client.state == "idle" and now - client.last_activity >= self.idle_timeout:
                        self._drop(client)
                for key, _ in self.selector.select(min(1.0, self.idle_timeout)):
                    if self._should_stop():
                        break
                    if key.data == "wake":
                        self._drain_wakeups()
                    elif key.data == "listen":
                        self._accept()
                    else:
                        client = key.data
                        self.selector.unregister(client.socket)
                        client.state = "pending"
                        self.pending.append(client)
        finally:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stopping.set()
        # Interrupt partial reads and stalled writes, then wait before closing FDs.
        for conn in self.clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.pool is not None:
            self.pool.close()
        for conn in self.clients:
            conn.close()
        self.clients.clear()
        self.pending.clear()
        self.selector.close()
        for conn in (self.listener, self.wake_reader, self.wake_writer):
            if conn is not None:
                conn.close()


def _number(minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("expected an integer") from None
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"expected {minimum} through {maximum}")
        return number
    return parse


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_number(0, 65535), default=5433)
    parser.add_argument("--data", default="./data")
    parser.add_argument("--workers", type=_number(1, 256), default=4)
    parser.add_argument("--queue", type=_number(1, 65535), default=64)
    options = parser.parse_args(argv)
    try:
        with Engine(options.data) as engine:
            with Server(engine, options.host, options.port, options.workers, options.queue) as server:
                previous = {}
                try:
                    for signum in (signal.SIGINT, signal.SIGTERM):
                        previous[signum] = signal.signal(signum, server.handle_signal)
                    server.serve_forever()
                finally:
                    for signum, handler in previous.items():
                        signal.signal(signum, handler)
    except DbError as error:
        print(f"ERROR [{error.code}] {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as error:
        print(f"ERROR {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
