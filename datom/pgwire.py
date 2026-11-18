"""PostgreSQL v3 Simple Query messages over a caller-owned TCP connection."""

import math
import select
import socket
import struct
import time
from enum import Enum, auto
from typing import Optional, Sequence

from .engine import Engine, ResultSink
from .errors import DbError
from .sql import iter_statements


PROTOCOL_V3 = 3 << 16
MAX_STARTUP_SIZE = 10_000
MAX_REQUEST_SIZE = 8 * 1024 * 1024
MAX_MESSAGE_PAYLOAD = (1 << 31) - 5


class SessionPhase(Enum):
    STARTUP = auto()
    READY = auto()
    AWAIT_SYNC = auto()


class _Disconnected(Exception):
    pass


def _wait(conn: socket.socket, writable: bool, deadline: float) -> None:
    poller = select.poll()
    try:
        poller.register(conn, select.POLLOUT if writable else select.POLLIN)
    except (OSError, ValueError) as error:
        raise _Disconnected from error
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Disconnected
        try:
            events = poller.poll(math.ceil(remaining * 1000))
        except InterruptedError:
            continue
        except (OSError, ValueError) as error:
            raise _Disconnected from error
        if events:
            return
        raise _Disconnected


def _receive(conn: socket.socket, size: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < size:
        if time.monotonic() >= deadline:
            raise _Disconnected
        try:
            part = conn.recv(size - len(data), socket.MSG_DONTWAIT)
        except InterruptedError:
            continue
        except BlockingIOError:
            _wait(conn, False, deadline)
            continue
        except OSError as error:
            raise _Disconnected from error
        if not part:
            raise _Disconnected
        data.extend(part)
    return bytes(data)


def _send(conn: socket.socket, data: bytes, timeout: float) -> None:
    view = memoryview(data)
    stalled_until = None
    while view:
        try:
            sent = conn.send(view, socket.MSG_DONTWAIT)
        except InterruptedError:
            continue
        except BlockingIOError:
            if stalled_until is None:
                stalled_until = time.monotonic() + timeout
            _wait(conn, True, stalled_until)
            continue
        except OSError as error:
            raise _Disconnected from error
        if sent <= 0:
            raise _Disconnected
        view = view[sent:]
        # Successful progress permits long result streams on a slow connection.
        stalled_until = None


def _payload(conn: socket.socket, maximum: int, deadline: float) -> bytes:
    length, = struct.unpack("!I", _receive(conn, 4, deadline))
    if not 4 <= length <= maximum:
        raise DbError("08P01", "invalid frontend message length")
    return _receive(conn, length - 4, deadline)


def _cstring(data: bytes, offset: int = 0):
    end = data.find(b"\0", offset)
    if end < 0:
        raise DbError("08P01", "unterminated protocol string")
    return data[offset:end], end + 1


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DbError("22021", "invalid UTF8 sequence") from error


class _Reply:
    def __init__(self, conn: socket.socket, timeout: float):
        self.conn = conn
        self.timeout = timeout
        self.buffer = bytearray()

    def frame(self, kind: bytes, payload: bytes = b"") -> None:
        if len(payload) > MAX_MESSAGE_PAYLOAD:
            raise DbError("54000", "response exceeds protocol message size")
        self.buffer.extend(kind)
        self.buffer.extend(struct.pack("!I", len(payload) + 4))
        self.buffer.extend(payload)
        if len(self.buffer) >= 16_384:
            self.flush()

    def flush(self) -> None:
        if self.buffer:
            _send(self.conn, self.buffer, self.timeout)
            self.buffer.clear()

    def error(self, error: DbError, fatal: bool = False) -> None:
        severity = b"FATAL" if fatal else b"ERROR"
        self.frame(
            b"E",
            b"S" + severity + b"\0V" + severity + b"\0C"
            + error.code.encode("ascii") + b"\0M"
            + str(error).encode("utf-8") + b"\0\0",
        )

    def parameter(self, key: str, value: str) -> None:
        self.frame(b"S", key.encode() + b"\0" + value.encode() + b"\0")


def _startup(conn: socket.socket, reply: _Reply, deadline: float) -> Optional[SessionPhase]:
    payload = _payload(conn, MAX_STARTUP_SIZE, deadline)
    if len(payload) < 4:
        raise DbError("08P01", "truncated startup packet")
    version, = struct.unpack_from("!I", payload)
    if version == 80877102:  # CancelRequest has no response.
        return None
    if version in (80877103, 80877104):  # SSLRequest / GSSENCRequest.
        if len(payload) != 4:
            raise DbError("08P01", "invalid encryption request")
        _send(conn, b"N", reply.timeout)
        return SessionPhase.STARTUP
    if version >> 16 != 3:
        raise DbError("0A000", "Datom supports PostgreSQL protocol 3.0")

    offset = 4
    user = database = ""
    unsupported = []
    while True:
        key_bytes, offset = _cstring(payload, offset)
        if not key_bytes:
            break
        value_bytes, offset = _cstring(payload, offset)
        key, value = _decode(key_bytes), _decode(value_bytes)
        if key == "user":
            user = value
        elif key == "database":
            database = value
        elif key.startswith("_pq_."):
            unsupported.append(key_bytes)
        elif key == "client_encoding":
            if value.lower() not in ("utf8", "utf-8", "unicode"):
                raise DbError("22023", "Datom supports UTF8 client encoding")
        elif key != "application_name" and (key != "options" or value):
            raise DbError("0A000", "unsupported startup option: " + key)
    if offset != len(payload):
        raise DbError("08P01", "trailing data in startup packet")
    if not user:
        raise DbError("28000", "startup requires a nonempty user")
    if (database or user) != "datom":
        raise DbError("3D000", "only database datom exists")
    if version != PROTOCOL_V3 or unsupported:
        reply.frame(
            b"v", struct.pack("!II", PROTOCOL_V3, len(unsupported))
            + b"".join(option + b"\0" for option in unsupported),
        )
    reply.frame(b"R", struct.pack("!I", 0))  # AuthenticationOk (trust).
    for key, value in (
        ("server_version", "0.1 (Datom)"),
        ("server_encoding", "UTF8"),
        ("client_encoding", "UTF8"),
        ("standard_conforming_strings", "on"),
        ("integer_datetimes", "on"),
    ):
        reply.parameter(key, value)
    reply.frame(b"Z", b"I")
    return SessionPhase.READY


def _columns(reply: _Reply, columns: Sequence[str]) -> None:
    data = bytearray(struct.pack("!H", len(columns)))
    for column in columns:
        is_id = column == "id"
        data.extend(column.encode("utf-8") + b"\0")
        # Table/attribute OIDs are unknown, int8/text types use text format.
        data.extend(struct.pack("!IhIhih", 0, 0, 20 if is_id else 25, 8 if is_id else -1, -1, 0))
    reply.frame(b"T", data)


def _row(reply: _Reply, row: Sequence[str]) -> None:
    data = bytearray(struct.pack("!H", len(row)))
    for value in row:
        encoded = value.encode("utf-8")
        if len(data) + 4 + len(encoded) > MAX_MESSAGE_PAYLOAD:
            raise DbError("54000", "result row exceeds protocol message size")
        data.extend(struct.pack("!I", len(encoded)))
        data.extend(encoded)
    reply.frame(b"D", data)


def _query(reply: _Reply, engine: Engine, payload: bytes) -> None:
    sql, offset = _cstring(payload)
    if offset != len(payload):
        raise DbError("08P01", "trailing data in Query")
    try:
        statements = iter_statements(_decode(sql))
        statement = next(statements, None)
        if next(statements, None) is not None:
            raise DbError("0A000", "send one SQL statement per Query message")
        if statement is None:
            reply.frame(b"I")
        else:
            if len(statement.columns) > 32767:
                raise DbError("54000", "too many columns for the wire protocol")
            sink = ResultSink(
                columns=lambda columns: _columns(reply, columns),
                row=lambda row: _row(reply, row),
            )
            result = engine.execute(statement, sink=sink)
            reply.frame(b"C", result.tag.encode("utf-8") + b"\0")
    except DbError as error:
        reply.error(error)
    reply.frame(b"Z", b"I")


def _process(
    conn: socket.socket, engine: Engine, phase: SessionPhase, reply: _Reply
) -> Optional[SessionPhase]:
    deadline = time.monotonic() + reply.timeout
    if phase is SessionPhase.STARTUP:
        return _startup(conn, reply, deadline)
    kind = _receive(conn, 1, deadline)
    payload = _payload(conn, MAX_REQUEST_SIZE, deadline)
    if kind in (b"X", b"S", b"H") and payload:
        raise DbError("08P01", "unexpected payload in control message")
    if kind == b"X":
        return None
    if kind == b"S":
        reply.frame(b"Z", b"I")
        return SessionPhase.READY
    if phase is SessionPhase.AWAIT_SYNC or kind == b"H":
        return phase
    if kind == b"Q":
        _query(reply, engine, payload)
    elif kind in (b"P", b"B", b"D", b"C", b"E"):
        reply.error(DbError("0A000", "use the PostgreSQL Simple Query protocol"))
        return SessionPhase.AWAIT_SYNC
    else:
        raise DbError("08P01", "unexpected frontend message type")
    return phase


def serve_message(
    conn: socket.socket, engine: Engine, phase: SessionPhase, io_timeout: float = 10.0
) -> Optional[SessionPhase]:
    """Consume one frame, returning its new session phase or None to disconnect.

    Input uses an absolute deadline for the whole frame. Output uses an inactivity
    timeout and permits continuous result streams of arbitrary duration. The caller
    retains socket ownership and its existing blocking/timeout configuration.
    """
    try:
        reply = _Reply(conn, io_timeout)
        try:
            next_phase = _process(conn, engine, phase, reply)
        except DbError as error:
            reply.error(error, fatal=True)
            next_phase = None
        reply.flush()
        return next_phase
    except _Disconnected:
        return None
