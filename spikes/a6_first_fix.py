"""A6 spike: first fix of Fabric #196 in a Sandboxes fork, replayed at the pre-fix commit.

Done-criterion: candidate box exists; `pytest <new tests>` passes in it; cost < $3.
Bonus (A7 preview): the same tests on the pre-fix base.
"""

import json
import shlex
import sys
import time
from pathlib import Path

from warden.config import load_settings
from warden.fix.implementer import run_implementer
from warden.fix.tools import ToolDispatcher
from warden.hunt.issue import fetch_issue
from warden.llm.client import LLM
from warden.llm.cost import CostMeter
from warden.plan.planner import plan_issue, sandbox_context
from warden.plan.research import research, tavily_client
from warden.runlog.events import EventLog
from warden.sandbox.box import Box, open_base

ROOT = Path(__file__).resolve().parents[1]
ISSUE = 196
PRE_FIX = "43f3851^"  # #196 was fixed in 43f3851 (PR #212)
TURNS = 40  # run 3 used all 25 while debugging its own test


class Progress:
    def step(self, text: str) -> None:
        print(f"  · {text}", file=sys.stderr, flush=True)

    def cost(self, usd: float) -> None:
        return None


t0 = time.perf_counter()
s = load_settings()
log = EventLog(s.runs_dir, f"a6-issue-{ISSUE}")
meter = CostMeter(cap_usd=s.max_usd_per_issue)
llm = LLM(s, meter, log)
issue = fetch_issue(s.target_repo, ISSUE, include_comments=False)

base = open_base(s, log, phase="fix")
co = base.run(f"git checkout -q --detach {shlex.quote(PRE_FIX)}", cwd=s.workdir, keep=True)
assert co.ok and co.box is not None, co.stderr
pre = co.box
print(f"pre-fix box {pre.uuid}", file=sys.stderr, flush=True)

tav = tavily_client(s.tavily_api_key)
plan = plan_issue(
    llm,
    issue,
    context=sandbox_context(lambda cmd: pre.run(cmd, cwd="/", keep=False).stdout),
    search=lambda queries: research(queries, client=tav, log=log),
    log=log,
    prompt=(ROOT / "prompts" / "planner.md").read_text(),
)
print(f"plan: {plan.summary[:300]}", file=sys.stderr, flush=True)
plan_usd = meter.spent_usd

dispatcher = ToolDispatcher(log, cwd=s.workdir, shell_wrap=s.shell_wrap)
cand = run_implementer(
    llm, pre, plan, log, dispatcher=dispatcher, max_tool_turns=TURNS, progress=Progress()
)
candidate = Box.from_ref(pre._b, cand.image_uuid, log, phase="verify")  # noqa: SLF001

verify = {}
if cand.tests_added:
    cmd = "pytest -q -p no:cacheprovider " + " ".join(shlex.quote(t) for t in cand.tests_added)
    on_cand = dispatcher.run(candidate, cmd, timeout=900)
    on_base = dispatcher.run(pre, cmd, timeout=900)
    verify = {
        "candidate": {"exit": on_cand.exit_code, "tail": on_cand.stdout[-600:]},
        "pre_fix_base": {"exit": on_base.exit_code, "tail": on_base.stdout[-600:]},
    }
diff = candidate.run("git diff --stat; git status --short", cwd=s.workdir, keep=False).stdout

art = log.dir / "artifacts"
art.mkdir(exist_ok=True)
(art / "plan.json").write_text(plan.model_dump_json(indent=2))
(art / "candidate.json").write_text(cand.model_dump_json(indent=2))
full_diff = candidate.run("git diff", cwd=s.workdir, keep=False).stdout
(art / "candidate.diff").write_text(full_diff)
log.close()

print(
    json.dumps(
        {
            "run_id": log.run_id,
            "candidate": cand.model_dump(),
            "plan_usd": plan_usd,
            "total_usd": meter.spent_usd,
            "by_model": meter.by_model,
            "verify": verify,
            "diff_stat": diff,
            "seconds": round(time.perf_counter() - t0, 1),
        },
        indent=1,
    )
)
