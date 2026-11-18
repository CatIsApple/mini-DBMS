"""Insert one million rows and compare indexed and sequential SELECTs."""

from __future__ import annotations

import math
import platform
import random
import tempfile
import time

from datom.engine import AccessPath, Engine
from datom.sql import parse_sql


RECORDS = 1_000_000
BATCH_SIZE = 1_000
SAMPLES = 31


def execute(engine: Engine, query: str):
    return engine.execute(parse_sql(query)[0])


def insert_records(engine: Engine) -> None:
    for first in range(1, RECORDS + 1, BATCH_SIZE):
        values = ",".join(
            f"('user{row_id}','user{row_id}@example.org')"
            for row_id in range(first, first + BATCH_SIZE)
        )
        result = execute(engine, f"INSERT INTO users(name,email) VALUES {values}")
        if result.tag != f"INSERT 0 {BATCH_SIZE}":
            raise AssertionError("unexpected inserted row count")


def measure(engine: Engine, row_id: int, indexed: bool) -> float:
    name = f"user{row_id}"
    condition = f"id = {row_id}" if indexed else f"name = '{name}'"
    query = f"SELECT id, name FROM users WHERE {condition}"
    started = time.perf_counter_ns()
    result = execute(engine, query)
    elapsed_us = (time.perf_counter_ns() - started) / 1_000
    expected_path = AccessPath.INDEX_LOOKUP if indexed else AccessPath.SEQUENTIAL_SCAN
    expected_examined = 1 if indexed else RECORDS
    if (result.access != expected_path or result.rows_examined != expected_examined
            or result.rows != [[str(row_id), name]]):
        raise AssertionError(f"incorrect query result or access path for {query}")
    return elapsed_us


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


def main() -> None:
    print(f"runtime={platform.python_implementation()} {platform.python_version()}", flush=True)
    with tempfile.TemporaryDirectory(prefix="datom-bench-") as directory:
        with Engine(directory) as engine:
            started = time.perf_counter()
            insert_records(engine)
            print(
                f"inserted={RECORDS} rows_per_statement={BATCH_SIZE} "
                f"insert_seconds={time.perf_counter() - started:.2f}",
                flush=True,
            )

        started = time.perf_counter()
        with Engine(directory) as engine:
            print(f"reopen_seconds={time.perf_counter() - started:.2f}", flush=True)
            measure(engine, RECORDS, False)
            measure(engine, RECORDS, True)
            random_ids = random.Random(20261118)
            indexed_times: list[float] = []
            scanned_times: list[float] = []
            for sample in range(SAMPLES):
                row_id = random_ids.randint(1, RECORDS)
                order = (True, False) if sample % 2 == 0 else (False, True)
                for indexed in order:
                    duration = measure(engine, row_id, indexed)
                    (indexed_times if indexed else scanned_times).append(duration)

        index_median = percentile(indexed_times, 0.5)
        scan_median = percentile(scanned_times, 0.5)
        print(f"queries_per_path={SAMPLES}")
        print(
            f"id_index median_us={index_median:.2f} "
            f"p95_us={percentile(indexed_times, 0.95):.2f} rows_examined=1"
        )
        print(
            f"name_scan median_us={scan_median:.2f} "
            f"p95_us={percentile(scanned_times, 0.95):.2f} rows_examined={RECORDS}"
        )
        print(f"median_scan_to_index_ratio={scan_median / index_median:.2f}x")


if __name__ == "__main__":
    main()
