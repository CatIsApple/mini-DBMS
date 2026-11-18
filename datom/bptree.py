"""An in-memory B+ tree mapping integer record IDs to file offsets."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class _Node:
    leaf: bool
    keys: List[int] = field(default_factory=list)
    offsets: List[int] = field(default_factory=list)
    children: List[_Node] = field(default_factory=list)
    next_leaf: Optional[_Node] = None


class BPlusTree:
    """Store offsets only in leaves, with separators in internal nodes.

    The order is the maximum number of children in an internal node.
    Every node holds at most order - 1 keys. Duplicate inserts replace
    the existing offset. The caller is responsible for synchronization.
    """

    def __init__(self, order: int = 64):
        if type(order) is not int or order < 3:
            raise ValueError("B+ tree order must be an integer of at least 3")
        self._order = order
        self._root = _Node(leaf=True)

    def find(self, key: int) -> Optional[int]:
        node = self._root
        while not node.leaf:
            node = node.children[bisect_right(node.keys, key)]
        slot = bisect_left(node.keys, key)
        if slot < len(node.keys) and node.keys[slot] == key:
            return node.offsets[slot]
        return None

    def insert(self, key: int, offset: int) -> None:
        split = self._insert(self._root, key, offset)
        if split is not None:
            separator, right = split
            self._root = _Node(
                leaf=False, keys=[separator], children=[self._root, right]
            )

    def _insert(self, node: _Node, key: int, offset: int) -> Optional[Tuple[int, _Node]]:
        if node.leaf:
            slot = bisect_left(node.keys, key)
            if slot < len(node.keys) and node.keys[slot] == key:
                node.offsets[slot] = offset
                return None
            node.keys.insert(slot, key)
            node.offsets.insert(slot, offset)
        else:
            child = bisect_right(node.keys, key)
            split = self._insert(node.children[child], key, offset)
            if split is None:
                return None
            separator, right = split
            node.keys.insert(child, separator)
            node.children.insert(child + 1, right)

        if len(node.keys) < self._order:
            return None
        return self._split(node)

    @staticmethod
    def _split(left: _Node) -> Tuple[int, _Node]:
        middle = len(left.keys) // 2
        separator = left.keys[middle]
        if left.leaf:
            # The promoted separator remains in its leaf as a record key.
            right = _Node(
                leaf=True,
                keys=left.keys[middle:],
                offsets=left.offsets[middle:],
                next_leaf=left.next_leaf,
            )
            left.keys = left.keys[:middle]
            left.offsets = left.offsets[:middle]
            left.next_leaf = right
        else:
            right = _Node(
                leaf=False,
                keys=left.keys[middle + 1:],
                children=left.children[middle + 1:],
            )
            left.keys = left.keys[:middle]
            left.children = left.children[:middle + 1]
        return separator, right
