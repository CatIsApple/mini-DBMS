import random
import unittest

from minidb.bptree import BPlusTree


class BPlusTreeTest(unittest.TestCase):
    def test_orders_and_insert_patterns(self):
        n = 2000
        for order in (3, 4, 5, 16, 64):
            for name, keys in (("asc", range(n)), ("desc", range(n, 0, -1)),
                               ("rand", random.Random(order).sample(range(10 * n), n))):
                t = BPlusTree(order)
                for k in keys:
                    t.insert(k, k * 2)
                t.check()
                self.assertEqual(len(t), n, (order, name))
                for k in keys:
                    self.assertEqual(t.search(k), k * 2)
                self.assertEqual([k for k, _ in t.items()], sorted(keys))

    def test_missing_and_duplicate(self):
        t = BPlusTree(4)
        self.assertIsNone(t.search(1))
        self.assertEqual(list(t.items()), [])
        t.insert(1, "a")
        with self.assertRaises(KeyError):
            t.insert(1, "b")
        self.assertEqual(t.search(1), "a")
        self.assertIn(1, t)
        self.assertNotIn(2, t)
        self.assertEqual(t.search(2, "d"), "d")

    def test_range_bounds(self):
        t = BPlusTree(4)
        for k in range(0, 100, 2):
            t.insert(k, k)
        got = lambda *a, **kw: [k for k, _ in t.items(*a, **kw)]
        self.assertEqual(got(10, 16), [10, 12, 14, 16])
        self.assertEqual(got(10, 16, lo_inclusive=False, hi_inclusive=False), [12, 14])
        self.assertEqual(got(11, 15), [12, 14])  # bounds not present as keys
        self.assertEqual(got(lo=95), [96, 98])
        self.assertEqual(got(hi=3), [0, 2])
        self.assertEqual(got(1000), [])
        self.assertEqual(got(20, 10), [])
        self.assertEqual(got(2.5, 7.5), [4, 6])  # float bounds on int keys

    def test_height_grows_logarithmically(self):
        t = BPlusTree(64)
        for k in range(100_000):
            t.insert(k, k)
        self.assertLessEqual(t.height, 4)

    def test_bad_order(self):
        with self.assertRaises(ValueError):
            BPlusTree(2)


if __name__ == "__main__":
    unittest.main()
