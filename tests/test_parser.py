import unittest

from minidb.errors import ParseError
from minidb.parser import Insert, Select, parse


class ParserTest(unittest.TestCase):
    def test_insert(self):
        (s,) = parse("insert into Users (Name, age) values ('a''b;c', -3), (NULL, 2.5e1);")
        self.assertEqual(s, Insert("users", ("name", "age"), (("a'b;c", -3), (None, 25.0))))

    def test_insert_without_columns(self):
        (s,) = parse("INSERT INTO t VALUES (1)")
        self.assertIsNone(s.columns)

    def test_select_variants(self):
        a, b, c = parse("SELECT * FROM t; select x, y from t limit 3; SELECT COUNT(*) FROM t")
        self.assertIsNone(a.columns)
        self.assertEqual((b.columns, b.limit), (("x", "y"), 3))
        self.assertTrue(c.count)

    def test_count_as_column_name(self):
        (s,) = parse("SELECT count FROM t")
        self.assertEqual(s.columns, ("count",))

    def test_where_tree(self):
        (s,) = parse("SELECT * FROM t WHERE a = 1 AND b <> 'x' OR c IS NOT NULL")
        self.assertEqual(s.where, ("or", [("and", [("cmp", "a", "=", 1), ("cmp", "b", "!=", "x")]),
                                          ("isnull", "c", True)]))

    def test_precedence_parens(self):
        (s,) = parse("SELECT * FROM t WHERE a = 1 AND (b = 2 OR c = 3)")
        self.assertEqual(s.where[0], "and")
        self.assertEqual(s.where[1][1][0], "or")

    def test_comments_whitespace_and_empty(self):
        self.assertEqual(parse("-- only a comment\n  ;; \n"), [])
        (s,) = parse("SELECT * -- why\nFROM t")
        self.assertEqual(s.table, "t")

    def test_explain(self):
        (s,) = parse("EXPLAIN SELECT * FROM t")
        self.assertTrue(s.explain)

    def test_strings_keep_case_and_unicode(self):
        (s,) = parse("INSERT INTO t VALUES ('HeLLo 안녕 SELECT')")
        self.assertEqual(s.rows[0][0], "HeLLo 안녕 SELECT")

    def test_errors(self):
        bad = [
            "SELEC * FROM t", "SELECT FROM t", "SELECT * t", "SELECT * FROM", "SELECT * FROM t WHERE",
            "SELECT * FROM t WHERE a", "SELECT * FROM t WHERE a = ", "SELECT * FROM t WHERE (a = 1",
            "SELECT * FROM t LIMIT -1", "SELECT * FROM t LIMIT 1.5", "SELECT * FROM t garbage",
            "INSERT INTO t VALUES", "INSERT INTO t VALUES ()", "INSERT INTO t VALUES (1,)",
            "INSERT INTO t (a) VALUES (1) (2)", "INSERT INTO t VALUES ('unterminated)",
            "INSERT INTO t VALUES (@)", "INSERT INTO t VALUES (-'x')", "EXPLAIN INSERT INTO t VALUES (1)",
            "DELETE FROM t", "SELECT * FROM select", "SELECT * FROM t WHERE a = 1 b = 2", "",
        ]
        for sql in bad:
            if sql == "":
                self.assertEqual(parse(sql), [])
                continue
            with self.assertRaises(ParseError, msg=sql):
                parse(sql)

    def test_deep_nesting_rejected_not_crashed(self):
        with self.assertRaises(ParseError):
            parse("SELECT * FROM t WHERE " + "(" * 5000 + "a = 1" + ")" * 5000)

    def test_long_flat_where_ok(self):
        sql = "SELECT * FROM t WHERE " + " OR ".join(f"a = {i}" for i in range(5000))
        self.assertEqual(len(parse(sql)[0].where[1]), 5000)

    def test_script_is_all_or_nothing(self):
        with self.assertRaises(ParseError):
            parse("SELECT * FROM t; SELECT * FROM")


if __name__ == "__main__":
    unittest.main()
