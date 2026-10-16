"""Replay the new spec review (artefacts + main-behaviour tests) on E1's PASS candidates.

For each E1 PASS task: find its run dir (grade note in events.jsonl), the chosen candidate's
image, regenerate its diff in the sandbox, run spec_review live (one Ultra call), and apply
decide's guard rule to the recorded theater reasons. Read-only on runs/.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from warden.config import load_settings
from warden.eval.swe_rebench import load_tasks
from warden.llm.client import LLM
from warden.llm.cost import CostMeter
from warden.runlog.events import EventLog
from warden.sandbox.box import Box, ContreeBackend
from warden.verify.spec import spec_review
from warden.verify.verifier import main_guard

WT = Path("/Users/moizp/warden/.claude/worktrees/agent-a4b8826b05dffa3f0")
RUNS = Path("/Users/moizp/warden/runs")
OUT = Path(sys.argv[1])
IDS = sys.argv[2:]


def run_dir(task_id: str) -> Path | None:
    found = None
    for d in sorted(RUNS.glob("2026101[45]-*")):
        ev = d / "events.jsonl"
        if not (d / "artifacts" / "verdict.json").exists() or not ev.exists():
            continue
        if f'"task":"{task_id}"' in ev.read_text(encoding="utf-8").replace(" ", ""):
            found = d  # last one wins (re-runs)
    return found


def main() -> None:
    s = load_settings()
    tasks = {t.instance_id: t for t in load_tasks(WT / "evals/swe_rebench/e1/tasks.jsonl")}
    backend = ContreeBackend(s.nebius_api_key, s.contree_base_url, s.nebius_project_id)
    log = EventLog(OUT / "runs", "spec-replay")
    system = (WT / "prompts/spec.md").read_text(encoding="utf-8")
    rows = []
    for tid in IDS:
        d = run_dir(tid)
        if d is None:
            print(f"{tid}: no run dir", file=sys.stderr)
            continue
        verdict = json.loads((d / "artifacts" / "verdict.json").read_text())
        cand = json.loads((d / "artifacts" / "candidate.json").read_text())
        box = Box.from_ref(backend, cand["image_uuid"], log, phase="verify")
        shown = box.run("git add -A -N . && git diff HEAD", cwd="/testbed", keep=False)
        llm = LLM(s, CostMeter(cap_usd=0.5), log)
        res = spec_review(
            llm,
            tasks[tid].issue_text(),
            diff=shown.stdout,
            tests=cand["tests_added"],
            system=system,
        )
        guards = main_guard(res.main_tests, verdict.get("theater_tests", {}))
        row = {
            "task": tid,
            "run": d.name,
            "old": verdict["decision"],
            "unmet": res.unmet,
            "unclear": res.unclear,
            "main_tests": res.main_tests,
            "main_guard": guards,
            "artefacts": [a.model_dump() for a in res.review.artefacts],
        }
        rows.append(row)
        print(json.dumps(row), flush=True)
    (OUT / "spec_replay.json").write_text(json.dumps(rows, indent=2))
    log.close()


if __name__ == "__main__":
    main()
