"""results.csv → README results table (resolved %, false-PASS %, theater catches, $/task).

Reads one or more results CSVs (shards of one eval, or older attempts), tolerating column sets
from every harness version: missing `false_fail` / `unverified` are derived or default to 0, and
extra columns (best-of-N's `candidates`, `chosen`) are ignored. A task that appears more than once
keeps its last non-ERROR row (an ERROR row only when it has nothing else). Tasks listed in
`excluded.txt` (bases that cannot be graded; see `warden/eval/bases.py`) are reported, not counted.

The README's Results table is generated from here, never typed (plan §10).
"""

from __future__ import annotations

import csv
import math
import statistics
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

START = "<!-- results:start -->"
END = "<!-- results:end -->"
PASS_VERDICTS = ("PASS", "PASS_WITH_CONCERNS")


@dataclass(frozen=True)
class Result:
    task_id: str
    resolved: bool
    verdict: str
    false_pass: bool
    false_fail: bool
    unverified: bool
    theater_catch: bool
    tool_turns: int
    usd: float
    seconds: float
    error: str = ""
    verify_retry: str = ""  # "1:PASS;3:FAIL": candidates that took a verify retry, final verdict

    @property
    def is_error(self) -> bool:
        return self.verdict == "ERROR"

    @classmethod
    def from_csv(cls, row: dict[str, str]) -> Result:
        def flag(key: str, default: bool) -> bool:
            v = (row.get(key) or "").strip()
            return default if v == "" else v.lower() in ("1", "true", "yes")

        def num(key: str) -> float:
            v = (row.get(key) or "").strip()
            return float(v) if v else 0.0

        verdict = (row.get("verdict") or "").strip()
        resolved = flag("resolved", False)
        unverified = flag("unverified", False)
        return cls(
            task_id=row["task_id"].strip(),
            resolved=resolved,
            verdict=verdict,
            false_pass=flag("false_pass", verdict in PASS_VERDICTS and not resolved),
            false_fail=flag("false_fail", verdict == "FAIL" and resolved and not unverified),
            unverified=unverified,
            theater_catch=flag("theater_catch", False),
            tool_turns=int(num("tool_turns")),
            usd=num("usd"),
            seconds=num("seconds"),
            error=(row.get("error") or "").strip(),
            verify_retry=(row.get("verify_retry") or "").strip(),
        )


def read_results(paths: Iterable[Path]) -> list[Result]:
    """Merge results CSVs in order, one row per task (first-seen order)."""
    merged: dict[str, Result] = {}
    for path in paths:
        with Path(path).open(encoding="utf-8", newline="") as fh:
            for raw in csv.DictReader(fh):
                if not (raw.get("task_id") or "").strip():
                    continue
                r = Result.from_csv(raw)
                prev = merged.get(r.task_id)
                if prev is None or not r.is_error or prev.is_error:
                    merged[r.task_id] = r
    return list(merged.values())


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


@dataclass(frozen=True)
class Metrics:
    n: int
    resolved: int
    resolved_ci: tuple[float, float]
    passes: int
    pass_resolved: int
    false_pass: int
    fails: int
    false_fail: int
    unverified: int
    theater_catches: int
    errors: int
    usd_total: float
    usd_mean: float
    median_seconds: float
    mean_turns: float
    excluded: dict[str, str] = field(default_factory=dict)
    clean_passes: int = 0  # PASS without concerns
    clean_pass_resolved: int = 0
    verify_retries: int = 0  # candidates sent back once on gate / regression / spec feedback
    verify_retry_pass: int = 0  # ... that ended PASS / PASS_WITH_CONCERNS

    @property
    def resolved_pct(self) -> float:
        return self.resolved / self.n if self.n else 0.0

    @property
    def pass_precision(self) -> float:
        return self.pass_resolved / self.passes if self.passes else 0.0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["resolved_ci"] = [round(x, 4) for x in self.resolved_ci]
        d["resolved_pct"] = round(self.resolved_pct, 4)
        d["pass_precision"] = round(self.pass_precision, 4)
        return d


def compute(rows: list[Result], *, excluded: dict[str, str] | None = None) -> Metrics:
    excluded = dict(excluded or {})
    counted = [r for r in rows if r.task_id not in excluded]
    n = len(counted)
    resolved = sum(r.resolved for r in counted)
    passes = [r for r in counted if r.verdict in PASS_VERDICTS]
    fails = [r for r in counted if r.verdict == "FAIL"]
    ran = [r for r in counted if not r.is_error]
    usd_total = sum(r.usd for r in counted)
    return Metrics(
        n=n,
        resolved=resolved,
        resolved_ci=wilson(resolved, n),
        passes=len(passes),
        pass_resolved=sum(r.resolved for r in passes),
        false_pass=sum(r.false_pass for r in counted),
        fails=len(fails),
        false_fail=sum(r.false_fail for r in counted),
        unverified=sum(r.unverified for r in counted),
        theater_catches=sum(r.theater_catch for r in counted),
        errors=sum(r.is_error for r in counted),
        usd_total=usd_total,
        usd_mean=usd_total / n if n else 0.0,
        median_seconds=statistics.median(r.seconds for r in counted) if counted else 0.0,
        mean_turns=statistics.fmean(r.tool_turns for r in ran) if ran else 0.0,
        excluded={k: v for k, v in excluded.items()},
        clean_passes=sum(r.verdict == "PASS" for r in counted),
        clean_pass_resolved=sum(r.verdict == "PASS" and r.resolved for r in counted),
        verify_retries=sum(len(_retried(r)) for r in counted),
        verify_retry_pass=sum(
            sum(v in PASS_VERDICTS for v in _retried(r).values()) for r in counted
        ),
    )


def _retried(r: Result) -> dict[str, str]:
    """`1:PASS;3:FAIL` → {"1": "PASS", "3": "FAIL"}."""
    out: dict[str, str] = {}
    for part in r.verify_retry.split(";"):
        idx, _, verdict = part.partition(":")
        if idx.strip():
            out[idx.strip()] = verdict.strip()
    return out


def _pct(x: float) -> str:
    return f"{round(100 * x)}%"


def _dur(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s"


def markdown(m: Metrics, *, label: str = "results") -> str:
    out = [f"| metric | {label} |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in metric_rows(m)]
    return "\n".join(out) + "\n"


def markdown_compare(columns: list[tuple[str, Metrics]]) -> str:
    """One table, one column per eval (e.g. E1 vs E2); a metric one eval lacks shows "—"."""
    rows = [dict(metric_rows(m)) for _, m in columns]
    keys: list[str] = []
    for r in rows:
        keys += [k for k in r if k not in keys]
    out = ["| metric | " + " | ".join(label for label, _ in columns) + " |"]
    out.append("|---|" + "---|" * len(columns))
    out += [f"| {k} | " + " | ".join(r.get(k, "—") for r in rows) + " |" for k in keys]
    return "\n".join(out) + "\n"


def metric_rows(m: Metrics) -> list[tuple[str, str]]:
    lo, hi = m.resolved_ci
    rows = [
        (
            "Resolved",
            f"**{m.resolved} / {m.n}** ({_pct(m.resolved_pct)}, "
            f"95% CI {round(100 * lo)}–{round(100 * hi)}%)",
        ),
        (
            "Verifier PASS precision (PASS the hidden tests accept)",
            f"{m.pass_resolved} of {m.passes} PASS" + (
                f" ({_pct(m.pass_precision)})" if m.passes else ""
            ),
        ),
        (
            "Clean PASS precision (PASS without concerns)",
            f"{m.clean_pass_resolved} of {m.clean_passes} PASS" + (
                f" ({_pct(m.clean_pass_resolved / m.clean_passes)})" if m.clean_passes else ""
            ),
        ),
        ("Verifier PASS that hidden tests reject (false PASS)",
         f"{m.false_pass} of {m.passes} PASS"),
        ("Verifier FAIL on a fix hidden tests accept (false FAIL)",
         f"{m.false_fail} of {m.fails} FAIL"),
        ("Unverified (FAIL only because the suite timed out)", str(m.unverified)),
        ("Theater catches (new test already passes on the unfixed base)", str(m.theater_catches)),
        ("Errors (run did not reach a verdict)", str(m.errors)),
        ("Cost", f"**${m.usd_mean:.2f} / task**, ${m.usd_total:.2f} total"),
        ("Median wall-clock per task", _dur(m.median_seconds)),
        ("Mean tool turns (runs that reached a verdict)", f"{m.mean_turns:.1f}"),
    ]  # fmt: skip
    if m.verify_retries:
        rows.insert(
            -3,
            (
                "Verify retries (valid repro, red gate / regression / spec: sent back once)",
                f"{m.verify_retries} candidates, {m.verify_retry_pass} then PASS",
            ),
        )
    if m.excluded:
        rows.append(("Excluded (base cannot be graded; see excluded.txt)", str(len(m.excluded))))
    return rows


def replace_block(text: str, block: str) -> str:
    """Replace what sits between the results markers with `block`."""
    i, j = text.find(START), text.find(END)
    if i < 0 or j < i:
        raise ValueError(f"README has no {START} … {END} markers")
    body = block if block.endswith("\n") else block + "\n"
    return text[: i + len(START)] + "\n" + body + text[j:]


def read_excluded(path: Path) -> dict[str, str]:
    from warden.eval.bases import read_excluded as read

    return read(path)
