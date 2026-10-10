"""B4 live: the gate on the A6 candidate (Fabric #196, = the code in Fabric-Inventory#220).

Done-criterion: the gate runs on the candidate box and produces the Verdict fields. Expected
here: ruff red (W292, the one failure Fabric's own CI found on #220), everything else green.
"""

import json
import time
from pathlib import Path

from warden.config import load_settings
from warden.runlog.events import EventLog
from warden.sandbox.box import Box, open_base
from warden.verify.gate import PROFILES, run_gate

ROOT = Path(__file__).resolve().parents[1]
cand = json.loads((ROOT / "runs" / "a6-issue-196" / "artifacts" / "candidate.json").read_text())

s = load_settings()
log = EventLog(s.runs_dir, "b4-gate-196")
box = Box.from_ref(open_base(s, log)._b, cand["image_uuid"], log, phase="verify")  # noqa: SLF001
t0 = time.perf_counter()
result = run_gate(
    box, log, commands=PROFILES[s.gate_profile], cwd=s.workdir, wrap=s.shell_wrap, timeout=1800
)
log.close()
report = {
    "candidate": cand["image_uuid"],
    "verdict_fields": result.verdict_fields(),
    "ok": result.ok,
    "not_verified": result.not_verified,
    "steps": {st.name: {"exit": st.exit_code, "s": round(st.ms / 1000, 1)} for st in result.steps},
    "seconds": round(time.perf_counter() - t0, 1),
}
out = ROOT / "runs" / "b4-gate-196" / "artifacts"
out.mkdir(parents=True, exist_ok=True)
(out / "gate.json").write_text(json.dumps(report, indent=2))
(out / "steps.txt").write_text(
    "\n\n".join(
        f"## {st.name} (exit {st.exit_code})\n$ {st.shell}\n{st.tail}" for st in result.steps
    )
)
print(json.dumps(report, indent=1))
