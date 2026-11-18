"""Exercise file and standard-input bytes through the Python CLI."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from datom.engine import Engine
from datom.sql import parse_sql


class CliTests(unittest.TestCase):
    def run_cli(self, directory, *options, stdin=None):
        return subprocess.run(
            [sys.executable, "-m", "datom", "--data", str(directory), *options],
            input=stdin, capture_output=True, timeout=10,
            cwd=Path(__file__).resolve().parents[1],
        )

    def test_literal_newlines_survive_file_and_stdin(self):
        sql = b"INSERT INTO users VALUES ('a\rb\r\nc', 'x');"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "data"
            script = Path(temporary) / "input.sql"
            script.write_bytes(sql)
            first = self.run_cli(directory, "-f", str(script))
            second = self.run_cli(directory, stdin=sql)
            self.assertEqual((first.returncode, second.returncode), (0, 0))
            with Engine(directory) as engine:
                rows = engine.execute(parse_sql("SELECT name FROM users")[0]).rows
                self.assertEqual(rows, [["a\rb\r\nc"], ["a\rb\r\nc"]])
            output = self.run_cli(directory, "-c", "SELECT name FROM users WHERE id=1")
            self.assertEqual(output.stdout, b"name\na\\rb\\r\\nc\nSELECT 1\n")

    def test_invalid_utf8_stdin_fails_before_inserting(self):
        with tempfile.TemporaryDirectory() as temporary:
            invalid = b"INSERT INTO users VALUES ('x', 'y'); -- \xff"
            result = self.run_cli(temporary, stdin=invalid)
            self.assertEqual(result.returncode, 1)
            self.assertIn(b"22021", result.stderr)
            with Engine(temporary) as engine:
                self.assertEqual(engine.execute(parse_sql("SELECT id FROM users")[0]).rows, [])
