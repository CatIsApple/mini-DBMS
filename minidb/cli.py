"""CLI SQL processor.

  python -m minidb.cli -d data -f queries.sql
  python -m minidb.cli -d data -e "SELECT * FROM users WHERE id = 1"
  python -m minidb.cli -d data            # interactive (or pipe SQL on stdin)
"""
import argparse
import json
import sys

from .engine import Database
from .errors import SQLError, StorageError
from .parser import parse


def format_table(columns, rows):
    cells = [[("NULL" if v is None else str(v)) for v in r] for r in rows]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(columns)]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    fmt = lambda vals: "| " + " | ".join(v.ljust(w) for v, w in zip(vals, widths)) + " |"
    return "\n".join([line, fmt(columns), line, *map(fmt, cells), line])


def show(res, as_json):
    if as_json:
        print(json.dumps(res.to_dict(), ensure_ascii=False))
    elif res.kind == "insert":
        print(f"INSERT {res.affected} (id {res.first_id}..{res.last_id})")
    else:
        print(format_table(res.columns, res.rows))
        if res.kind == "select":
            print(f"({len(res.rows)} row{'s' if len(res.rows) != 1 else ''}; {res.plan})")


def run_script(db, sql, as_json):
    """Returns 0 on success, 1 on SQL error. Parses everything before executing anything."""
    try:
        stmts = parse(sql)
        for s in stmts:
            show(db.run(s), as_json)
    except SQLError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="minidb", description="Mini DBMS SQL processor")
    ap.add_argument("-d", "--data", default="data", help="directory with schema.json and table files")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("-f", "--file", help="SQL script file")
    g.add_argument("-e", "--execute", help="SQL string")
    ap.add_argument("--json", action="store_true", help="one JSON object per result")
    ap.add_argument("--fsync", action="store_true", help="fsync after every INSERT")
    a = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    try:
        db = Database(a.data, fsync=a.fsync)
    except (StorageError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    with db:
        if a.execute is not None:
            return run_script(db, a.execute, a.json)
        if a.file:
            try:
                with open(a.file, encoding="utf-8-sig") as f:
                    sql = f.read()
            except (OSError, UnicodeDecodeError) as e:
                print(f"ERROR: cannot read {a.file}: {e}", file=sys.stderr)
                return 2
            return run_script(db, sql, a.json)
        if not sys.stdin.isatty():
            return run_script(db, sys.stdin.read(), a.json)
        print("minidb  (end statements with ';', Ctrl-D to quit)")
        buf = ""
        while True:
            try:
                buf += input("minidb> " if not buf else "   ...> ") + "\n"
            except EOFError:
                return 0
            if buf.strip().endswith(";"):
                run_script(db, buf, a.json)
                buf = ""


if __name__ == "__main__":
    sys.exit(main())
