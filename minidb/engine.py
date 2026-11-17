"""Executor: schema loading, type checking, query planning (B+ tree vs full scan)."""
import json
import math
import operator
import os
import re
from dataclasses import dataclass, field

from .errors import ExecutionError, StorageError
from .parser import Insert, Select, parse
from .storage import Table

_IDENT = re.compile(r"[a-z_][a-z0-9_]*\Z")
_TYPES = ("INT", "FLOAT", "TEXT")
_OPS = {"=": operator.eq, "!=": operator.ne, "<": operator.lt,
        "<=": operator.le, ">": operator.gt, ">=": operator.ge}


@dataclass
class Result:
    kind: str  # insert | select | explain
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    affected: int = 0
    first_id: int | None = None
    last_id: int | None = None
    plan: str | None = None

    def to_dict(self):
        d = {"kind": self.kind}
        if self.kind == "insert":
            d.update(affected=self.affected, first_id=self.first_id, last_id=self.last_id)
        else:
            d.update(columns=self.columns, rows=self.rows, plan=self.plan)
        return d


def load_schema(data_dir):
    """schema.json: {"users": {"name": "TEXT", "age": "INT"}}  (`id` is implicit, auto-assigned)."""
    path = os.path.join(data_dir, "schema.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise StorageError(f"schema file not found: {path}") from None
    except ValueError as e:
        raise StorageError(f"{path}: invalid JSON ({e})") from None
    if not isinstance(raw, dict) or not raw:
        raise StorageError(f"{path}: must be a non-empty object of tables")
    schema = {}
    for t, cols in raw.items():
        if not _IDENT.match(t.lower()) or not isinstance(cols, dict) or not cols:
            raise StorageError(f"{path}: bad table definition {t!r}")
        out = {}
        for c, ty in cols.items():
            c, ty = c.lower(), str(ty).upper()
            if not _IDENT.match(c) or c == "id" or c in out or ty not in _TYPES:
                raise StorageError(
                    f"{path}: bad column {t}.{c} {ty} (types: {', '.join(_TYPES)}; 'id' is reserved)")
            out[c] = ty
        schema[t.lower()] = out
    return schema


def _coerce(v, ctype, col):
    if v is None:
        return None
    try:
        if ctype == "TEXT" and isinstance(v, str):
            return v
        if ctype == "INT" and isinstance(v, int) and -(2**63) <= v < 2**63:
            return v
        if ctype == "FLOAT" and isinstance(v, (int, float)) and math.isfinite(v):
            return float(v)
    except OverflowError:
        pass
    raise ExecutionError(f"column '{col}' is {ctype}: invalid value {v!r}")


class Database:
    def __init__(self, data_dir, fsync=False):
        self.tables = {}
        try:
            for name, cols in load_schema(data_dir).items():
                self.tables[name] = Table(name, cols, os.path.join(data_dir, name + ".tbl"), fsync)
        except BaseException:
            self.close()
            raise

    def close(self):
        for t in self.tables.values():
            t.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def schema(self):
        return {n: dict(zip(t.columns, t.types)) for n, t in self.tables.items()}

    def execute(self, sql):
        """Parse the whole script first (syntax errors apply nothing), then run in order."""
        results = []
        for stmt in parse(sql):
            try:
                results.append(self.run(stmt))
            except ExecutionError as e:
                e.completed = len(results)
                raise
        return results

    def run(self, stmt):
        return self._insert(stmt) if isinstance(stmt, Insert) else self._select(stmt)

    def _table(self, name):
        try:
            return self.tables[name]
        except KeyError:
            raise ExecutionError(f"unknown table '{name}'") from None

    def _insert(self, s: Insert):
        t = self._table(s.table)
        cols = s.columns or t.user_cols
        for c in cols:
            if c == "id":
                raise ExecutionError("'id' is auto-assigned and cannot be inserted")
            if c not in t.user_cols:
                raise ExecutionError(f"unknown column '{c}' in table '{t.name}'")
        if len(set(cols)) != len(cols):
            raise ExecutionError("duplicate column in INSERT column list")
        pos = [t.user_cols.index(c) for c in cols]
        types = t.types[1:]
        rows = []
        for r in s.rows:
            if len(r) != len(cols):
                raise ExecutionError(f"expected {len(cols)} values, got {len(r)}")
            full = [None] * len(t.user_cols)
            for p, v in zip(pos, r):
                full[p] = _coerce(v, types[p], t.user_cols[p])
            rows.append(full)
        first, last = t.insert_many(rows)
        return Result("insert", affected=len(rows), first_id=first, last_id=last)

    def _select(self, s: Select):
        t = self._table(s.table)
        idx = {c: i for i, c in enumerate(t.columns)}
        want = list(t.columns) if s.columns is None else list(s.columns)
        for c in want:
            if c not in idx:
                raise ExecutionError(f"unknown column '{c}' in table '{t.name}'")
        pred = _compile(s.where, idx, t.types) if s.where else None
        plan = _plan(s.where)
        desc = _describe(plan)
        if s.explain:
            return Result("explain", columns=["plan"], rows=[[desc]], plan=desc)
        if s.count:
            return Result("select", columns=["count"], rows=[[t.count(plan, pred)]], plan=desc)
        rows = t.select(plan, pred, s.limit)
        if s.columns is not None:
            ix = [idx[c] for c in want]
            rows = [[r[i] for i in ix] for r in rows]
        return Result("select", columns=want, rows=rows, plan=desc)


def _compile(node, idx, types):
    kind = node[0]
    if kind in ("and", "or"):
        fs = [_compile(n, idx, types) for n in node[1]]
        if kind == "and":
            def f(r):
                for g in fs:
                    if not g(r):
                        return False
                return True
        else:
            def f(r):
                for g in fs:
                    if g(r):
                        return True
                return False
        return f
    col = node[1]
    if col not in idx:
        raise ExecutionError(f"unknown column '{col}'")
    i = idx[col]
    if kind == "isnull":
        neg = node[2]
        return lambda r: (r[i] is None) != neg
    _, _, op, v = node
    if v is None:
        return lambda r: False  # SQL: comparing with NULL is never true
    ok = isinstance(v, str) if types[i] == "TEXT" else not isinstance(v, str)
    if not ok:
        raise ExecutionError(f"cannot compare {types[i]} column '{col}' with {v!r}")
    cmp = _OPS[op]
    return lambda r: (x := r[i]) is not None and cmp(x, v)


def _plan(where):
    """Use the B+ tree for top-level AND terms on id; the full predicate still filters afterwards."""
    if where is None:
        return ("scan",)
    terms = where[1] if where[0] == "and" else [where]
    lo = hi = None
    for n in terms:
        if n[0] != "cmp" or n[1] != "id" or n[3] is None or isinstance(n[3], str):
            continue
        op, v = n[2], n[3]
        if op == "=":
            return ("lookup", v)
        if op in (">", ">=") and lo is None:
            lo = (v, op == ">=")
        elif op in ("<", "<=") and hi is None:
            hi = (v, op == "<=")
    if lo or hi:
        return ("range", lo and lo[0], hi and hi[0], lo[1] if lo else True, hi[1] if hi else True)
    return ("scan",)


def _describe(plan):
    if plan[0] == "lookup":
        return f"INDEX LOOKUP (B+ tree) id = {plan[1]}"
    if plan[0] == "range":
        _, lo, hi, li, hin = plan
        return (f"INDEX RANGE SCAN (B+ tree) id in {'[' if li and lo is not None else '('}"
                f"{'-inf' if lo is None else lo}, {'+inf' if hi is None else hi}{']' if hin and hi is not None else ')'}")
    return "FULL TABLE SCAN"
