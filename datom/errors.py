"""Errors shared by the SQL engine and the PostgreSQL protocol adapter."""

from __future__ import annotations


class DbError(Exception):
    """A database error with a PostgreSQL-compatible SQLSTATE."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
