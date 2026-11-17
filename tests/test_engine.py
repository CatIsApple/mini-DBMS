import json
import os
import random

from minidb import Database, ExecutionError, ParseError, StorageError
from tests.helpers import DBTestCase


class EngineTest(DBTestCase):
    def test_insert_assigns_sequential_ids(self):
        r = self.q("INSERT INTO users (name, age) VALUES ('a', 1), ('b', 2)")
        self.assertEqual((r.affected, r.first_id, r.last_id), (2, 1, 2))
        r = self.q("INSERT INTO users (name) VALUES ('c')")
        self.assertEqual((r.first_id, r.last_id), (3, 3))
        self.assertEqual(self.q("SELECT * FROM users").rows,
                         [[1, "a", 1, None], [2, "b", 2, None], [3, "c", None, None]])

    def test_column_order_and_projection(self):
        self.q("INSERT INTO users (age, name) VALUES (7, 'x')")
        r = self.q("SELECT age, id, name FROM users")
        self.assertEqual((r.columns, r.rows), (["age", "id", "name"], [[7, 1, "x"]]))

    def test_insert_without_column_list(self):
        self.q("INSERT INTO users VALUES ('a', 1, 2.5)")
        self.assertEqual(self.q("SELECT * FROM users").rows, [[1, "a", 1, 2.5]])
        with self.assertRaises(ExecutionError):
            self.q("INSERT INTO users VALUES ('a', 1)")

    def test_where_operators_and_logic(self):
        self.q("INSERT INTO users (name, age) VALUES ('a', 10), ('b', 20), ('c', 30), ('d', NULL)")
        ids = lambda w: [r[0] for r in self.q(f"SELECT id FROM users WHERE {w}").rows]
        self.assertEqual(ids("age = 20"), [2])
        self.assertEqual(ids("age != 20"), [1, 3])  # NULL excluded (SQL semantics)
        self.assertEqual(ids("age <> 20"), [1, 3])
        self.assertEqual(ids("age < 30"), [1, 2])
        self.assertEqual(ids("age >= 20"), [2, 3])
        self.assertEqual(ids("age IS NULL"), [4])
        self.assertEqual(ids("age IS NOT NULL"), [1, 2, 3])
        self.assertEqual(ids("age = NULL"), [])
        self.assertEqual(ids("name = 'a' OR name = 'c'"), [1, 3])
        self.assertEqual(ids("age > 5 AND (name = 'a' OR name = 'c') AND id > 1"), [3])
        self.assertEqual(ids("age = 20.0"), [2])  # int column vs float literal
        self.assertEqual(ids("name > 'b'"), [3, 4])

    def test_limit_and_count(self):
        self.q("INSERT INTO users (name) VALUES ('a'), ('b'), ('c')")
        self.assertEqual(len(self.q("SELECT * FROM users LIMIT 2").rows), 2)
        self.assertEqual(self.q("SELECT * FROM users LIMIT 0").rows, [])
        self.assertEqual(self.q("SELECT COUNT(*) FROM users").rows, [[3]])
        self.assertEqual(self.q("SELECT COUNT(*) FROM users WHERE id >= 2").rows, [[2]])
        self.assertEqual(self.q("SELECT * FROM users WHERE id > 1 LIMIT 1").rows[0][0], 2)

    def test_empty_table(self):
        self.assertEqual(self.q("SELECT * FROM users").rows, [])
        self.assertEqual(self.q("SELECT * FROM users WHERE id = 1").rows, [])
        self.assertEqual(self.q("SELECT COUNT(*) FROM users").rows, [[0]])

    def test_errors(self):
        cases = {
            "SELECT * FROM nope": ExecutionError,
            "SELECT nope FROM users": ExecutionError,
            "SELECT * FROM users WHERE nope = 1": ExecutionError,
            "SELECT * FROM users WHERE name = 5": ExecutionError,
            "SELECT * FROM users WHERE age = 'x'": ExecutionError,
            "SELECT * FROM users WHERE id = 'x'": ExecutionError,
            "INSERT INTO users (id, name) VALUES (1, 'a')": ExecutionError,
            "INSERT INTO users (nope) VALUES (1)": ExecutionError,
            "INSERT INTO users (name, name) VALUES ('a', 'b')": ExecutionError,
            "INSERT INTO users (name, age) VALUES ('a')": ExecutionError,
            "INSERT INTO users (age) VALUES ('x')": ExecutionError,
            "INSERT INTO users (age) VALUES (1.5)": ExecutionError,
            "INSERT INTO users (age) VALUES (9223372036854775808)": ExecutionError,
            "INSERT INTO users (name) VALUES (5)": ExecutionError,
            "INSERT INTO users (score) VALUES (1e999)": ExecutionError,
            "SELECT * FROM": ParseError,
        }
        for sql, exc in cases.items():
            with self.assertRaises(exc, msg=sql):
                self.q(sql)
        self.assertEqual(self.q("SELECT COUNT(*) FROM users").rows, [[0]])  # nothing leaked in

    def test_multirow_insert_is_atomic(self):
        with self.assertRaises(ExecutionError):
            self.q("INSERT INTO users (age) VALUES (1), (2), ('bad')")
        self.assertEqual(self.q("SELECT COUNT(*) FROM users").rows, [[0]])
        self.assertEqual(self.q("INSERT INTO users (age) VALUES (1)").first_id, 1)  # id not burned

    def test_script_reports_completed_count(self):
        with self.assertRaises(ExecutionError) as cm:
            self.db.execute("INSERT INTO users (age) VALUES (1); SELECT * FROM nope")
        self.assertEqual(cm.exception.completed, 1)
        with self.assertRaises(ParseError):  # syntax error: nothing at all runs
            self.db.execute("INSERT INTO users (age) VALUES (2); SELECT")
        self.assertEqual(self.q("SELECT COUNT(*) FROM users").rows, [[1]])

    def test_special_text(self):
        vals = ["O'Brien", "한글 😀", "line1\nline2", 'quote"back\\slash', "", "; DROP", "tab\t"]
        for v in vals:
            self.db.execute("INSERT INTO users (name) VALUES ('" + v.replace("'", "''") + "')")
        self.reopen()
        self.assertEqual([r[1] for r in self.q("SELECT * FROM users").rows], vals)

    def test_lone_surrogate_rejected(self):
        with self.assertRaises(ExecutionError):
            self.q("INSERT INTO users (name) VALUES ('\ud800')")
        self.assertEqual(self.q("SELECT COUNT(*) FROM users").rows, [[0]])

    def test_tables_are_isolated(self):
        self.q("INSERT INTO users (name) VALUES ('u')")
        self.q("INSERT INTO logs (msg) VALUES ('m')")
        self.assertEqual(self.q("SELECT * FROM logs").rows, [[1, "m"]])

    def test_persistence_and_id_continuation(self):
        self.q("INSERT INTO users (name) VALUES ('a'), ('b')")
        self.reopen()
        self.assertEqual(self.q("SELECT name FROM users WHERE id = 2").rows, [["b"]])
        self.assertEqual(self.q("INSERT INTO users (name) VALUES ('c')").first_id, 3)

    def test_torn_last_line_is_dropped_on_open(self):
        self.q("INSERT INTO users (name) VALUES ('a'), ('b')")
        self.db.close()
        with open(os.path.join(self.dir, "users.tbl"), "ab") as f:
            f.write(b'[3,"half-writ')
        self.db = Database(self.dir)
        self.assertEqual(self.q("SELECT COUNT(*) FROM users").rows, [[2]])
        self.assertEqual(self.q("INSERT INTO users (name) VALUES ('c')").first_id, 3)
        self.reopen()
        self.assertEqual(self.q("SELECT name FROM users").rows, [["a"], ["b"], ["c"]])

    def test_corrupt_file_raises(self):
        self.db.close()
        with open(os.path.join(self.dir, "users.tbl"), "wb") as f:
            f.write(b"garbage\n")
        with self.assertRaises(StorageError):
            Database(self.dir)

    def test_schema_validation(self):
        self.db.close()
        path = os.path.join(self.dir, "schema.json")
        for bad in ({}, {"t": {}}, {"t": {"id": "INT"}}, {"t": {"a": "BLOB"}}, {"../x": {"a": "INT"}}):
            with open(path, "w") as f:
                json.dump(bad, f)
            with self.assertRaises(StorageError, msg=bad):
                Database(self.dir)
        os.remove(path)
        with self.assertRaises(StorageError):
            Database(self.dir)


class IndexPlanTest(DBTestCase):
    def setUp(self):
        super().setUp()
        self.q("INSERT INTO users (name, age) VALUES " + ",".join(f"('n{i}', {i % 7})" for i in range(1, 301)))

    def plan(self, where):
        return self.q(f"EXPLAIN SELECT * FROM users WHERE {where}").plan

    def test_plans(self):
        self.assertIn("INDEX LOOKUP", self.plan("id = 5"))
        self.assertIn("INDEX LOOKUP", self.plan("age = 1 AND id = 5"))
        self.assertIn("INDEX RANGE", self.plan("id > 5"))
        self.assertIn("INDEX RANGE", self.plan("id >= 5 AND id < 9 AND age = 1"))
        self.assertIn("FULL TABLE SCAN", self.plan("age = 1"))
        self.assertIn("FULL TABLE SCAN", self.plan("id = 5 OR age = 1"))  # OR: index unsafe
        self.assertIn("FULL TABLE SCAN", self.plan("id != 5"))
        self.assertIn("FULL TABLE SCAN", self.plan("id = NULL"))
        self.assertIn("FULL TABLE SCAN", self.q("EXPLAIN SELECT * FROM users").plan)

    def test_index_results_equal_oracle(self):
        rows = self.q("SELECT * FROM users").rows
        rng = random.Random(1)
        ops = {"=": lambda a, b: a == b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
               ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}
        for _ in range(300):
            o1, o2 = rng.choice(list(ops)), rng.choice(list(ops))
            a, b = rng.randint(-5, 310), rng.randint(-5, 310)
            if rng.random() < 0.3:
                a += 0.5
            where = f"id {o1} {a} AND id {o2} {b}"
            want = [r for r in rows if ops[o1](r[0], a) and ops[o2](r[0], b)]
            got = self.q(f"SELECT * FROM users WHERE {where}").rows
            self.assertEqual(got, want, where)

    def test_index_plus_residual_filter(self):
        want = [[r[0]] for r in self.q("SELECT * FROM users").rows if 10 <= r[0] <= 20 and r[2] == 0]
        self.assertEqual(self.q("SELECT id FROM users WHERE id >= 10 AND id <= 20 AND age = 0").rows, want)

    def test_lookup_edge_ids(self):
        for w in ("id = 0", "id = -1", "id = 301", "id = 2.5", "id = 99999999999999999999"):
            self.assertEqual(self.q(f"SELECT * FROM users WHERE {w}").rows, [], w)
        self.assertEqual(self.q("SELECT id FROM users WHERE id = 1").rows, [[1]])
        self.assertEqual(self.q("SELECT id FROM users WHERE id = 300").rows, [[300]])
        self.assertEqual(self.q("SELECT id FROM users WHERE id = 3.0").rows, [[3]])

    def test_index_survives_reopen(self):
        self.reopen()
        self.assertEqual(self.q("SELECT name FROM users WHERE id = 250").rows, [["n250"]])
        self.assertEqual(len(self.db.tables["users"].index), 300)
