"""A7 spike: theater check of the A6 candidate (Fabric #196).

Done-criterion: the new tests FAIL on the pre-fix base fork (and pass on the candidate).
"""

import json
import shlex
import sys
from pathlib import Path

from warden.config import load_settings
from warden.runlog.events import EventLog
from warden.sandbox.box import Box, open_base
from warden.verify.theater import DEFAULT_CMD, parse_outcomes, theater_check

ROOT = Path(__file__).resolve().parents[1]
A6 = ROOT / "runs" / "a6-issue-196" / "artifacts" / "candidate.json"
PRE_FIX = "43f3851^"

s = load_settings()
cand = json.loads(A6.read_text())
log = EventLog(s.runs_dir, "a7-issue-196")
base0 = open_base(s, log, phase="verify")
co = base0.run(f"git checkout -q --detach {shlex.quote(PRE_FIX)}", cwd=s.workdir, keep=True)
assert co.ok and co.box is not None, co.stderr
pre = co.box
candidate = Box.from_ref(pre._b, cand["image_uuid"], log, phase="verify")  # noqa: SLF001

node_ids: list[str] = cand["tests_added"]
files = sorted({n.split("::", 1)[0] for n in node_ids})
new_tests = {
    f: candidate.run(f"cat {shlex.quote(f)}", cwd=s.workdir, keep=False).stdout.encode()
    for f in files
}

theater = theater_check(pre, new_tests, node_ids=node_ids, cwd=s.workdir, wrap=s.shell_wrap)
on_cand_cmd = f"{DEFAULT_CMD} {' '.join(shlex.quote(n) for n in node_ids)}"
on_cand = candidate.run(f"{s.shell_wrap} {shlex.quote(on_cand_cmd)}", cwd=s.workdir, keep=False)
log.close()

report = {
    "base_uuid": theater.base_uuid,
    "probe_uuid": theater.probe_uuid,
    "candidate_uuid": cand["image_uuid"],
    "label": theater.label,
    "failed_on_base": theater.failed_on_base,
    "inconclusive": theater.inconclusive,
    "on_base": theater.outcomes,
    "on_candidate": parse_outcomes(on_cand.stdout),
    "base_exit": theater.exit_code,
    "candidate_exit": on_cand.exit_code,
}
out = ROOT / "runs" / "a7-issue-196" / "artifacts"
out.mkdir(parents=True, exist_ok=True)
(out / "theater.json").write_text(json.dumps(report, indent=2))
(out / "on_base.txt").write_text(theater.stdout)
(out / "on_candidate.txt").write_text(on_cand.stdout[-4000:])
print(json.dumps(report, indent=1))
print(theater.stdout[-2500:], file=sys.stderr)
