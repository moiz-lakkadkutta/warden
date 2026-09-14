"""Render runs/<id>/events.jsonl to a single static HTML page (the console's data source).

Kept deliberately small: a phase timeline, the notes per phase, sandbox ops with from→to uuids
(the branch tree data), and the evidence table artefact if present. The FastAPI viewer in
console/ serves these files read-only.
"""

from __future__ import annotations

import html
from collections import defaultdict
from pathlib import Path

from warden.runlog.events import EventLog

PHASES = ("hunt", "plan", "fix", "verify", "pr")
PHASE_COLOURS = {
    "hunt": "#7A4E9E",
    "plan": "#2F6F8F",
    "fix": "#B9770E",
    "verify": "#2E7D4F",
    "pr": "#4A5563",
}


def render_run(runs_dir: Path, run_id: str) -> Path:
    events = EventLog.read(runs_dir, run_id)
    by_phase: dict[str, list] = defaultdict(list)  # type: ignore[type-arg]
    usd_by_phase: dict[str, float] = defaultdict(float)
    for ev in events:
        by_phase[ev.phase].append(ev)
        if ev.usd:
            usd_by_phase[ev.phase] += ev.usd
    total_usd = sum(usd_by_phase.values())
    pr_body = runs_dir / run_id / "artifacts" / "pr_body.md"

    parts = [
        "<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>warden run {html.escape(run_id)}</title>",
        "<style>body{font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px;max-width:900px;margin-inline:auto;color:#14202E;background:#F3F5F7}"
        "h1{font-size:1.4rem}.tl{display:flex;gap:4px;margin:16px 0}.tl div{flex:1;padding:8px;color:#fff;border-radius:3px;font-size:13px}"
        ".ph{margin:20px 0;padding:0 0 0 12px;border-left:3px solid #D7DDE3}.ev{font:13px ui-monospace,monospace;margin:4px 0;color:#4A5563}"
        "pre{background:#fff;border:1px solid #D7DDE3;padding:12px;overflow-x:auto;font-size:13px}</style>",
        f"<h1>warden · run {html.escape(run_id)}</h1>",
        f"<p>{len(events)} events · ${total_usd:.2f}</p>",
        "<div class='tl'>",
    ]
    for p in PHASES:
        n = len(by_phase.get(p, []))
        parts.append(
            f"<div style='background:{PHASE_COLOURS[p]};opacity:{1 if n else 0.35}'>{p}<br>{n} ev · ${usd_by_phase.get(p, 0):.2f}</div>"
        )
    parts.append("</div>")
    for p in PHASES:
        evs = by_phase.get(p, [])
        if not evs:
            continue
        parts.append(f"<section class='ph' style='border-color:{PHASE_COLOURS[p]}'><h2>{p}</h2>")
        for ev in evs:
            if ev.kind == "sandbox":
                d = ev.data
                parts.append(
                    f"<div class='ev'>⎇ {html.escape(str(d.get('from')))} → {html.escape(str(d.get('to')))} · <code>{html.escape(str(d.get('shell')))}</code> · exit {d.get('exit_code')} · {ev.ms} ms</div>"
                )
            elif ev.kind == "llm":
                d = ev.data
                parts.append(
                    f"<div class='ev'>◎ {html.escape(str(d.get('role')))} {html.escape(str(d.get('model')))} · {d.get('prompt_tokens')}+{d.get('completion_tokens')} tok · ${ev.usd or 0:.4f}</div>"
                )
            else:
                parts.append(
                    f"<div class='ev'>· {html.escape(str(ev.data.get('text', ev.kind)))}</div>"
                )
        parts.append("</section>")
    if pr_body.exists():
        parts.append(
            "<h2>Evidence</h2><pre>" + html.escape(pr_body.read_text(encoding="utf-8")) + "</pre>"
        )
    out = runs_dir / run_id / "index.html"
    out.write_text("\n".join(parts), encoding="utf-8")
    return out
