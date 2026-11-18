"""Execute SQL files or command-line SQL through the shared database engine."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from .engine import Engine, ResultSink
from .errors import DbError
from .sql import iter_statements


def print_row(row: list[str]) -> None:
    escaped = [
        value.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")
        for value in row
    ]
    print("\t".join(escaped))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data"))
    source = parser.add_mutually_exclusive_group()
    source.add_argument("-f", "--file", type=Path, help="UTF-8 SQL file")
    source.add_argument("-c", "--command", help="SQL to execute")
    args = parser.parse_args(argv)

    try:
        if args.file is not None:
            script = args.file.read_bytes().decode("utf-8")
        elif args.command is not None:
            script = args.command
        else:
            script = sys.stdin.buffer.read().decode("utf-8")

        with Engine(args.data) as engine:
            sink = ResultSink(columns=print_row, row=print_row)
            for statement in iter_statements(script):
                result = engine.execute(statement, sink=sink)
                print(result.tag)
        sys.stdout.flush()
        return 0
    except DbError as error:
        print(f"ERROR [{error.code}] {error}", file=sys.stderr)
    except UnicodeError:
        print("ERROR [22021] SQL input must be valid UTF-8", file=sys.stderr)
    except BrokenPipeError:
        # Avoid a second error when Python flushes stdout during shutdown.
        sys.stdout = open("/dev/null", "w", encoding="utf-8")
    except OSError as error:
        print(f"ERROR [58030] {error}", file=sys.stderr)
    return 1
