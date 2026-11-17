class SQLError(Exception):
    """User-facing SQL problem (bad syntax, unknown table, type mismatch...)."""

    completed = 0  # statements of a multi-statement script that ran before the failure


class ParseError(SQLError):
    pass


class ExecutionError(SQLError):
    pass


class StorageError(Exception):
    """Data file is corrupt or unusable. Not the caller's SQL fault."""
