"""Honest micro-benchmark: 10k span inserts + full-text search.

    python bench/bench.py [--spans 10000] [--db /tmp/bench.db]

Measures the two write modes agents actually use (one commit per span, and a
batch() transaction), then search latency over the resulting file.
"""

from __future__ import annotations

import argparse
import os
import platform
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

import sqlite3  # noqa: E402

from rundb import connect  # noqa: E402

WORDS = ("deploy timeout retry pytest import module missing region auth token cache build lint "
         "schema migrate request response parse json yaml docker image push pull branch merge "
         "config env secret network socket refused permission denied quota limit").split()
TOOLS = ["bash", "pytest", "git", "http_get", "read_file", "write_file", "search", "docker"]


def text(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n))


def insert(db, n: int, rng: random.Random, batched: bool) -> float:
    runs = [db.start_run("bench", goal=text(rng, 6)) for _ in range(max(1, n // 100))]
    t0 = time.perf_counter()

    def body() -> None:
        for i in range(n):
            err = f"{rng.choice(WORDS)} error: {text(rng, 8)}" if rng.random() < 0.1 else None
            db.log_span(runs[i % len(runs)], "tool", rng.choice(TOOLS),
                        input=text(rng, 20), output=None if err else text(rng, 40), error=err,
                        tokens_in=rng.randint(50, 2000), tokens_out=rng.randint(10, 500))

    if batched:
        with db.batch():
            body()
    else:
        body()
    return time.perf_counter() - t0


def pct(values: list[float], p: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spans", type=int, default=10_000)
    ap.add_argument("--queries", type=int, default=200)
    args = ap.parse_args()
    rng = random.Random(42)
    tmp = Path(tempfile.mkdtemp(prefix="rundb-bench-"))

    print(f"python {platform.python_version()}  sqlite {sqlite3.sqlite_version}  "
          f"{platform.system()} {platform.machine()}  cpus={os.cpu_count()}")
    print(f"spans={args.spans}\n")

    results = {}
    for mode, batched in (("one commit per span", False), ("batch() transaction", True)):
        path = tmp / f"{'batch' if batched else 'single'}.db"
        db = connect(path)
        secs = insert(db, args.spans, rng, batched)
        results[mode] = (secs, path, db)
        print(f"insert, {mode:22} {secs:7.2f}s  {args.spans / secs:9,.0f} spans/s")

    _, path, db = results["batch() transaction"]
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size = sum(os.path.getsize(str(path) + s) for s in ("", "-wal") if os.path.exists(str(path) + s))
    print(f"\nfile size after {args.spans} spans: {size / 1e6:.1f} MB ({size / args.spans:.0f} bytes/span incl. FTS)")

    for label, make in (
        ("1 term", lambda: rng.choice(WORDS)),
        ("2 terms", lambda: f"{rng.choice(WORDS)} {rng.choice(WORDS)}"),
        ("filtered by run", None),
    ):
        lat = []
        runs = [r.id for r in db.list_runs(limit=200)]
        for _ in range(args.queries):
            t0 = time.perf_counter()
            if make is None:
                db.search(rng.choice(WORDS), run_id=rng.choice(runs), limit=20)
            else:
                db.search(make(), limit=20)
            lat.append((time.perf_counter() - t0) * 1000)
        print(f"search {label:16} p50 {statistics.median(lat):6.2f} ms   p95 {pct(lat, 95):6.2f} ms")

    t0 = time.perf_counter()
    db.what_failed("bench")
    print(f"what_failed(workspace)   {(time.perf_counter() - t0) * 1000:6.2f} ms")
    for _, _, d in results.values():
        d.close()


if __name__ == "__main__":
    main()
