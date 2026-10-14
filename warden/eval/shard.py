"""Split one eval's task list across k processes: `warden eval --shard i/k`.

Shard i takes the tasks whose position in tasks.jsonl is i mod k (stable for a committed task
list, whatever excluded.txt says) and writes `results.shard-i.csv` beside it; `warden report
<dir>/results.shard-*.csv` merges them. Tasks in excluded.txt (bases that cannot be graded) are
never run.
"""

from __future__ import annotations

import re

_SHARD = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


def parse_shard(spec: str) -> tuple[int, int]:
    """`"i/k"` → (i, k) with 0 ≤ i < k."""
    m = _SHARD.match(spec)
    if not m:
        raise ValueError(f"--shard must look like i/k (e.g. 0/5), not {spec!r}")
    i, k = int(m.group(1)), int(m.group(2))
    if k < 1 or not 0 <= i < k:
        raise ValueError(f"--shard {spec}: need 0 <= i < k")
    return i, k


def take[T](items: list[T], i: int, k: int) -> list[T]:
    return [x for n, x in enumerate(items) if n % k == i]


def csv_name(shard: tuple[int, int] | None) -> str:
    return "results.csv" if shard is None else f"results.shard-{shard[0]}.csv"
