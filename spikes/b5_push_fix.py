"""B5 live: push the A6 candidate (Fabric #196 replay) as a fix branch on Fabric and open a
draft PR against the replay base branch, with only the evidence that was actually produced.
"""

import json
import subprocess
import sys
from pathlib import Path

from warden.config import load_settings
from warden.integrate.branch import export_patch, push_fix_branch
from warden.integrate.pr import PENDING_LABEL, open_draft_pr
from warden.runlog.events import EventLog
from warden.sandbox.box import Box, open_base
from warden.schemas import Plan

ROOT = Path(__file__).resolve().parents[1]
A6 = ROOT / "runs" / "a6-issue-196" / "artifacts"
A7 = ROOT / "runs" / "a7-issue-196" / "artifacts" / "theater.json"
BASE_SHA = "fb19cde68d37146f6ef4228507fa122a520b6906"  # 43f3851^ — #196 fixed in 43f3851
TITLE = "Dashboard sales/GST KPIs count DRAFT invoices"
MODELS = "Nemotron 3 Ultra / Super / 3.5 Lightning on Nebius Token Factory"

s = load_settings()
cand = json.loads((A6 / "candidate.json").read_text())
plan = Plan.model_validate_json((A6 / "plan.json").read_text())
theater = json.loads(A7.read_text())
log = EventLog(s.runs_dir, "b5-issue-196")

box = Box.from_ref(open_base(s, log)._b, cand["image_uuid"], log, phase="pr")  # noqa: SLF001
patch = export_patch(box, cwd=s.workdir, issue=196, title=TITLE)
log.artifact("candidate.patch", patch)
pushed = push_fix_branch(
    s.target_repo,
    patch,
    issue=196,
    run_id="a6",
    base_rev=BASE_SHA,
    cache_dir=s.runs_dir / ".repos" / s.target_repo.replace("/", "__"),
)
print(f"pushed {pushed.head} ({pushed.sha[:7]}) on {pushed.base}", file=sys.stderr)

subprocess.run(
    [
        "gh",
        "label",
        "create",
        PENDING_LABEL,
        "-R",
        s.target_repo,
        "--color",
        "B60205",
        "--description",
        "Warden: money/tax/schema/security change - a human must review",
    ],
    capture_output=True,
    text=True,
    check=False,
)  # exists already -> non-zero, fine

on_base, on_cand = theater["on_base"], theater["on_candidate"]
rows = "\n".join(
    f"| `{n.split('::')[1]}` | {on_base.get(n, '?')} | {on_cand.get(n, '?')} |"
    for n in cand["tests_added"]
)
body = f"""## Warden evidence — #196 {TITLE}

> **Replay of a closed issue.** #196 was fixed by a human in 43f3851 (#212). Warden re-solved it
> from the issue text alone (comments hidden) on the code as it was before that fix
> (`{pushed.base}` = {BASE_SHA[:7]}). This draft is evidence for the hackathon, **not** for merging.

**Verdict: not issued** — the gate (full suite, mypy, ruff, alembic, OpenAPI) has not run on this
candidate yet. Only what actually ran is reported below.

| Check | Result |
|---|---|
| New tests on unfixed base | **FAIL — as required** ({theater["label"]}) |
| New tests on candidate | 3 passed |
| Existing `test_dashboard_service.py` on candidate | passed |
| Full suite · mypy · ruff · alembic · OpenAPI | **not run** |
| Risk class | {", ".join(plan.risk_class)} → **{PENDING_LABEL}** |
| Cost | $0.15 (plan $0.03 · implement $0.13) · {MODELS} |

| New test | unfixed base | candidate |
|---|---|---|
{rows}

The repro fails on the unfixed base with `assert Decimal('10498.95') == Decimal('0')`, the number
from the 2026-09-02 QA campaign.

**Plan summary:** {plan.summary}

Sandboxes: candidate `{cand["image_uuid"]}` · theater probe `{theater["probe_uuid"]}`.
"""
pr = open_draft_pr(
    s.target_repo,
    pushed.head,
    pushed.base,
    f"[Warden replay] #196 {TITLE}",
    body,
    labels=[PENDING_LABEL] if plan.needs_human else [],
    log=log,
)
log.close()
print(
    json.dumps(
        {"head": pushed.head, "base": pushed.base, "sha": pushed.sha, "pr": pr.url}, indent=1
    )
)
