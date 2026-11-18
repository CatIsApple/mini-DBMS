import random
import unittest

from datom.bptree import BPlusTree


class BPlusTreeTests(unittest.TestCase):
    def assert_tree_valid(self, tree, expected):
        leaves = []
        depths = set()

        def inspect(node, depth, is_root=False):
            self.assertEqual(node.keys, sorted(set(node.keys)))
            self.assertLess(len(node.keys), tree._order)
            if node.leaf:
                self.assertEqual(len(node.offsets), len(node.keys))
                self.assertEqual(node.children, [])
                if not is_root:
                    self.assertGreaterEqual(len(node.keys), tree._order // 2)
                leaves.append(node)
                depths.add(depth)
                return node.keys

            self.assertEqual(len(node.children), len(node.keys) + 1)
            self.assertEqual(node.offsets, [])
            minimum_children = 2 if is_root else (tree._order + 1) // 2
            self.assertGreaterEqual(len(node.children), minimum_children)
            groups = [inspect(child, depth + 1) for child in node.children]
            self.assertEqual(node.keys, [keys[0] for keys in groups[1:]])
            return [key for keys in groups for key in keys]

        self.assertEqual(inspect(tree._root, 0, True), sorted(expected))
        self.assertEqual(len(depths), 1)
        linked = []
        leaf = leaves[0]
        for expected_leaf in leaves:
            self.assertIs(leaf, expected_leaf)
            linked.extend(zip(leaf.keys, leaf.offsets))
            leaf = leaf.next_leaf
        self.assertIsNone(leaf)
        self.assertEqual(linked, sorted(expected.items()))

    def test_empty_tree_and_integer_boundaries(self):
        tree = BPlusTree()
        self.assertIsNone(tree.find(0))
        expected = {-(1 << 63): 0, 0: 99, (1 << 64) - 1: (1 << 64) - 1}
        for key, offset in expected.items():
            tree.insert(key, offset)
        for key, offset in expected.items():
            self.assertEqual(tree.find(key), offset)
        self.assertIsNone(tree.find(-1))
        self.assertIsNone(tree.find(1 << 64))
        self.assert_tree_valid(tree, expected)

    def test_order_validation(self):
        for order in (0, 1, 2, -4, 3.5, True, "64"):
            with self.subTest(order=order):
                with self.assertRaises(ValueError):
                    BPlusTree(order)

    def test_multilevel_splits_in_all_insertion_orders(self):
        randomizer = random.Random(2026)
        for order in (3, 4, 5, 8, 64):
            ascending = list(range(1200))
            shuffled = ascending.copy()
            randomizer.shuffle(shuffled)
            for sequence in (ascending, ascending[::-1], shuffled):
                with self.subTest(order=order, first=sequence[0]):
                    tree = BPlusTree(order)
                    expected = {}
                    for number in sequence:
                        key = 2 * number
                        expected[key] = number + 17
                        tree.insert(key, expected[key])
                        self.assertEqual(tree.find(key), expected[key])
                    for key, offset in expected.items():
                        self.assertEqual(tree.find(key), offset)
                        self.assertIsNone(tree.find(key + 1))
                    for key in range(0, 2400, 14):
                        tree.insert(key, -key)
                        expected[key] = -key
                    for key, offset in expected.items():
                        self.assertEqual(tree.find(key), offset)
                    self.assert_tree_valid(tree, expected)

    def test_randomized_stress_and_duplicate_updates(self):
        randomizer = random.Random(1000000)
        keys = list(range(25000))
        randomizer.shuffle(keys)
        tree = BPlusTree()
        expected = {}
        for key in keys:
            expected[key] = key * 100
            tree.insert(key, expected[key])
        for _ in range(3000):
            key = randomizer.randrange(len(keys))
            expected[key] = randomizer.randrange(1 << 63)
            tree.insert(key, expected[key])
        for key, offset in expected.items():
            self.assertEqual(tree.find(key), offset)
        self.assertIsNone(tree.find(-1))
        self.assertIsNone(tree.find(len(keys)))
        self.assert_tree_valid(tree, expected)


if __name__ == "__main__":
    unittest.main()
