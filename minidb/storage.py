"""File-backed table: append-only JSON-lines file + in-memory B+ tree (id -> byte offset).

Record format, one per line:  [id,"col1",col2,...]\n
- INSERT appends; the index maps id -> offset so `WHERE id = ?` is a seek, not a scan.
- Index is rebuilt from the file on open (a torn last line from a crash is dropped).
- Concurrency: many readers or one writer (RWLock); readers use their own file handle.
"""
import json
import os
import threading
from contextlib import contextmanager
from itertools import islice

from .bptree import BPlusTree
from .errors import ExecutionError, StorageError


class RWLock:
    """Writer-preferring readers-writer lock (not reentrant)."""

    def __init__(self):
        self._c = threading.Condition()
        self._readers = 0
        self._writing = False
        self._waiting_writers = 0

    @contextmanager
    def read(self):
        with self._c:
            while self._writing or self._waiting_writers:
                self._c.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._c:
                self._readers -= 1
                if not self._readers:
                    self._c.notify_all()

    @contextmanager
    def write(self):
        with self._c:
            self._waiting_writers += 1
            while self._writing or self._readers:
                self._c.wait()
            self._waiting_writers -= 1
            self._writing = True
        try:
            yield
        finally:
            with self._c:
                self._writing = False
                self._c.notify_all()


class Table:
    def __init__(self, name, columns, path, fsync=False):
        """columns: ordered {name: type} WITHOUT the auto `id` column."""
        self.name = name
        self.user_cols = tuple(columns)
        self.types = ("INT", *columns.values())  # aligned with self.columns
        self.columns = ("id", *self.user_cols)
        self.path, self.fsync = path, fsync
        self.index = BPlusTree()
        self.lock = RWLock()
        self._tls = threading.local()
        self._handles, self._handles_lock = [], threading.Lock()
        self.next_id = self._load()
        self._w = open(path, "ab")
        self._size = os.path.getsize(path)

    def _load(self):
        last = off = 0
        if os.path.exists(self.path):
            with open(self.path, "rb") as f:
                for line in f:
                    if not line.endswith(b"\n"):
                        break  # torn write
                    try:
                        rid = int(line[1:line.index(b",")])
                    except ValueError:
                        raise StorageError(f"{self.path}: corrupt record at byte {off}") from None
                    if rid <= last:
                        raise StorageError(f"{self.path}: ids not ascending at byte {off}")
                    self.index.insert(rid, off)  # shortcut: per-row insert, bulk-build would be faster
                    last, off = rid, off + len(line)
            if off != os.path.getsize(self.path):
                os.truncate(self.path, off)
        return last + 1

    def __len__(self):
        return len(self.index)

    def insert_many(self, rows):
        """rows: validated value lists (no id). All-or-nothing. Returns (first_id, last_id)."""
        with self.lock.write():
            first, pos = self.next_id, self._size
            lines, offsets = [], []
            for n, r in enumerate(rows):
                try:
                    line = (json.dumps([first + n, *r], ensure_ascii=False,
                                       separators=(",", ":")) + "\n").encode("utf-8")
                except UnicodeEncodeError:
                    raise ExecutionError("text is not valid Unicode (lone surrogate)") from None
                lines.append(line)
                offsets.append(pos)
                pos += len(line)
            # shortcut: no rollback if the write itself fails (disk full); reopen truncates torn tail
            self._w.write(b"".join(lines))
            self._w.flush()
            if self.fsync:
                os.fsync(self._w.fileno())
            self._size, self.next_id = pos, first + len(rows)
            for n, off in enumerate(offsets):
                self.index.insert(first + n, off)
            return first, first + len(rows) - 1

    def _reader(self):
        f = getattr(self._tls, "f", None)
        if f is None:
            f = self._tls.f = open(self.path, "rb")
            with self._handles_lock:
                self._handles.append(f)
        return f

    def _rows(self, plan, pred):
        f = self._reader()
        if plan[0] == "scan":
            f.seek(0)
            for line in f:
                row = json.loads(line)
                if pred is None or pred(row):
                    yield row
            return
        if plan[0] == "lookup":
            off = self.index.search(plan[1])
            offsets = () if off is None else (off,)
        else:  # ("range", lo, hi, lo_inclusive, hi_inclusive)
            offsets = (o for _, o in self.index.items(*plan[1:]))
        for off in offsets:
            f.seek(off)
            row = json.loads(f.readline())
            if pred is None or pred(row):
                yield row

    def select(self, plan, pred, limit=None):
        with self.lock.read():
            it = self._rows(plan, pred)
            return list(it if limit is None else islice(it, limit))

    def count(self, plan, pred):
        with self.lock.read():
            return sum(1 for _ in self._rows(plan, pred))

    def close(self):
        with self.lock.write():
            self._w.close()
            with self._handles_lock:
                for f in self._handles:
                    f.close()
                self._handles.clear()
            self._tls = threading.local()
