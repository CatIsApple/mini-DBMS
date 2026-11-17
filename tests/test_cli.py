import contextlib
import io
import os

from minidb.cli import format_table, main
from tests.helpers import DBTestCase


class CLITest(DBTestCase):
    def run_cli(self, *args):
        self.db.close()
        out, err = io.StringIO(), io.StringIO()
        out.reconfigure = err.reconfigure = lambda **kw: None  # main() sets utf-8 on real streams
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["-d", self.dir, *args])
        return code, out.getvalue(), err.getvalue()

    def test_execute_and_file(self):
        code, out, _ = self.run_cli("-e", "INSERT INTO users (name, age) VALUES ('kim', 3);")
        self.assertEqual((code, out.strip()), (0, "INSERT 1 (id 1..1)"))
        path = os.path.join(self.dir, "q.sql")
        with open(path, "w", encoding="utf-8-sig") as f:  # BOM like Windows Notepad
            f.write("-- c\nSELECT name, age FROM users WHERE id = 1;")
        code, out, _ = self.run_cli("-f", path)
        self.assertEqual(code, 0)
        self.assertIn("| kim  | 3   |", out)
        self.assertIn("INDEX LOOKUP", out)

    def test_error_exit_codes(self):
        self.assertEqual(self.run_cli("-e", "SELEC")[0], 1)
        self.assertEqual(self.run_cli("-e", "SELECT * FROM nope")[0], 1)
        self.assertEqual(self.run_cli("-f", os.path.join(self.dir, "missing.sql"))[0], 2)
        self.assertEqual(self.run_cli("-d", os.path.join(self.dir, "nodir"), "-e", "SELECT 1")[0], 2)

    def test_json_output(self):
        self.run_cli("-e", "INSERT INTO users (name) VALUES ('x')")
        _, out, _ = self.run_cli("--json", "-e", "SELECT name FROM users")
        self.assertIn('"rows": [["x"]]', out)

    def test_table_format_null_and_unicode(self):
        t = format_table(["a", "b"], [[1, None], [22, "x"]])
        self.assertIn("| 1  | NULL |", t)
