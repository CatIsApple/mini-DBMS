"""Append-only table files, statement snapshots, and in-memory ID indexes.

A DTX1 frame is one INSERT statement. Its bytes match the original C++ format:
little-endian magic/length/count/FNV checksums, packed rows, then CMT1. Recovery
keeps complete frames and truncates an interrupted final frame.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import errno
import fcntl
import os
from pathlib import Path
import re
import stat
import struct
import threading
from typing import Callable, Optional

from .bptree import BPlusTree
from .errors import DbError
from .sql import Statement


MAX_PAYLOAD = 64 * 1024 * 1024
MAX_ID = (1 << 63) - 1
_HEADER = struct.Struct("<4sIIII")
_HEADER_PREFIX = struct.Struct("<4sIII")
_ROW = struct.Struct("<QII")
_MAGIC = b"DTX1"
_COMMIT = b"CMT1"
_SCHEMAS = {"users": ("id", "name", "email"), "products": ("id", "name", "sku")}
_ID_PATTERN = re.compile(r"[+-]?[0-9]+\Z")


class AccessPath(Enum):
    NONE = "none"
    SEQUENTIAL_SCAN = "sequential_scan"
    INDEX_LOOKUP = "index_lookup"


@dataclass
class Result:
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    tag: str = ""
    access: AccessPath = AccessPath.NONE
    rows_examined: int = 0


@dataclass
class ResultSink:
    columns: Callable[[list[str]], None]
    row: Callable[[list[str]], None]


def _io_error(operation: str, error: OSError) -> DbError:
    return DbError("58030", f"{operation}: {error.strerror or str(error)}")


def _corrupt(offset: int) -> None:
    raise DbError("XX001", f"invalid table data at byte {offset}")


def _checksum(data) -> int:
    value = 2166136261
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def _sync(fd: int) -> None:
    while True:
        try:
            os.fsync(fd)
            return
        except InterruptedError:
            continue


def _truncate(fd: int, size: int) -> None:
    while True:
        try:
            os.ftruncate(fd, size)
            break
        except InterruptedError:
            continue
    _sync(fd)


def _write_all(fd: int, offset: int, data) -> None:
    with memoryview(data) as view:
        position = 0
        while position < len(view):
            try:
                count = os.pwrite(fd, view[position:], offset + position)
            except InterruptedError:
                continue
            if count <= 0:
                raise OSError(errno.EIO, "no progress writing table")
            position += count


def _read_exact(fd: int, offset: int, size: int) -> bytes:
    parts = []
    remaining = size
    while remaining:
        try:
            part = os.pread(fd, remaining, offset)
        except InterruptedError:
            continue
        if not part:
            _corrupt(offset)
        parts.append(part)
        offset += len(part)
        remaining -= len(part)
    return b"".join(parts)


def _scan(fd: int, size: int, verify: bool, visit: Callable) -> int:
    """Visit complete rows and return the last complete frame boundary."""
    start = 0
    while start < size:
        header = _read_exact(fd, start, min(_HEADER.size, size - start))
        prefix_size = min(len(header), len(_MAGIC))
        if header[:prefix_size] != _MAGIC[:prefix_size]:
            _corrupt(start)
        if len(header) < _HEADER.size:
            break
        _, payload_size, count, payload_hash, header_hash = _HEADER.unpack(header)
        if header_hash != _checksum(header[:_HEADER_PREFIX.size]):
            _corrupt(start)
        if not count or payload_size > MAX_PAYLOAD or count > payload_size // _ROW.size:
            _corrupt(start)
        total = _HEADER.size + payload_size + len(_COMMIT)
        if total > size - start:
            break
        packed = _read_exact(fd, start + _HEADER.size, payload_size + len(_COMMIT))
        if packed[payload_size:] != _COMMIT:
            _corrupt(start)
        if verify and _checksum(memoryview(packed)[:payload_size]) != payload_hash:
            _corrupt(start)
        position = 0
        for _ in range(count):
            offset = start + _HEADER.size + position
            if payload_size - position < _ROW.size:
                _corrupt(offset)
            key, first_size, second_size = _ROW.unpack_from(packed, position)
            first = position + _ROW.size
            second = first + first_size
            end = second + second_size
            if end > payload_size:
                _corrupt(offset)
            visit(key, packed, first, second, end, offset)
            position = end
        if position != payload_size:
            _corrupt(start)
        start += total
    return start


def _parse_id(value: str) -> int:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise DbError("22P02", "invalid BIGINT id")
    # Reject enormous decimal strings before int()'s implementation-specific cap.
    digits = value.lstrip("+-").lstrip("0")
    if len(digits) > 19:
        raise DbError("22003", "id outside BIGINT range")
    result = int(("-" if value.startswith("-") else "") + (digits or "0"))
    if not -(1 << 63) <= result <= MAX_ID:
        raise DbError("22003", "id outside BIGINT range")
    return result


def _encode(value: str) -> bytes:
    if value is None:
        raise DbError("23502", "NULL values are unsupported")
    if not isinstance(value, str):
        raise DbError("42804", "expected a text value")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DbError("22021", "invalid UTF-8 text") from error


class _Table:
    def __init__(self, directory: Path, name: str, columns: tuple[str, str, str]):
        self.name = name
        self.columns = columns
        self.lock = threading.Lock()
        self.fd = -1
        self.available = True
        self.committed = 0
        self.last_id = 0
        self.index = BPlusTree()
        try:
            self.fd = os.open(directory / f"{name}.tbl", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise DbError("55006", "database directory is already in use") from error
            self._rebuild()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.fd != -1:
            fd, self.fd = self.fd, -1
            os.close(fd)

    def _rebuild(self) -> None:
        index = BPlusTree()
        latest = 0
        status = os.fstat(self.fd)
        if not stat.S_ISREG(status.st_mode):
            raise DbError("58030", "table must be a regular file")

        def add(key, packed, first, second, end, offset):
            nonlocal latest
            if key != latest + 1 or key > MAX_ID:
                _corrupt(offset)
            index.insert(key, offset)
            latest = key

        end = _scan(self.fd, status.st_size, True, add)
        if end != status.st_size:
            _truncate(self.fd, end)
        self.index = index
        self.last_id = latest
        self.committed = end

    def _check_available(self) -> None:
        if not self.available:
            raise DbError("58030", "table requires reopening after a failed write")

    def _column(self, name: str) -> int:
        try:
            return self.columns.index(name)
        except ValueError as error:
            raise DbError("42703", f"unknown column: {name}") from error

    def select(self, statement: Statement, sink: Optional[ResultSink]) -> Result:
        if len(statement.columns) > 32767:
            raise DbError("54000", "too many output columns")
        result = Result(columns=list(statement.columns or self.columns))
        projection = [self._column(name) for name in result.columns]
        filter_column = self._column(statement.where.column) if statement.where else None
        by_id = filter_column == 0
        key = _parse_id(statement.where.value) if by_id else 0
        match = _encode(statement.where.value) if statement.where and not by_id else None
        result.access = AccessPath.INDEX_LOOKUP if by_id else AccessPath.SEQUENTIAL_SCAN
        with self.lock:
            self._check_available()
            snapshot_end = self.committed
            offset = self.index.find(key) if by_id and key > 0 else None

        # Everything below reads the immutable committed prefix. Callbacks may
        # block or insert into the same table without holding its write lock.
        if sink:
            sink.columns(result.columns.copy())
        returned = 0

        def emit(row_id, packed, first, second, end):
            nonlocal returned
            spans = ((None, None), (first, second), (second, end))
            id_text = str(row_id)
            size = sum(len(id_text) if col == 0 else spans[col][1] - spans[col][0]
                       for col in projection)
            if size > MAX_PAYLOAD:
                raise DbError("54000", "projected row exceeds 64 MiB")
            values = {0: id_text}
            output = []
            for col in projection:
                if col not in values:
                    begin, finish = spans[col]
                    try:
                        values[col] = packed[begin:finish].decode("utf-8")
                    except UnicodeDecodeError as error:
                        raise DbError("22021", "invalid UTF-8 text in table") from error
                output.append(values[col])
            if sink:
                sink.row(output)
            else:
                result.rows.append(output)
            returned += 1

        if offset is not None:
            if offset > snapshot_end or snapshot_end - offset < _ROW.size:
                _corrupt(offset)
            head = _read_exact(self.fd, offset, _ROW.size)
            stored_id, first_size, second_size = _ROW.unpack(head)
            size = first_size + second_size
            if stored_id != key or size > MAX_PAYLOAD or size > snapshot_end - offset - _ROW.size:
                _corrupt(offset)
            packed = _read_exact(self.fd, offset + _ROW.size, size)
            emit(key, packed, 0, first_size, size)
            result.rows_examined = 1
        elif not by_id:
            def visit(row_id, packed, first, second, end, row_offset):
                result.rows_examined += 1
                if filter_column == 1 and packed[first:second] != match:
                    return
                if filter_column == 2 and packed[second:end] != match:
                    return
                emit(row_id, packed, first, second, end)

            if _scan(self.fd, snapshot_end, False, visit) != snapshot_end:
                _corrupt(snapshot_end)
        result.tag = f"SELECT {returned}"
        return result

    def insert(self, statement: Statement) -> Result:
        first_at, second_at = 0, 1
        if statement.columns:
            positions = {}
            for position, name in enumerate(statement.columns):
                column = self._column(name)
                if column == 0:
                    raise DbError("428C9", "id is generated automatically")
                if column in positions:
                    raise DbError("42701", f"duplicate target column: {name}")
                positions[column] = position
            if len(positions) != 2:
                raise DbError("23502", f"{self.columns[1]} and {self.columns[2]} are both required")
            first_at, second_at = positions[1], positions[2]
        if not statement.values:
            raise DbError("42601", "INSERT needs at least one row")
        encoded = []
        payload_size = 0
        for values in statement.values:
            if len(values) != 2:
                raise DbError("42601", "expected two values per row")
            first, second = _encode(values[first_at]), _encode(values[second_at])
            payload_size += _ROW.size + len(first) + len(second)
            if payload_size > MAX_PAYLOAD:
                raise DbError("54000", "INSERT payload exceeds 64 MiB")
            encoded.append((first, second))
        frame = bytearray(_HEADER.size + payload_size + len(_COMMIT))
        result = Result(tag=f"INSERT 0 {len(encoded)}")
        with self.lock:
            self._check_available()
            if len(encoded) > MAX_ID - self.last_id:
                raise DbError("22003", "id space exhausted")
            if len(frame) > MAX_ID - self.committed:
                raise DbError("54000", "table size limit exceeded")
            key = self.last_id
            position = _HEADER.size
            offsets = []
            for first, second in encoded:
                key += 1
                offsets.append(self.committed + position)
                _ROW.pack_into(frame, position, key, len(first), len(second))
                position += _ROW.size
                frame[position:position + len(first)] = first
                position += len(first)
                frame[position:position + len(second)] = second
                position += len(second)
            payload_hash = _checksum(memoryview(frame)[_HEADER.size:position])
            _HEADER_PREFIX.pack_into(frame, 0, _MAGIC, payload_size, len(encoded), payload_hash)
            struct.pack_into("<I", frame, _HEADER_PREFIX.size,
                             _checksum(memoryview(frame)[:_HEADER_PREFIX.size]))
            frame[position:] = _COMMIT
            new_end = self.committed + len(frame)
            touched_index = False
            try:
                _write_all(self.fd, self.committed, frame)
                _sync(self.fd)
                touched_index = True
                for row_id, offset in enumerate(offsets, self.last_id + 1):
                    self.index.insert(row_id, offset)
            except BaseException:
                try:
                    _truncate(self.fd, self.committed)
                    if touched_index:
                        self._rebuild()
                except BaseException:
                    self.available = False
                    raise
                raise
            self.committed = new_end
            self.last_id = key
        return result


class Engine:
    """Thread-safe engine. Keep it open until its server workers have finished."""

    def __init__(self, directory):
        self._condition = threading.Condition()
        self._local = threading.local()
        self._active = 0
        self._closing = False
        self._closed = False
        self._tables = {}
        try:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            # Consistent lock order prevents different engines from acquiring
            # different halves of a database directory.
            for name, columns in _SCHEMAS.items():
                self._tables[name] = _Table(directory, name, columns)
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                _sync(fd)
            finally:
                os.close(fd)
        except BaseException as error:
            self.close()
            if isinstance(error, OSError):
                raise _io_error("open database", error) from error
            raise

    def __enter__(self) -> Engine:
        with self._condition:
            if self._closed or self._closing:
                raise DbError("55000", "database is closed")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        if getattr(self._local, "depth", 0):
            raise DbError("55006", "cannot close database from an executing statement")
        with self._condition:
            if self._closed:
                return
            self._closing = True
            while self._active:
                self._condition.wait()
            try:
                for table in self._tables.values():
                    table.close()
            finally:
                self._closed = True

    def execute(self, statement: Statement, sink: Optional[ResultSink] = None) -> Result:
        with self._condition:
            if self._closed or self._closing:
                raise DbError("55000", "database is closed")
            self._active += 1
            self._local.depth = getattr(self._local, "depth", 0) + 1
        try:
            if statement.table not in self._tables:
                raise DbError("42P01", f"unknown table: {statement.table}")
            table = self._tables[statement.table]
            if statement.kind == "insert":
                return table.insert(statement)
            if statement.kind == "select":
                return table.select(statement, sink)
            raise DbError("0A000", f"unsupported statement kind: {statement.kind}")
        except OSError as error:
            raise _io_error("table I/O", error) from error
        finally:
            with self._condition:
                self._active -= 1
                self._local.depth -= 1
                self._condition.notify_all()
