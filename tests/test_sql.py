from __future__ import annotations

import unittest

from datom.errors import DbError
from datom.sql import Predicate, Statement, iter_statements, parse_sql


class SqlTests(unittest.TestCase):
    def assert_sql_error(self, sql, detail="", code="42601"):
        with self.assertRaises(DbError) as caught:
            parse_sql(sql)
        self.assertEqual(caught.exception.code, code)
        self.assertIn(detail, str(caught.exception))

    def test_sql_file(self):
        statements = parse_sql("""
            ;-- This comment contains a semicolon;
            InSeRt /* outer /* nested */ comment */ InTo USERS(Name, age)
            VaLuEs ('O''Brien; -- /*', 21), ('', -1);
            sEleCt name, "ExactCase" fRoM Users wHeRe Id = 42;
            SELECT * FROM users WHERE name = '정글;DB';;
            SELECT * FROM users -- No final delimiter
        """)
        self.assertEqual(statements, [
            Statement("insert", "users", ["name", "age"],
                      [["O'Brien; -- /*", "21"], ["", "-1"]]),
            Statement("select", "users", ["name", "ExactCase"], where=Predicate("id", "42")),
            Statement("select", "users", where=Predicate("name", "정글;DB")),
            Statement("select", "users"),
        ])

    def test_identifiers_and_literals(self):
        sql = """INSERT INTO "Mixed""Case" VALUES (+123, '한글', 'a\nb', '''')"""
        statement = parse_sql(sql)[0]
        self.assertEqual(statement.table, 'Mixed"Case')
        self.assertEqual(statement.columns, [])
        self.assertEqual(statement.values, [["+123", "한글", "a\nb", "'"]])
        self.assertEqual(parse_sql('select "select" from "FROM"')[0],
                         Statement("select", "FROM", ["select"]))
        self.assertEqual(parse_sql("select _col$2 from _t1")[0].columns, ["_col$2"])
        self.assertEqual(parse_sql("  ; -- test\r\n /* ok */ ;; "), [])

    def test_precise_errors(self):
        for sql, detail in [
            ("select * from users where id = ", "at byte 32"),
            ("insert into t values ('unfinished);", "unterminated string"),
            ('select * from "broken', "unterminated identifier"),
            ('select * from ""', "empty quoted identifier"),
            ("select * from t /* outer /* inner */", "unterminated block comment"),
            ("select\0 x", "at byte 7"),
            ("select '한글' from t", "at byte 8"),
        ]:
            with self.subTest(sql=sql):
                self.assert_sql_error(sql, detail)
        sql = 'select "한글" from t where id = '
        self.assert_sql_error(sql, f"at byte {len(sql.encode('utf-8')) + 1}")

    def test_invalid_statements(self):
        for sql in [
            "create table x (id int)", "insert users values (1)",
            "insert into t values ()", "insert into t values (1,)",
            "insert into t (name,) values ('x')", "insert into t values ('ok'),",
            "select from users", "select name, from t", "select * from t junk",
            "select * from t where id > 4", "select * from t where id = 1 and id = 2",
            "select * from t select * from t", "select * from t where id = 1.0",
            "insert into t values (null)", "select * from t; bad",
            "select * from t where id = １", "select * from users\u00a0",
            "select * from t where id=1abc",
        ]:
            with self.subTest(sql=sql):
                self.assert_sql_error(sql, "at byte")

    def test_incremental_parsing(self):
        statements = iter_statements("select * from first; select * from second trailing")
        self.assertEqual(next(statements), Statement("select", "first"))
        with self.assertRaises(DbError) as caught:
            next(statements)
        self.assertEqual(caught.exception.code, "42601")

    def test_unterminated_later_comment(self):
        statements = iter_statements("select * from first; /* open")
        self.assertEqual(next(statements), Statement("select", "first"))
        with self.assertRaises(DbError):
            next(statements)

    def test_encoding_before_any_statement(self):
        valid = "é정🍎\U0010ffff"
        self.assertEqual(parse_sql(f"select * from users where name='{valid}'")[0].where.value,
                         valid)
        for suffix, code in [
            ("\ud800", "22021"), ("\udfff", "22021"), ("\ud800\udc00", "22021"),
            ("\0", "42601"), ("\0\ud800", "42601"), ("\ud800\0", "22021"),
        ]:
            with self.subTest(suffix=repr(suffix)):
                yielded = []
                with self.assertRaises(DbError) as caught:
                    for statement in iter_statements("insert into t values (1); --" + suffix):
                        yielded.append(statement)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(yielded, [])

    def test_statement_collections_are_independent(self):
        first, second = parse_sql("select * from users; select * from users")
        first.columns.append("name")
        first.values.append(["a"])
        self.assertEqual(second.columns, [])
        self.assertEqual(second.values, [])


if __name__ == "__main__":
    unittest.main()
