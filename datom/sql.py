"""Parser for INSERT and SELECT with a single equality predicate."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterator, NamedTuple, NoReturn, Optional

from .errors import DbError


@dataclass
class Predicate:
    column: str
    value: str


@dataclass
class Statement:
    kind: str
    table: str
    # Empty for SELECT * or an implicit INSERT column list.
    columns: list[str] = field(default_factory=list)
    values: list[list[str]] = field(default_factory=list)
    where: Optional[Predicate] = None


_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9$]*")
_INTEGER = re.compile(r"[+-]?[0-9]+")
_TRIVIA = re.compile(r"(?:[ \t\n\r\f\v]+|--[^\r\n]*)+")
_COMMENT_BOUNDARY = re.compile(r"/\*|\*/")
_RESERVED = {"select", "from", "where", "insert", "into", "values"}
_PUNCTUATION = {"*", ",", "(", ")", "=", ";"}


def _syntax_error(sql: str, position: int, message: str) -> NoReturn:
    byte = len(sql[:position].encode("utf-8")) + 1
    raise DbError("42601", f"{message} at byte {byte}")


def _validate_encoding(sql: str) -> None:
    nul = sql.find("\0")
    try:
        sql.encode("utf-8")
    except UnicodeEncodeError as error:
        if 0 <= nul < error.start:
            _syntax_error(sql, nul, "unexpected NUL character")
        byte = len(sql[:error.start].encode("utf-8")) + 1
        raise DbError("22021", f"invalid UTF-8 sequence at byte {byte}") from None
    if nul >= 0:
        _syntax_error(sql, nul, "unexpected NUL character")


class _Token(NamedTuple):
    kind: str
    position: int
    text: str = ""


class _Lexer:
    def __init__(self, sql: str) -> None:
        _validate_encoding(sql)
        self.sql = sql
        self.offset = 0

    def next(self) -> _Token:
        self._skip_trivia()
        start = self.offset
        if start == len(self.sql):
            return _Token("end", start)
        char = self.sql[start]
        word = _WORD.match(self.sql, start)
        if word:
            self.offset = word.end()
            return _Token("word", start, word.group().lower())
        integer = _INTEGER.match(self.sql, start)
        if integer:
            self.offset = integer.end()
            return _Token("integer", start, integer.group())
        self.offset += 1
        if char == "'":
            return self._quoted("string", start, char)
        if char == '"':
            return self._quoted("identifier", start, char)
        if char in _PUNCTUATION:
            return _Token(char, start)
        _syntax_error(self.sql, start, "unexpected character")

    def _skip_trivia(self) -> None:
        while self.offset < len(self.sql):
            match = _TRIVIA.match(self.sql, self.offset)
            if match:
                self.offset = match.end()
            elif self.sql.startswith("/*", self.offset):
                self._block_comment()
            else:
                return

    def _block_comment(self) -> None:
        start = self.offset
        self.offset += 2
        depth = 1
        while depth:
            boundary = _COMMENT_BOUNDARY.search(self.sql, self.offset)
            if boundary is None:
                _syntax_error(self.sql, start, "unterminated block comment")
            self.offset = boundary.end()
            depth += 1 if boundary.group() == "/*" else -1

    def _quoted(self, kind: str, start: int, delimiter: str) -> _Token:
        fragments: list[str] = []
        while True:
            closing = self.sql.find(delimiter, self.offset)
            if closing < 0:
                label = "string" if kind == "string" else "identifier"
                _syntax_error(self.sql, start, f"unterminated {label}")
            fragments.append(self.sql[self.offset:closing])
            self.offset = closing + 1
            if not self.sql.startswith(delimiter, self.offset):
                decoded = "".join(fragments)
                if kind == "identifier" and not decoded:
                    _syntax_error(self.sql, start, "empty quoted identifier")
                return _Token(kind, start, decoded)
            fragments.append(delimiter)
            self.offset += 1


class _Parser:
    def __init__(self, sql: str) -> None:
        self.lexer = _Lexer(sql)
        self.token = self.lexer.next()

    def statements(self) -> Iterator[Statement]:
        while self.token.kind != "end":
            if self._consume(";"):
                continue
            if self._keyword("insert"):
                statement = self._insert()
            elif self._keyword("select"):
                statement = self._select()
            else:
                self._fail("expected INSERT or SELECT")
            if self.token.kind not in {"end", ";"}:
                self._fail("expected ';' between statements")
            yield statement

    def _fail(self, message: str) -> NoReturn:
        _syntax_error(self.lexer.sql, self.token.position, message)

    def _consume(self, kind: str) -> bool:
        if self.token.kind != kind:
            return False
        self.token = self.lexer.next()
        return True

    def _expect(self, kind: str, message: str) -> None:
        if not self._consume(kind):
            self._fail(message)

    def _keyword(self, word: str) -> bool:
        if self.token.kind != "word" or self.token.text != word:
            return False
        self.token = self.lexer.next()
        return True

    def _take_text(self) -> str:
        text = self.token.text
        self.token = self.lexer.next()
        return text

    def _identifier(self) -> str:
        if self.token.kind == "identifier":
            return self._take_text()
        if self.token.kind == "word" and self.token.text not in _RESERVED:
            return self._take_text()
        self._fail("expected an identifier")

    def _literal(self) -> str:
        if self.token.kind not in {"string", "integer"}:
            self._fail("expected a string or integer literal")
        return self._take_text()

    def _columns(self) -> list[str]:
        columns = [self._identifier()]
        while self._consume(","):
            columns.append(self._identifier())
        return columns

    def _insert(self) -> Statement:
        if not self._keyword("into"):
            self._fail("expected INTO after INSERT")
        statement = Statement("insert", self._identifier())
        if self._consume("("):
            statement.columns = self._columns()
            self._expect(")", "expected ')' after column list")
        if not self._keyword("values"):
            self._fail("expected VALUES")
        while True:
            self._expect("(", "expected '(' before row values")
            row = [self._literal()]
            while self._consume(","):
                row.append(self._literal())
            self._expect(")", "expected ')' after row values")
            statement.values.append(row)
            if not self._consume(","):
                return statement

    def _select(self) -> Statement:
        columns = [] if self._consume("*") else self._columns()
        if not self._keyword("from"):
            self._fail("expected FROM after SELECT list")
        statement = Statement("select", self._identifier(), columns)
        if self._keyword("where"):
            column = self._identifier()
            self._expect("=", "expected '=' in WHERE condition")
            statement.where = Predicate(column, self._literal())
        return statement


def iter_statements(sql: str) -> Iterator[Statement]:
    """Validate encoding, then yield complete statements in source order.

    Syntax errors in later statements can occur after earlier statements have
    been yielded. Use parse_sql when the whole script must parse before use.
    """
    return _Parser(sql).statements()


def parse_sql(sql: str) -> list[Statement]:
    """Parse all statements, raising DbError for invalid SQL."""
    return list(iter_statements(sql))
