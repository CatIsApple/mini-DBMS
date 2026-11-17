"""Black-box tests for the SQL-file CLI and PostgreSQL Simple Query TCP server."""

import argparse
import concurrent.futures
import contextlib
import re
import select
import socket
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path


TIMEOUT = 8


def encode_frame(tag, payload):
    return tag + struct.pack("!I", len(payload) + 4) + payload


def read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise EOFError("unexpected end of PostgreSQL frame")
        data.extend(part)
    return bytes(data)


def read_frame(sock):
    tag = read_exact(sock, 1)
    size, = struct.unpack("!I", read_exact(sock, 4))
    assert 4 <= size <= 1 << 20, (tag, size)
    return tag, read_exact(sock, size - 4)


def reply(sock):
    messages = []
    while len(messages) < 1000:
        tag, body = read_frame(sock)
        messages.append((tag, body))
        if tag == b"Z":
            assert body == b"I", messages
            return messages
    raise AssertionError("no ReadyForQuery within 1000 response frames")


def send_fragmented(sock, frame):
    # Deliver a partial header and a partial payload in separate writes.
    for part in (frame[:2], frame[2:5], frame[5:-1], frame[-1:]):
        sock.sendall(part)
        time.sleep(0.005)


def startup_frame(database):
    params = b"user\0tester\0database\0" + database.encode("utf8") + b"\0\0"
    return encode_frame(b"", struct.pack("!I", 3 << 16) + params)


@contextlib.contextmanager
def client(port, fragmented=False, receive_buffer=None):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conn:
        conn.settimeout(TIMEOUT)
        if receive_buffer is not None:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
        conn.connect(("127.0.0.1", port))
        conn.sendall(struct.pack("!II", 8, 80877103))
        assert read_exact(conn, 1) == b"N", "expected TLS negotiation decline"
        frame = startup_frame("datom")
        if fragmented:
            send_fragmented(conn, frame)
        else:
            conn.sendall(frame)
        messages = reply(conn)
        assert messages[0] == (b"R", b"\0\0\0\0"), messages
        yield conn


def query(conn, sql, fragmented=False):
    frame = encode_frame(b"Q", sql.encode("utf8") + b"\0")
    if fragmented:
        send_fragmented(conn, frame)
    else:
        conn.sendall(frame)
    return reply(conn)


def verify_columns(desc, expected_columns):
    count, = struct.unpack_from("!H", desc)
    columns = []
    cursor = 2
    for _ in range(count):
        end = desc.index(b"\0", cursor)
        name = desc[cursor:end].decode()
        _, _, oid, _, _, fmt = struct.unpack_from("!IhIhih", desc, end + 1)
        assert (oid, fmt) == (20 if name == "id" else 25, 0), (name, oid, fmt)
        columns.append(name)
        cursor = end + 19
    assert columns == expected_columns and cursor == len(desc), columns


def data_row(body, count):
    assert struct.unpack_from("!H", body)[0] == count
    cursor = 2
    row = []
    for _ in range(count):
        size, = struct.unpack_from("!i", body, cursor)
        cursor += 4
        assert 0 <= size <= len(body) - cursor
        row.append(body[cursor:cursor + size].decode("utf8"))
        cursor += size
    assert cursor == len(body)
    return row


def select_rows(messages, expected_columns):
    tags = [tag for tag, _ in messages]
    assert tags == [b"T"] + [b"D"] * (len(tags) - 3) + [b"C", b"Z"], messages
    verify_columns(messages[0][1], expected_columns)
    rows = [data_row(body, len(expected_columns)) for _, body in messages[1:-2]]
    assert messages[-2][1] == f"SELECT {len(rows)}\0".encode(), messages[-2]
    return rows


def sql_error(messages, sqlstate):
    assert [tag for tag, _ in messages] == [b"E", b"Z"], messages
    fields = messages[0][1].split(b"\0")
    assert b"C" + sqlstate in fields, fields
    assert any(field.startswith(b"M") and len(field) > 1 for field in fields), fields


def cli_run(binary, data, sql, success=True):
    script = data.parent / "queries.sql"
    script.write_text(sql, encoding="utf8")
    proc = subprocess.run([binary, "--data", data, "-f", script],
                          capture_output=True, text=True, timeout=TIMEOUT)
    assert (proc.returncode == 0) == success, (sql, proc.stdout, proc.stderr)
    return proc.stdout.splitlines()


def cli_checks(binary, data):
    directory = subprocess.run([binary, "--data", data, "-f", data.parent],
                               capture_output=True, text=True, timeout=TIMEOUT)
    assert directory.returncode != 0 and "directory" in directory.stderr, directory
    assert cli_run(binary, data, "") == []
    lines = cli_run(binary, data, """
        -- The semicolon inside the name belongs to the string.
        INSERT INTO users (email, name) VALUES
            ('first@test','김;O''Brien'), ('second@test','두번째');
        SELECT id, name FROM users WHERE id = 1;
    """)
    assert lines == ["INSERT 0 2", "id\tname", "1\t김;O'Brien", "SELECT 1"], lines
    lines = cli_run(binary, data, "SELECT email FROM users WHERE name = '김;O''Brien';")
    assert lines == ["email", "first@test", "SELECT 1"], lines
    cli_run(binary, data, "INSERT INTO users VALUES ('bad', 'ok'), ('short');", False)
    cli_run(binary, data, "SELECT * FROM missing;", False)
    lines = cli_run(binary, data, "SELECT id FROM users;")
    assert lines[:1] == ["id"] and sorted(lines[1:-1]) == ["1", "2"] and lines[-1] == "SELECT 2", lines


def check_queries(port):
    with socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT) as conn:
        conn.sendall(startup_frame("unknown_db"))
        tag, body = read_frame(conn)
        assert tag == b"E" and b"C3D000" in body.split(b"\0"), (tag, body)
        assert conn.recv(1) == b"", "fatal startup failure should close the session"
    with client(port, fragmented=True) as conn:
        rows = select_rows(query(conn, "SELECT id, name FROM users WHERE id=1", True),
                           ["id", "name"])
        assert rows == [["1", "김;O'Brien"]], rows
        sql_error(query(conn, "SELECT name FROM"), b"42601")
        assert query(conn, ";") == [(b"I", b""), (b"Z", b"I")]
        sql_error(query(conn, "INSERT INTO users VALUES ('bad','bad'); SELECT * FROM users"),
                  b"0A000")
        assert select_rows(query(conn, "SELECT id FROM users WHERE name='bad'"), ["id"]) == []
        # Unsupported extended queries must discard subsequent commands until Sync.
        parse = encode_frame(b"P", b"\0SELECT name FROM users\0\0\0")
        skipped = encode_frame(b"Q", b"INSERT INTO users VALUES ('skipped','bad')\0")
        conn.sendall(parse + skipped + encode_frame(b"S", b""))
        sql_error(reply(conn), b"0A000")
        assert select_rows(query(conn, "SELECT id FROM users WHERE name='skipped'"), ["id"]) == []
        conn.sendall(encode_frame(b"X", b""))
        assert conn.recv(1) == b"", "Terminate should close the session"


def concurrent_writes(port):
    clients = 12
    rounds = 3
    barrier = threading.Barrier(clients, timeout=TIMEOUT)

    def writer(worker):
        with client(port) as conn:
            barrier.wait()
            for i in range(rounds):
                name = f"writer-{worker}-{i}"
                result = query(conn, f"INSERT INTO users VALUES ('{name}','x')")
                assert result == [(b"C", b"INSERT 0 1\0"), (b"Z", b"I")], result
                rows = select_rows(query(conn, f"SELECT id FROM users WHERE name='{name}'"),
                                   ["id"])
                assert len(rows) == 1, rows
                row_id = int(rows[0][0])
                indexed = select_rows(query(conn, f"SELECT name FROM users WHERE id={row_id}"),
                                      ["name"])
                assert indexed == [[name]], indexed

    with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as workers:
        list(workers.map(writer, range(clients)))
    with client(port) as conn:
        rows = select_rows(query(conn, "SELECT id, name FROM users"), ["id", "name"])
    total = 2 + clients * rounds
    assert sorted(int(row[0]) for row in rows) == list(range(1, total + 1)), rows
    expected_names = {f"writer-{n}-{i}" for n in range(clients) for i in range(rounds)}
    assert {name for _, name in rows} == expected_names | {"김;O'Brien", "두번째"}, rows
    return total


def snapshot_during_write(port):
    # 10 MiB across eight ordinary INSERTs; the reader advertises a small TCP window.
    payload = "p" * (64 * 1024)
    count = 160
    with client(port) as writer, client(port, receive_buffer=64 * 1024) as reader:
        values = ",".join(f"('{payload}', 'stock')" for _ in range(20))
        for _ in range(count // 20):
            response = query(writer, f"INSERT INTO products (name,sku) VALUES {values}")
            assert response == [(b"C", b"INSERT 0 20\0"), (b"Z", b"I")], response

        reader.sendall(encode_frame(b"Q", b"SELECT id, name FROM products\0"))
        tag, descriptor = read_frame(reader)
        assert tag == b"T", tag
        verify_columns(descriptor, ["id", "name"])

        def next_id():
            tag, body = read_frame(reader)
            assert tag == b"D", tag
            row_id, name = data_row(body, 2)
            assert name == payload, "snapshot included an unexpected product"
            return int(row_id)

        ids = [next_id()]
        # The read snapshot is now established. Leave its socket undrained until the write finishes.
        response = query(writer, "INSERT INTO products (name,sku) VALUES ('late','new')")
        assert response == [(b"C", b"INSERT 0 1\0"), (b"Z", b"I")], response
        ids.extend(next_id() for _ in range(count - 1))
        assert read_frame(reader) == (b"C", f"SELECT {count}\0".encode())
        assert read_frame(reader) == (b"Z", b"I")
        assert sorted(ids) == list(range(1, count + 1)), ids
        rows = select_rows(query(reader, f"SELECT name FROM products WHERE id={count + 1}"),
                           ["name"])
        assert rows == [["late"]], rows


def server_checks(cli, server, data):
    proc = subprocess.Popen([server, "--data", data, "--port", "0", "--workers", "3"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([proc.stdout], [], [], TIMEOUT)
        assert ready, "server did not announce its port"
        line = proc.stdout.readline()
        match = re.search(r"listening on 127\.0\.0\.1:(\d+)", line)
        assert match, (line, proc.poll())
        port = int(match.group(1))
        cli_run(cli, data, "SELECT * FROM users;", False)

        with contextlib.ExitStack() as sessions:
            # Four authenticated idle sessions must not monopolize a three-worker pool.
            for _ in range(4):
                sessions.enter_context(client(port))
            check_queries(port)
            total = concurrent_writes(port)
            snapshot_during_write(port)

            # Stop with three incomplete inputs and idle peers still holding sockets open.
            for _ in range(3):
                sock = sessions.enter_context(client(port))
                sock.sendall(b"Q\x00\x00")
            proc.terminate()
            assert proc.wait(timeout=TIMEOUT) == 0
        return total
    finally:
        if proc.poll() is None:
            proc.kill()
        _, stderr = proc.communicate(timeout=TIMEOUT)
        if stderr:
            print(stderr.rstrip())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--server", type=Path, required=True)
    args = parser.parse_args()
    cli, server = args.cli.resolve(), args.server.resolve()
    with tempfile.TemporaryDirectory(prefix="datom-test-") as tmp:
        data = Path(tmp) / "db"
        cli_checks(cli, data)
        print("PASS CLI: SQL file, escaping, scan, indexed read, restart, atomic rejection")
        count = server_checks(cli, server, data)
        print("PASS TCP: startup, invalid database, framing, query errors, concurrent clients, idle sessions, shutdown")
        print("PASS snapshot: 10 MiB SELECT excludes a concurrent committed insert; next query sees it")
        lines = cli_run(cli, data, f"SELECT id FROM users WHERE id={count};")
        assert lines == ["id", str(count), "SELECT 1"], lines
        lines = cli_run(cli, data, "INSERT INTO users VALUES ('after restart','ok'); SELECT id FROM users WHERE name='after restart';")
        assert lines == ["INSERT 0 1", "id", str(count + 1), "SELECT 1"], lines
        print("PASS storage: server writes survive reopen and automatic IDs resume")


if __name__ == "__main__":
    main()
