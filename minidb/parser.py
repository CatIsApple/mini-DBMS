"""SQL tokenizer + recursive-descent parser -> small AST.

Grammar (case-insensitive keywords, `--` comments, `;` separated):
  [EXPLAIN] SELECT (* | COUNT(*) | col, ...) FROM t [WHERE expr] [LIMIT n]
  INSERT INTO t [(col, ...)] VALUES (v, ...) [, (v, ...)]...
  expr := and (OR and)* ; and := atom (AND atom)*
  atom := ( expr ) | col (=|!=|<>|<|<=|>|>=) value | col IS [NOT] NULL
"""
import re
from dataclasses import dataclass

from .errors import ParseError

_TOKEN = re.compile(
    r"""(?P<ws>\s+|--[^\n]*)
    |(?P<str>'(?:[^']|'')*')
    |(?P<num>[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)
    |(?P<id>[A-Za-z_][A-Za-z0-9_]*)
    |(?P<op><=|>=|<>|!=|[=<>,();*-])
    |(?P<bad>.)""",
    re.X | re.S,
)
_RESERVED = {"select", "from", "where", "insert", "into", "values", "and", "or",
             "is", "not", "null", "limit", "explain"}
_MAX_DEPTH = 50


@dataclass(frozen=True)
class Insert:
    table: str
    columns: tuple | None
    rows: tuple


@dataclass(frozen=True)
class Select:
    table: str
    columns: tuple | None  # None = *
    count: bool
    where: tuple | None  # ("cmp",col,op,val) ("isnull",col,negated) ("and"|"or",[nodes])
    limit: int | None
    explain: bool


def tokenize(sql):
    out = []
    for m in _TOKEN.finditer(sql):
        kind, text = m.lastgroup, m.group()
        if kind == "ws":
            continue
        if kind == "str":
            out.append(("str", text[1:-1].replace("''", "'")))
        elif kind == "num":
            out.append(("num", int(text) if text.isdigit() else float(text)))
        elif kind == "id":
            out.append(("id", text.lower()))
        elif kind == "op":
            out.append(("op", text))
        elif text == "'":
            raise ParseError("unterminated string literal")
        else:
            raise ParseError(f"unexpected character {text!r}")
    out.append(("eof", None))
    return out


class _Parser:
    def __init__(self, toks):
        self.t, self.i, self.depth = toks, 0, 0

    def err(self, msg):
        kind, v = self.t[self.i]
        near = "end of input" if kind == "eof" else repr(v)
        return ParseError(f"{msg} (near {near})")

    def peek(self):
        return self.t[self.i]

    def advance(self):
        tok = self.t[self.i]
        if tok[0] != "eof":
            self.i += 1
        return tok

    def kw(self, *words):
        k, v = self.t[self.i]
        if k == "id" and v in words:
            self.i += 1
            return v
        return None

    def expect_kw(self, word):
        if not self.kw(word):
            raise self.err(f"expected {word.upper()}")

    def op(self, o):
        if self.t[self.i] == ("op", o):
            self.i += 1
            return True
        return False

    def expect_op(self, o):
        if not self.op(o):
            raise self.err(f"expected {o!r}")

    def ident(self):
        k, v = self.t[self.i]
        if k != "id" or v in _RESERVED:
            raise self.err("expected identifier")
        self.i += 1
        return v

    def value(self):
        neg = self.op("-")
        k, v = self.peek()
        if k == "num":
            self.advance()
            return -v if neg else v
        if neg:
            raise self.err("expected number after '-'")
        if k == "str":
            self.advance()
            return v
        if self.kw("null"):
            return None
        raise self.err("expected value")

    def statement(self):
        explain = bool(self.kw("explain"))
        if self.kw("select"):
            return self.select(explain)
        if explain:
            raise self.err("EXPLAIN supports SELECT only")
        if self.kw("insert"):
            return self.insert()
        raise self.err("unsupported statement (only SELECT / INSERT / EXPLAIN SELECT)")

    def select(self, explain):
        columns, count = None, False
        if self.op("*"):
            pass
        elif self.t[self.i] == ("id", "count") and self.t[self.i + 1] == ("op", "("):
            self.i += 2
            self.expect_op("*")
            self.expect_op(")")
            count = True
        else:
            columns = [self.ident()]
            while self.op(","):
                columns.append(self.ident())
            columns = tuple(columns)
        self.expect_kw("from")
        table = self.ident()
        where = self.expr() if self.kw("where") else None
        limit = None
        if self.kw("limit"):
            k, limit = self.advance()
            if k != "num" or not isinstance(limit, int):
                raise self.err("LIMIT needs a non-negative integer")
        return Select(table, columns, count, where, limit, explain)

    def insert(self):
        self.expect_kw("into")
        table = self.ident()
        columns = None
        if self.op("("):
            columns = [self.ident()]
            while self.op(","):
                columns.append(self.ident())
            self.expect_op(")")
            columns = tuple(columns)
        self.expect_kw("values")
        rows = []
        while True:
            self.expect_op("(")
            row = [self.value()]
            while self.op(","):
                row.append(self.value())
            self.expect_op(")")
            rows.append(tuple(row))
            if not self.op(","):
                break
        return Insert(table, columns, tuple(rows))

    def expr(self):
        self.depth += 1
        if self.depth > _MAX_DEPTH:
            raise self.err("expression nested too deeply")
        parts = [self.and_()]
        while self.kw("or"):
            parts.append(self.and_())
        self.depth -= 1
        return parts[0] if len(parts) == 1 else ("or", parts)

    def and_(self):
        parts = [self.atom()]
        while self.kw("and"):
            parts.append(self.atom())
        return parts[0] if len(parts) == 1 else ("and", parts)

    def atom(self):
        if self.op("("):
            e = self.expr()
            self.expect_op(")")
            return e
        col = self.ident()
        if self.kw("is"):
            neg = bool(self.kw("not"))
            self.expect_kw("null")
            return ("isnull", col, neg)
        k, o = self.peek()
        if k != "op" or o not in ("=", "!=", "<>", "<", "<=", ">", ">="):
            raise self.err("expected comparison operator")
        self.advance()
        return ("cmp", col, "!=" if o == "<>" else o, self.value())


def parse(sql):
    """Parse a whole script into a list of statements (all-or-nothing)."""
    p = _Parser(tokenize(sql))
    out = []
    while True:
        while p.op(";"):
            pass
        if p.peek()[0] == "eof":
            return out
        out.append(p.statement())
        if not p.op(";") and p.peek()[0] != "eof":
            raise p.err("expected ';' or end of input")
