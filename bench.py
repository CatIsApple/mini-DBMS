"""Phase 2 benchmark: bulk INSERT through the SQL processor, then index vs linear SELECT.

  python bench.py                       # 1,000,000 rows
  python bench.py --rows 200000 --keep  # smaller, keep the data under .bench/
"""
import argparse
import json
import os
import random
import shutil
import time

from minidb import Database


def timed(fn):
    t = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--batch", type=int, default=1000, help="rows per INSERT statement")
    ap.add_argument("--lookups", type=int, default=10_000)
    ap.add_argument("--scans", type=int, default=3)
    ap.add_argument("--dir", default=".bench")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    shutil.rmtree(a.dir, ignore_errors=True)
    os.makedirs(a.dir)
    with open(os.path.join(a.dir, "schema.json"), "w") as f:
        json.dump({"users": {"name": "TEXT", "age": "INT", "email": "TEXT"}}, f)

    rng = random.Random(42)
    db = Database(a.dir)

    def insert_all():
        for start in range(0, a.rows, a.batch):
            n = min(a.batch, a.rows - start)
            vals = ",".join(f"('user_{i}',{i % 90},'user_{i}@example.com')" for i in range(start + 1, start + n + 1))
            db.execute(f"INSERT INTO users (name, age, email) VALUES {vals};")

    _, t = timed(insert_all)
    print(f"INSERT   {a.rows:,} rows via SQL ({a.batch}/stmt): {t:.2f}s  ({a.rows / t:,.0f} rows/s)")
    db.close()
    _, t = timed(lambda: Database(a.dir).close())
    print(f"REOPEN   rebuild B+ tree index from file: {t:.2f}s")
    db = Database(a.dir)
    tbl = db.tables["users"]
    print(f"INDEX    {len(tbl.index):,} keys, tree height {tbl.index.height}")

    ids = [rng.randint(1, a.rows) for _ in range(a.lookups)]
    _, t_idx = timed(lambda: [db.execute(f"SELECT * FROM users WHERE id = {i}") for i in ids])
    per_idx = t_idx / a.lookups

    emails = [rng.randint(1, a.rows) for _ in range(a.scans)]

    def scans():
        return [db.execute(f"SELECT * FROM users WHERE email = 'user_{i}@example.com'")[0] for i in emails]

    res, t_scan = timed(scans)
    per_scan = t_scan / a.scans
    assert all(r.rows and r.rows[0][0] == i for r, i in zip(res, emails))
    assert "FULL TABLE SCAN" in res[0].plan
    assert "INDEX LOOKUP" in db.execute("SELECT * FROM users WHERE id = 1")[0].plan

    print(f"SELECT   WHERE id = ?      (B+ tree) : {per_idx * 1e6:10.1f} us/query  x{a.lookups:,}")
    print(f"SELECT   WHERE email = ?   (linear)  : {per_scan * 1e3:10.1f} ms/query  x{a.scans}")
    print(f"SPEEDUP  index is {per_scan / per_idx:,.0f}x faster")
    _, t_rng = timed(lambda: db.execute("SELECT COUNT(*) FROM users WHERE id >= 1000 AND id < 11000"))
    print(f"RANGE    10,000 ids via leaf chain : {t_rng * 1e3:.1f} ms")
    db.close()
    if not a.keep:
        shutil.rmtree(a.dir, ignore_errors=True)


if __name__ == "__main__":
    main()
