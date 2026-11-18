"""Exercise Datom over TCP using an installed PostgreSQL psql executable."""

import argparse
import contextlib
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


TIMEOUT = 20
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def temporary_server(command, directory):
    process = subprocess.Popen(
        [*command, "--data", directory, "--port", "0", "--workers", "2"],
        stdout=subprocess.PIPE, text=True, cwd=PROJECT_ROOT,
    )
    try:
        if not select.select([process.stdout], [], [], TIMEOUT)[0]:
            raise RuntimeError("server did not announce its ephemeral port")
        line = process.stdout.readline()
        match = re.search(r"listening on 127\.0\.0\.1:(\d+)", line)
        if not match:
            raise RuntimeError(f"unexpected startup output: {line!r}")
        yield match.group(1)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()


def run_script(psql, port, directory, sql):
    script = directory / "input.sql"
    script.write_text(sql, encoding="utf-8")
    # Ignore user profiles, libpq service files and ambient connection settings.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    environment.update(LC_ALL="C", PGCLIENTENCODING="UTF8",
                       PGCONNECT_TIMEOUT="5", PGGSSENCMODE="disable", PGSSLMODE="prefer")
    return subprocess.run(
        [psql, "-X", "-w", "-h", "127.0.0.1", "-p", port, "-U", "tester", "-d", "datom",
         "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose", "-A", "-t", "-F", "\t",
         "-f", script],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
        timeout=TIMEOUT, env=environment,
    )


def expect_output(result, expected):
    if result.returncode != 0 or result.stdout.splitlines() != expected:
        raise AssertionError(
            f"psql exited {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, help="override the default python -m datom.server")
    parser.add_argument("--psql", default="psql", help="psql executable name or path")
    args = parser.parse_args()
    psql = shutil.which(args.psql)
    if psql is None:
        parser.error(f"psql executable not found: {args.psql}")
    server = [args.server.resolve()] if args.server else [sys.executable, "-m", "datom.server"]
    with tempfile.TemporaryDirectory(prefix="datom-psql-") as tmp:
        directory = Path(tmp)
        with temporary_server(server, directory / "db") as port:
            initial = run_script(psql, port, directory, """
                INSERT INTO users (name, email) VALUES
                    ('김다온', 'first@test'), ('O''Brien;서울', 'second@test');
                SELECT id, name FROM users WHERE id = 2;
                """)
            expect_output(initial, ["INSERT 0 2", "2\tO'Brien;서울"])

            failed = run_script(psql, port, directory, """
                SELECT id FROM missing_table;
                INSERT INTO users VALUES ('unexpected', 'third@test');
                """)
            # psql -f with ON_ERROR_STOP returns 3 specifically for script errors.
            if failed.returncode != 3 or "42P01" not in failed.stderr:
                raise AssertionError(f"expected undefined_table error: {failed!r}")

            reconnected = run_script(psql, port, directory, "SELECT id, name FROM users;\n")
            expect_output(reconnected, ["1\t김다온", "2\tO'Brien;서울"])
    print("PASS stock psql: SQL file, command tag, UTF-8 values, SQLSTATE, reconnect")


if __name__ == "__main__":
    main()
