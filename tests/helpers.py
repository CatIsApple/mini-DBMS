import json
import os
import tempfile
import unittest

from minidb import Database

SCHEMA = {"users": {"name": "TEXT", "age": "INT", "score": "FLOAT"}, "logs": {"msg": "TEXT"}}


class DBTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        with open(os.path.join(self.dir, "schema.json"), "w", encoding="utf-8") as f:
            json.dump(SCHEMA, f)
        self.db = Database(self.dir)
        self.addCleanup(self._tmp.cleanup)  # runs last (LIFO)
        self.addCleanup(lambda: self.db.close())

    def reopen(self):
        self.db.close()
        self.db = Database(self.dir)

    def q(self, sql):
        return self.db.execute(sql)[-1]
