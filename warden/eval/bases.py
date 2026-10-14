"""Base validation for an eval sample: sandbox only, no model calls.

For each task: the preloaded image opens, the unfixed base grades NOT resolved (the hidden
FAIL_TO_PASS tests fail), and, when the gold patch is supplied, the gold fix grades resolved. A task
that fails any of these is listed in `excluded.txt` with the reason, so the eval reports it as
"harness can't grade this" rather than as an agent failure.

The gold patch is fetched in memory from the dataset for this check and never written to disk.

    uv run python -m warden.eval.bases evals/swe_rebench/e1 --gold --workers 4
"""

from __future__ import annotations

import csv
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from warden.eval import swe_rebench as swe
from warden.runlog.events import EventLog
from warden.sandbox.box import Box, SandboxBackend

GOLD_PATCH = "/tmp/warden-gold.patch"
CSV_COLUMNS = ("task_id", "image_ok", "base_unresolved", "gold_resolved", "seconds", "note")

Grader = Callable[[Box, swe.Task], swe.Grade]
Preparer = Callable[[SandboxBackend, swe.Task, EventLog], Box]


@dataclass(frozen=True)
class BaseCheck:
    task_id: str
    image_ok: bool
    base_unresolved: bool | None  # None: never graded (image or install broken)
    gold_resolved: bool | None  # None: gold not checked
    seconds: int
    note: str
    p2p_red_on_base: bool = False

    @property
    def excluded_reason(self) -> str:
        if not self.image_ok:
            return "image missing or unusable"
        if self.base_unresolved is None:
            return "base could not be prepared or graded (install/grading error)"
        if not self.base_unresolved:
            return "unfixed base already passes the hidden tests"
        if self.gold_resolved is False:
            return "gold patch does not grade resolved (hidden tests unreliable here)"
        if self.gold_resolved is None and self.p2p_red_on_base:
            return "P2P tests fail on the unfixed base (gold not checked)"
        return ""

    def csv(self) -> dict[str, str]:
        def b(v: bool | None) -> str:
            return "" if v is None else str(int(v))

        return {
            "task_id": self.task_id,
            "image_ok": b(self.image_ok),
            "base_unresolved": b(self.base_unresolved),
            "gold_resolved": b(self.gold_resolved),
            "seconds": str(self.seconds),
            "note": self.note,
        }


def _counts(g: swe.Grade, task: swe.Task) -> str:
    return (
        f"F2P {g.f2p_passed}/{len(task.fail_to_pass)} P2P {g.p2p_passed}/{len(task.pass_to_pass)}"
    )


def check_base(
    backend: SandboxBackend,
    task: swe.Task,
    log: EventLog,
    *,
    gold_patch: str | None = None,
    grader: Grader = swe.grade,
    prepare: Preparer = swe.prepare_base,
) -> BaseCheck:
    t0 = time.perf_counter()

    def done(**kw: Any) -> BaseCheck:
        return BaseCheck(task_id=task.instance_id, seconds=int(time.perf_counter() - t0), **kw)

    try:
        Box.from_ref(backend, task.tag, log, phase="hunt")
    except Exception as err:  # noqa: BLE001 — a missing image is a row
        return done(
            image_ok=False, base_unresolved=None, gold_resolved=None, note=_err(err, task.tag)
        )
    try:
        base = prepare(backend, task, log)
        g = grader(base, task)
    except Exception as err:  # noqa: BLE001 — install/grading failure is a row
        # `use` can be lazy: a missing image surfaces as prepare_base's first run failing
        image_ok = not str(err).startswith("could not prepare")
        return done(image_ok=image_ok, base_unresolved=None, gold_resolved=None, note=_err(err, ""))
    note = f"base {_counts(g, task)}"
    p2p_red = g.p2p_passed < len(task.pass_to_pass)
    gold_resolved: bool | None = None
    if gold_patch is not None:
        try:
            applied = base.run(
                f"git apply {GOLD_PATCH}",
                cwd=swe.WORKDIR,
                keep=True,
                files={GOLD_PATCH: gold_patch.encode()},
            )
            if not applied.ok or applied.box is None:
                gold_resolved = False
                note += f"; gold patch did not apply: {applied.stderr.strip()[:120]}"
            else:
                gg = grader(applied.box, task)
                gold_resolved = gg.resolved
                note += f"; gold {_counts(gg, task)}"
        except Exception as err:  # noqa: BLE001
            gold_resolved = False
            note += f"; gold check error: {_err(err, '')}"
    return done(
        image_ok=True,
        base_unresolved=not g.resolved,
        gold_resolved=gold_resolved,
        note=note,
        p2p_red_on_base=p2p_red,
    )


def _err(err: BaseException, ctx: str) -> str:
    msg = f"{type(err).__name__}: {str(err)[:200]}".replace("\n", " ")
    return f"{ctx}: {msg}" if ctx else msg


def run_checks(
    tasks: list[swe.Task], check: Callable[[swe.Task], BaseCheck], *, workers: int = 4
) -> list[BaseCheck]:
    """`check` over `tasks` with at most `workers` in flight; results in task order."""
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(check, tasks))


def write_outputs(out_dir: Path, checks: list[BaseCheck]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "bases.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for c in checks:
            w.writerow(c.csv())
    lines = [f"{c.task_id}\t{c.excluded_reason}" for c in checks if c.excluded_reason]
    (out_dir / "excluded.txt").write_text("".join(f"{x}\n" for x in lines), encoding="utf-8")


_BASE_P2P = re.compile(r"^base F2P \d+/\d+ P2P (\d+)/(\d+)")


def read_checks(path: Path) -> list[BaseCheck]:
    """bases.csv → checks (P2P-on-base read back from the note); a missing file is none."""
    if not path.exists():
        return []

    def b(v: str) -> bool | None:
        return None if v == "" else v == "1"

    out = []
    with path.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            m = _BASE_P2P.match(r["note"])
            out.append(
                BaseCheck(
                    task_id=r["task_id"],
                    image_ok=r["image_ok"] == "1",
                    base_unresolved=b(r["base_unresolved"]),
                    gold_resolved=b(r["gold_resolved"]),
                    seconds=int(r["seconds"] or 0),
                    note=r["note"],
                    p2p_red_on_base=bool(m and int(m.group(1)) < int(m.group(2))),
                )
            )
    return out


def merge_checks(old: list[BaseCheck], new: list[BaseCheck]) -> list[BaseCheck]:
    """`new` replaces `old` by task id, in `old`'s order; unseen ids go last."""
    fresh = {c.task_id: c for c in new}
    merged = [fresh.pop(c.task_id, c) for c in old]
    return merged + [c for c in new if c.task_id in fresh]


def read_excluded(path: Path) -> dict[str, str]:
    """`excluded.txt` → {task_id: reason}; a missing file is no exclusions."""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            tid, _, reason = line.partition("\t")
            out[tid.strip()] = reason.strip()
    return out


# --------------------------------------------------------------------------- live


def gold_patches(n: int, *, seed: int, get: Callable[..., Any] | None = None) -> dict[str, str]:
    """{instance_id: gold patch} for the same rows `fetch_sample(n, seed)` picks. In memory only."""
    get = get or _patient_get
    offsets = swe.pick_offsets(swe.num_rows(get=get), n, seed=seed)
    out: dict[str, str] = {}
    for o in offsets:
        row = swe.fetch_rows(offset=o, length=1, get=get)[0]
        out[str(row["instance_id"])] = str(row["patch"])
    return out


def _patient_get(url: str, **kw: Any) -> Any:
    """httpx.get that waits out the datasets-server's 429s and 5xx."""
    import httpx

    resp = httpx.get(url, **kw)
    for i in range(8):
        if resp.status_code != 429 and resp.status_code < 500:
            break
        time.sleep(5 * (i + 1))
        resp = httpx.get(url, **kw)
    return resp


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from warden.config import load_settings
    from warden.sandbox.box import ContreeBackend

    p = argparse.ArgumentParser(prog="python -m warden.eval.bases", description=__doc__)
    p.add_argument("out", type=Path, help="eval dir holding tasks.jsonl")
    p.add_argument("--gold", action="store_true", help="also check the gold patch resolves")
    p.add_argument("--seed", type=int, default=7, help="the seed the sample was drawn with")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--only", nargs="+", default=None, help="re-check these ids; merge")
    args = p.parse_args(argv)

    tasks = swe.load_tasks(args.out / "tasks.jsonl")
    sample = len(tasks)
    if args.only:
        tasks = [t for t in tasks if t.instance_id in set(args.only)]
    gold: dict[str, str] = {}
    if args.gold:
        print(f"fetching {len(tasks)} gold patches (memory only)", file=sys.stderr)
        gold = gold_patches(sample, seed=args.seed)
        missing = {t.instance_id for t in tasks} - set(gold)
        if missing:
            print(f"gold rows differ from tasks.jsonl: {sorted(missing)}", file=sys.stderr)
            return 2
    s = load_settings()
    runs = s.runs_dir / "bases"

    def check(task: swe.Task) -> BaseCheck:
        backend = ContreeBackend(s.nebius_api_key, s.contree_base_url, s.nebius_project_id)
        log = EventLog(runs, task.instance_id)
        try:
            c = check_base(backend, task, log, gold_patch=gold.get(task.instance_id))
        finally:
            log.close()
        mark = "ok " if not c.excluded_reason else "BAD"
        print(f"{mark} {c.task_id:50} {c.seconds:4d}s  {c.note}", file=sys.stderr, flush=True)
        return c

    checks = run_checks(tasks, check, workers=args.workers)
    if args.only:
        checks = merge_checks(read_checks(args.out / "bases.csv"), checks)
    write_outputs(args.out, checks)
    bad = [c for c in checks if c.excluded_reason]
    print(f"{len(checks) - len(bad)}/{len(checks)} bases ok; {len(bad)} excluded", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
