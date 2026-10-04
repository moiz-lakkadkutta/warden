"""A5 spike: first real plan for Fabric #196, issue comments excluded (one is the human plan).

Context is read at PRE_FIX (the fix commit's parent), never the fixed HEAD.
Done-criterion: a Plan JSON with >= 1 citation whose URL resolves; diff vs the human plan comment
goes to docs/spike-A5.md (written by hand from this output).
"""

import json
import sys
from pathlib import Path

import httpx

from warden.config import load_settings
from warden.hunt.issue import fetch_issue
from warden.llm.client import LLM
from warden.llm.cost import CostMeter
from warden.plan.planner import plan_issue, sandbox_context
from warden.plan.research import research, tavily_client
from warden.runlog.events import EventLog
from warden.sandbox.box import open_base

ROOT = Path(__file__).resolve().parents[1]
ISSUE = 196
# Fixed by 43f3851 (PR #212); replay against its parent so the fix is not in the context.
PRE_FIX = "43f3851^"

s = load_settings()
log = EventLog(s.runs_dir, f"a5-issue-{ISSUE}-prefix")
issue = fetch_issue(s.target_repo, ISSUE, include_comments=False)
meter = CostMeter(cap_usd=s.max_usd_per_issue)
llm = LLM(s, meter, log)
box = open_base(s, log, phase="plan")
tav = tavily_client(s.tavily_api_key)

plan = plan_issue(
    llm,
    issue,
    context=sandbox_context(lambda cmd: box.run(cmd, cwd="/", keep=False).stdout, rev=PRE_FIX),
    search=lambda queries: research(queries, client=tav, log=log),
    log=log,
    prompt=(ROOT / "prompts" / "planner.md").read_text(),
)
(log.dir / "artifacts").mkdir(exist_ok=True)
(log.dir / "artifacts" / "plan.json").write_text(plan.model_dump_json(indent=2))

resolves = {}
for c in plan.citations:
    try:
        r = httpx.get(c.url, follow_redirects=True, timeout=20)
        resolves[c.url] = r.status_code
    except httpx.HTTPError as err:
        resolves[c.url] = str(err)
log.close()

print(plan.model_dump_json(indent=2))
print(
    json.dumps(
        {
            "run_id": log.run_id,
            "usd": meter.spent_usd,
            "by_model": meter.by_model,
            "resolves": resolves,
        },
        indent=1,
    ),
    file=sys.stderr,
)
