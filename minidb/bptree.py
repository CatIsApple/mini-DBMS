"""In-memory B+ tree: unique keys, values live in linked leaves (fast range scans).

Insert-only. Delete is not needed by the engine (tables are append-only).
Not thread-safe; the table's RW lock guards it.
"""
from bisect import bisect_left, bisect_right


class _Node:
    __slots__ = ("leaf", "keys", "vals", "next")

    def __init__(self, leaf):
        self.leaf = leaf
        self.keys = []
        self.vals = []  # leaf: values; internal: children (len(keys) + 1)
        self.next = None  # leaf chain


class BPlusTree:
    def __init__(self, order=64):
        if order < 3:
            raise ValueError("order must be >= 3")
        self.max_keys = order - 1
        self.root = _Node(True)
        self._size = 0

    def __len__(self):
        return self._size

    def __contains__(self, key):
        return self.search(key) is not None

    @property
    def height(self):
        h, node = 1, self.root
        while not node.leaf:
            node, h = node.vals[0], h + 1
        return h

    def _find_leaf(self, key):
        node = self.root
        while not node.leaf:
            node = node.vals[bisect_right(node.keys, key)]
        return node

    def search(self, key, default=None):
        leaf = self._find_leaf(key)
        i = bisect_left(leaf.keys, key)
        if i < len(leaf.keys) and leaf.keys[i] == key:
            return leaf.vals[i]
        return default

    def insert(self, key, value):
        """Add key. Raises KeyError on duplicate."""
        path, node = [], self.root
        while not node.leaf:
            i = bisect_right(node.keys, key)
            path.append((node, i))
            node = node.vals[i]
        i = bisect_left(node.keys, key)
        if i < len(node.keys) and node.keys[i] == key:
            raise KeyError(f"duplicate key {key!r}")
        node.keys.insert(i, key)
        node.vals.insert(i, value)
        self._size += 1
        if len(node.keys) <= self.max_keys:
            return

        # leaf split: right sibling keeps keys >= separator (separator is copied up)
        mid = len(node.keys) // 2
        right = _Node(True)
        right.keys, right.vals = node.keys[mid:], node.vals[mid:]
        del node.keys[mid:], node.vals[mid:]
        right.next, node.next = node.next, right
        sep = right.keys[0]
        left = node
        # propagate splits upward
        while path:
            parent, i = path.pop()
            parent.keys.insert(i, sep)
            parent.vals.insert(i + 1, right)
            if len(parent.keys) <= self.max_keys:
                return
            mid = len(parent.keys) // 2  # internal split: middle key moves up (not copied)
            sep = parent.keys[mid]
            right = _Node(False)
            right.keys, right.vals = parent.keys[mid + 1:], parent.vals[mid + 1:]
            del parent.keys[mid:], parent.vals[mid + 1:]
            left = parent
        root = _Node(False)
        root.keys, root.vals = [sep], [left, right]
        self.root = root

    def items(self, lo=None, hi=None, lo_inclusive=True, hi_inclusive=True):
        """Yield (key, value) ascending, within optional bounds."""
        if lo is None:
            node, i = self.root, 0
            while not node.leaf:
                node = node.vals[0]
        else:
            node = self._find_leaf(lo)
            i = (bisect_left if lo_inclusive else bisect_right)(node.keys, lo)
        while node:
            keys = node.keys
            while i < len(keys):
                k = keys[i]
                if hi is not None and (k > hi or (k == hi and not hi_inclusive)):
                    return
                yield k, node.vals[i]
                i += 1
            node, i = node.next, 0

    def check(self):
        """Assert structural invariants (used by tests)."""
        depths = set()

        def walk(node, lo, hi, d):
            assert node.keys == sorted(node.keys)
            assert len(node.keys) <= self.max_keys
            for k in node.keys:
                assert (lo is None or k >= lo) and (hi is None or k < hi)
            if node.leaf:
                assert len(node.keys) == len(node.vals)
                depths.add(d)
                return
            assert len(node.vals) == len(node.keys) + 1
            bounds = [lo, *node.keys, hi]
            for c, a, b in zip(node.vals, bounds, bounds[1:]):
                walk(c, a, b, d + 1)

        walk(self.root, None, None, 1)
        assert len(depths) == 1, "leaves at different depths"
        keys = [k for k, _ in self.items()]
        assert keys == sorted(set(keys)) and len(keys) == self._size
