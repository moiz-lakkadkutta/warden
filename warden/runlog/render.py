"""Render runs/<id>/events.jsonl (+ result.json, artifacts/pr_body.md) to one static HTML page.

The page is the console (docs/ux-brief.md §2.4): header pill, duration-scaled phase timeline,
branch tree (base → candidate / base → theater), the evidence table verbatim from the PR body,
a per-phase log and a footer with the resume command. Everything here is pure except
`render_run`, which reads the run directory and writes index.html. No external assets.
"""

from __future__ import annotations

import html
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from warden.runlog.events import EventLog, phase_spans
from warden.schemas import Phase, RunEvent

PHASES: tuple[Phase, ...] = ("hunt", "plan", "fix", "verify", "pr")
PHASE_COLOURS: dict[str, str] = {
    "hunt": "#7A4E9E",
    "plan": "#2F6F8F",
    "fix": "#B9770E",
    "verify": "#2E7D4F",
    "pr": "#4A5563",
}
# Semantic palette: reserved for verdicts and tree states, never for phases.
VERDICT_COLOURS: dict[str, str] = {
    "PASS": "#1E7B4A",
    "PASS WITH CONCERNS": "#B26B00",
    "FAIL": "#B3261E",
    "PENDING-HUMAN": "#B26B00",
    "COST CAP": "#B3261E",
    "RUNNING": "#4A5563",
}

THEATER_AS_REQUIRED = "failed on unfixed base — as required"
THEATER_IS_THEATER = "passed on unfixed base — tests are theater"
NOT_RUN = "not run"


# --- branch tree -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Edge:
    parent: str
    child: str
    phase: Phase
    shell: str
    exit_code: int
    ms: int
    candidate: int | None = None  # best-of-N tag on the event


@dataclass(frozen=True)
class NodeRun:
    node: str
    phase: Phase
    shell: str
    exit_code: int
    ms: int


@dataclass(frozen=True)
class BranchTree:
    base: str | None
    candidate: str | None
    theater: str | None
    edges: tuple[Edge, ...]
    runs: tuple[NodeRun, ...]

    def children(self, node: str) -> list[str]:
        return [e.child for e in self.edges if e.parent == node]

    def test_run(self, node: str) -> NodeRun | None:
        """Last run on `node` whose command is a pytest invocation."""
        hit = None
        for r in self.runs:
            if r.node == node and r.shell.startswith("pytest"):
                hit = r
        return hit

    def lineage(self, node: str) -> list[str]:
        """Checkpoints from the root down to `node` (root excluded)."""
        parents = {e.child: e.parent for e in self.edges}
        path: list[str] = []
        while node in parents and node not in path:
            path.append(node)
            node = parents[node]
        path.reverse()
        return path


def build_tree(events: list[RunEvent]) -> BranchTree:
    edges: list[Edge] = []
    runs: list[NodeRun] = []
    for ev in events:
        if ev.kind != "sandbox":
            continue
        d = ev.data
        frm = str(d.get("from") or "")
        to = str(d.get("to") or "")
        shell = str(d.get("shell") or "")
        code = _int(d.get("exit_code"))
        ms = ev.ms or 0
        tag = d.get("candidate")
        if to and to != frm:
            cand = tag if isinstance(tag, int) else None
            edges.append(Edge(frm, to, ev.phase, shell, code, ms, cand))
        else:
            runs.append(NodeRun(frm, ev.phase, shell, code, ms))

    children = {e.child for e in edges}
    base = next((e.parent for e in edges if e.parent not in children), None)
    candidate = next((e.child for e in reversed(edges) if e.phase == "fix"), None)
    theater = next(
        (e.child for e in edges if e.phase == "verify" and base is not None and e.parent == base),
        None,
    )
    return BranchTree(base, candidate, theater, tuple(edges), tuple(runs))


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# --- derived numbers -------------------------------------------------------------------------


def phase_durations_ms(events: list[RunEvent]) -> dict[Phase, int]:
    spans = phase_spans(events)
    out: dict[Phase, int] = {}
    for p in PHASES:
        if p in spans:
            start, end = spans[p]
            out[p] = max(0, int((end - start).total_seconds() * 1000))
        else:
            out[p] = 0
    return out


_VERDICT_RE = re.compile(r"\*\*Verdict:\s*(.+?)\*\*")
_LINK_RE = re.compile(r"\s*\[↗\]\([^)]*\)")


def parse_evidence(md: str) -> tuple[str | None, list[tuple[str, str]]]:
    """(verdict, rows) from a PR body: the `**Verdict: X**` line and the first Check/Result table."""
    m = _VERDICT_RE.search(md)
    verdict = m.group(1).strip() if m else None
    rows: list[tuple[str, str]] = []
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if _cells(line)[:2] != ["Check", "Result"]:
            continue
        for row in lines[i + 1 :]:
            cells = _cells(row)
            if len(cells) < 2:
                break
            if cells[0] and set(cells[0]) <= {"-", ":"}:
                continue  # separator
            rows.append((cells[0], cells[1]))
        break
    return verdict, rows


def _cells(line: str) -> list[str]:
    s = line.strip()
    if not s.startswith("|"):
        return []
    return [c.strip() for c in s.strip("|").split("|")]


def _plain(cell: str) -> str:
    """Markdown table cell → plain text (drop the evidence link and bold markers)."""
    return _LINK_RE.sub("", cell).replace("**", "").strip()


def verdict_label(
    events: list[RunEvent],
    result: dict[str, Any] | None,
    evidence_rows: list[tuple[str, str]],
) -> str:
    if any(ev.kind == "cost_cap" for ev in events):
        return "COST CAP"
    if any(ev.phase == "pr" and ev.data.get("label") == "PENDING-HUMAN" for ev in events):
        return "PENDING-HUMAN"
    if any(c == "Risk class" and "PENDING-HUMAN" in r for c, r in evidence_rows):
        return "PENDING-HUMAN"
    if result and result.get("verdict"):
        return str(result["verdict"]).replace("_", " ")
    return "RUNNING"


def _models(events: list[RunEvent], evidence_rows: list[tuple[str, str]]) -> list[str]:
    """Distinct model ids of llm events; failing that, the names in the evidence Cost row."""
    seen: list[str] = []
    for ev in events:
        if ev.kind == "llm" and ev.data.get("model"):
            m = str(ev.data["model"])
            if m not in seen:
                seen.append(m)
    if seen:
        return seen
    cost = next((r for c, r in evidence_rows if c == "Cost"), "")
    for chunk in _plain(cost).split("·"):
        part = chunk.strip()
        if not part or part.startswith("$"):
            continue
        name = part.rsplit(" ", 1)[0] if " " in part else part
        if name not in seen:
            seen.append(name)
    return seen


def _fmt_ms(ms: int) -> str:
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    m, s = divmod(ms // 1000, 60)
    return f"{m}m {s:02d}s"


# --- HTML -----------------------------------------------------------------------------------


def _e(v: Any) -> str:
    return html.escape(str(v), quote=True)


def _css() -> str:
    phase_rules = "".join(f".p-{p}{{background:{c}}}" for p, c in PHASE_COLOURS.items())
    verdict_rules = "".join(
        f'.pill[data-verdict="{v}"]{{background:{c}}}' for v, c in VERDICT_COLOURS.items()
    )
    return (
        "<style>"
        ":root{--bg:#F3F5F7;--card:#fff;--ink:#14202E;--muted:#4A5563;--line:#D7DDE3;"
        "--ok:#1E7B4A;--warn:#B26B00;--bad:#B3261E;--none:#8A94A0;"
        "--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"
        '--sans:system-ui,"Segoe UI",Helvetica,Arial,sans-serif}'
        "@media (prefers-color-scheme:dark){:root{--bg:#14181D;--card:#1E242B;--ink:#E8ECF0;"
        "--muted:#A7B0BA;--line:#333B45;--ok:#4CC38A;--warn:#E0A03A;--bad:#F0655B;--none:#7B8592}}"
        "*{box-sizing:border-box}"
        "body{font:15px/1.5 var(--sans);margin:0;padding:24px 16px 48px;max-width:960px;"
        "margin-inline:auto;color:var(--ink);background:var(--bg)}"
        "code,.mono,.num{font-family:var(--mono);font-size:13px}"
        "h1{font-size:1.25rem;margin:0}"
        "h2{font-size:.8rem;margin:28px 0 8px;color:var(--muted);text-transform:uppercase;"
        "letter-spacing:.06em}"
        "a{color:inherit}"
        "#header{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}"
        "#header .meta{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;"
        "color:var(--muted)}"
        ".pill{display:inline-block;padding:2px 10px;border-radius:999px;color:#fff;"
        "background:var(--none);font:600 12px/1.6 var(--sans);letter-spacing:.04em}"
        ".chip{display:inline-block;padding:1px 8px;border:1px solid var(--line);"
        "border-radius:4px;font-family:var(--mono);font-size:12px;background:var(--card)}"
        "#timeline{display:flex;gap:3px;margin:16px 0 0}"
        "#timeline a{display:block;min-width:0;padding:6px 8px;color:#fff;text-decoration:none;"
        "border-radius:3px;font-size:12px;overflow:hidden;white-space:nowrap;"
        "text-overflow:ellipsis}"
        "#timeline a .num{color:#fff;opacity:.85}"
        "#timeline a.idle{opacity:.35}"
        "#tree{display:flex;gap:24px;align-items:stretch;margin-top:8px}"
        ".node{flex:0 0 auto;min-width:160px;padding:12px 14px;border:1px solid var(--line);"
        "border-radius:6px;background:var(--card);align-self:center}"
        ".label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}"
        ".id{font-family:var(--mono);font-size:13px}"
        ".branches{flex:1 1 auto;display:flex;flex-direction:column;gap:12px;min-width:0;"
        "position:relative;padding-left:24px}"
        ".branches::before{content:'';position:absolute;left:0;top:12%;bottom:12%;"
        "border-left:2px solid var(--line)}"
        ".edge{position:relative;padding:12px 14px;border:1px solid var(--line);"
        "border-left-width:4px;border-radius:6px;background:var(--card)}"
        ".edge::before{content:'';position:absolute;left:-24px;top:50%;width:20px;"
        "border-top:2px solid var(--line)}"
        ".edge .status{font-weight:600;margin-top:4px}"
        ".edge.ok{border-left-color:var(--ok)}.edge.ok .status{color:var(--ok)}"
        ".edge.fail{border-left-color:var(--bad)}.edge.fail .status{color:var(--bad)}"
        ".edge.theater{border-left-color:var(--warn)}.edge.theater .status{color:var(--warn)}"
        ".edge.none{border-left-color:var(--none)}.edge.none .status{color:var(--none)}"
        "table{border-collapse:collapse;width:100%;background:var(--card);"
        "border:1px solid var(--line)}"
        "th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);"
        "vertical-align:top}th{font-size:12px;color:var(--muted)}"
        "td+td{font-family:var(--mono);font-size:13px}"
        ".scroll{overflow-x:auto}"
        "details{margin:8px 0;border-left:3px solid var(--line);padding-left:12px}"
        "summary{cursor:pointer;font-weight:600}summary .num{font-weight:400;color:var(--muted)}"
        ".ev{font-family:var(--mono);font-size:13px;margin:4px 0;color:var(--muted);"
        "word-break:break-word}.ev.err,.ev.cap{color:var(--bad)}"
        "#footer{margin-top:32px;padding-top:12px;border-top:1px solid var(--line);"
        "color:var(--muted);font-size:13px;display:flex;flex-wrap:wrap;gap:8px 16px}"
        "@media (max-width:600px){#tree{flex-direction:column}}"
        "@media (max-width:600px){.branches{padding-left:0}"
        ".branches::before,.edge::before{display:none}"
        ".node{align-self:stretch}#timeline a{padding:6px 4px}}"
        f"{phase_rules}{verdict_rules}"
        "</style>"
    )


def _header(
    run_id: str,
    events: list[RunEvent],
    result: dict[str, Any] | None,
    rows: list[tuple[str, str]],
) -> str:
    label = verdict_label(events, result, rows)
    if result and result.get("cost_usd") is not None:
        total = float(result["cost_usd"])
    else:
        total = sum(ev.usd or 0.0 for ev in events)
    if events:
        wall = _fmt_ms(int((events[-1].ts - events[0].ts).total_seconds() * 1000))
    else:
        wall = "—"
    issue = (
        f"<span class='num'>#{_e(result['issue'])}</span>" if result and result.get("issue") else ""
    )
    chips = "".join(
        f'<span class="chip" title="{_e(m)}">{_e(m.split("/")[-1])}</span>'
        if "/" in m
        else f'<span class="chip">{_e(m)}</span>'
        for m in _models(events, rows)
    )
    return (
        '<header id="header">'
        f"<h1>warden · run <span class='mono'>{_e(run_id)}</span></h1>"
        f'<span class="pill" data-verdict="{_e(label)}">{_e(label)}</span>'
        f'<span class="meta">{issue}<span class="num">${total:.2f}</span>'
        f'<span class="num">{_e(wall)}</span>{chips}</span>'
        "</header>"
    )


def _timeline(events: list[RunEvent]) -> str:
    durs = phase_durations_ms(events)
    usd: dict[str, float] = defaultdict(float)
    active: set[str] = set()
    for ev in events:
        usd[ev.phase] += ev.usd or 0.0
        active.add(ev.phase)
    bars = []
    for p in PHASES:
        idle = "" if p in active else " idle"
        bars.append(
            f'<a href="#phase-{p}" class="p-{p}{idle}" style="flex-grow:{durs[p] + 1}" '
            f'title="{p} · {_fmt_ms(durs[p])} · ${usd[p]:.2f}">'
            f"{p} <span class='num'>${usd[p]:.2f}</span></a>"
        )
    return f'<nav id="timeline">{"".join(bars)}</nav>'


def _edge_card(
    label: str, node: str | None, run: NodeRun | None, cls: str, status: str, note: str = ""
) -> str:
    cmd = (
        f"<div class='mono'>{_e(run.shell)} → exit {run.exit_code} · {_fmt_ms(run.ms)}</div>"
        if run
        else ""
    )
    node_id = _e(node) if node else "—"
    return (
        f'<div class="{cls}"><div class="label">{_e(label)}</div>'
        f'<div class="id">{node_id}{note}</div>{cmd}'
        f'<div class="status">{_e(status)}</div></div>'
    )


def _exit_text(run: NodeRun | None) -> str:
    if run is None:
        return NOT_RUN
    return "passed (exit 0)" if run.exit_code == 0 else f"failed (exit {run.exit_code})"


def best_of_n_choice(events: list[RunEvent]) -> tuple[int | None, dict[int, str]]:
    """(chosen index, {index: decision or 'skipped'}) from the best-of-N note; (None, {}) when
    the run had one candidate."""
    for ev in reversed(events):
        if ev.kind == "note" and "best_of_n" in ev.data:
            chosen = ev.data.get("chosen")
            each = {
                int(c["index"]): str(c.get("decision") or c.get("status") or "")
                for c in ev.data.get("candidates", [])
            }
            return (chosen if isinstance(chosen, int) else None), each
    return None, {}


def _candidate_cards(tree: BranchTree, events: list[RunEvent]) -> list[str]:
    """Best-of-N: one card per candidate (the leaf of its fix lineage), the chosen one marked."""
    chosen, each = best_of_n_choice(events)
    leaves: dict[int, str] = {}
    for e in tree.edges:
        if e.phase == "fix" and e.candidate is not None:
            leaves[e.candidate] = e.child
    cards = []
    for k in sorted(set(leaves) | set(each)):
        node = leaves.get(k)
        run = tree.test_run(node) if node else None
        decision = each.get(k, "")
        cls = "edge ok" if decision.startswith("PASS") else "edge fail" if decision else "edge none"
        status = (decision.replace("_", " ") or "not verified") + (
            " · chosen" if k == chosen else ""
        )
        cards.append(_edge_card(f"candidate #{k}", node, run, cls, status))
    return cards


def _tree(events: list[RunEvent], rows: list[tuple[str, str]]) -> str:
    tree = build_tree(events)
    row = {c: _plain(r) for c, r in rows}
    cards: list[str] = []
    if any(e.candidate is not None for e in tree.edges):
        cards = _candidate_cards(tree, events)
        base_id = _e(tree.base) if tree.base else "—"
        base_card = (
            f'<div class="node"><div class="label">base</div><div class="id">{base_id}</div></div>'
        )
        return (
            f'<section id="tree">{base_card}<div class="branches">{"".join(cards)}</div></section>'
        )

    # candidate: leaf of the fix lineage (never chosen by exit code)
    run = tree.test_run(tree.candidate) if tree.candidate else None
    hops = len(tree.lineage(tree.candidate)) if tree.candidate else 0
    note = f" <span class='num'>· {hops} checkpoints</span>" if hops > 1 else ""
    if run is None:
        cards.append(_edge_card("candidate", tree.candidate, None, "edge none", NOT_RUN, note))
    else:
        cls = "edge ok" if run.exit_code == 0 else "edge fail"
        text = row.get("Full suite on candidate") or _exit_text(run)
        cards.append(_edge_card("candidate", tree.candidate, run, cls, text, note))

    # theater probe: base + the new tests only; red here is the good outcome
    probe = tree.test_run(tree.theater) if tree.theater else None
    if probe is None:
        cards.append(_edge_card("theater probe", tree.theater, None, "edge none", NOT_RUN))
    elif probe.exit_code != 0:
        cards.append(
            _edge_card(
                "theater probe", tree.theater, probe, "edge fail required", THEATER_AS_REQUIRED
            )
        )
    else:
        cards.append(
            _edge_card("theater probe", tree.theater, probe, "edge theater", THEATER_IS_THEATER)
        )

    # anything else forked from base in verify (best-of-N, race probes): plain cards
    if tree.base is not None:
        for e in tree.edges:
            if e.phase == "verify" and e.parent == tree.base and e.child != tree.theater:
                r = tree.test_run(e.child)
                cards.append(_edge_card("branch", e.child, r, "edge", _exit_text(r)))

    base_id = _e(tree.base) if tree.base else "—"
    base_card = (
        f'<div class="node"><div class="label">base</div><div class="id">{base_id}</div></div>'
    )
    return f'<section id="tree">{base_card}<div class="branches">{"".join(cards)}</div></section>'


def _evidence(verdict: str | None, rows: list[tuple[str, str]]) -> str:
    if not rows:
        return (
            '<section id="evidence"><p>no evidence table yet — '
            "artifacts/pr_body.md not written</p></section>"
        )
    head = f"<p>Verdict: <strong>{_e(verdict)}</strong></p>" if verdict else ""
    body = "".join(f"<tr><td>{_e(c)}</td><td>{_e(_plain(r))}</td></tr>" for c, r in rows)
    return (
        f'<section id="evidence">{head}<div class="scroll"><table>'
        "<thead><tr><th>Check</th><th>Result</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div></section>"
    )


def _log_row(ev: RunEvent) -> str:
    row = _log_row_body(ev)
    k = ev.data.get("candidate")
    if isinstance(k, int):  # best-of-N: say which candidate's branch this line is on
        return row.replace("<div class='ev", f"<div data-candidate='{k}' class='ev", 1).replace(
            "'>", f"'><span class='num'>#{k}</span> ", 1
        )
    return row


def _log_row_body(ev: RunEvent) -> str:
    d = ev.data
    if ev.kind == "sandbox":
        return (
            f"<div class='ev'>⎇ {_e(d.get('from', ''))} → {_e(d.get('to', ''))} · "
            f"<code>{_e(d.get('shell', ''))}</code> → exit {_int(d.get('exit_code'))} · "
            f"{_fmt_ms(ev.ms or 0)}</div>"
        )
    if ev.kind == "llm":
        return (
            f"<div class='ev'>◎ {_e(d.get('role', ''))} {_e(d.get('model', ''))} · "
            f"{_int(d.get('prompt_tokens'))}+{_int(d.get('completion_tokens'))} tok · "
            f"${ev.usd or 0:.4f}</div>"
        )
    if ev.kind == "cost_cap":
        return (
            f"<div class='ev cap'>■ cost cap ${float(d.get('cap_usd', 0)):.2f} reached "
            f"(spent ${float(d.get('spent_usd', 0)):.2f})</div>"
        )
    if ev.kind == "error":
        return f"<div class='ev err'>✕ {_e(d.get('text', d))}</div>"
    return f"<div class='ev'>· {_e(d.get('text', ev.kind))}</div>"


def _log(events: list[RunEvent]) -> str:
    by_phase: dict[str, list[RunEvent]] = defaultdict(list)
    for ev in events:
        by_phase[ev.phase].append(ev)
    durs = phase_durations_ms(events)
    parts = ['<section id="log">']
    for p in PHASES:
        evs = by_phase.get(p, [])
        usd = sum(ev.usd or 0.0 for ev in evs)
        state = " open" if evs else ""
        parts.append(
            f'<details id="phase-{p}"{state} style="border-color:{PHASE_COLOURS[p]}">'
            f"<summary>{p} <span class='num'>{len(evs)} events · {_fmt_ms(durs[p])} · "
            f"${usd:.2f}</span></summary>"
        )
        parts.extend(_log_row(ev) for ev in evs)
        if not evs:
            parts.append("<div class='ev'>· no events</div>")
        parts.append("</details>")
    parts.append("</section>")
    return "".join(parts)


def _footer(run_id: str) -> str:
    return (
        '<footer id="footer">'
        f"<span class='mono'>{_e(run_id)}</span>"
        f"<code>warden resume {_e(run_id)}</code>"
        '<a href="events.jsonl">events.jsonl</a>'
        '<a href="cost.jsonl">cost.jsonl</a>'
        '<a href="artifacts/pr_body.md">pr_body.md</a>'
        "</footer>"
    )


def render_html(
    run_id: str,
    events: list[RunEvent],
    result: dict[str, Any] | None,
    evidence_md: str | None,
) -> str:
    verdict, rows = parse_evidence(evidence_md or "")
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>warden run {_e(run_id)}</title>",
        _css(),
        "</head><body>",
        _header(run_id, events, result, rows),
        _timeline(events),
        "<h2>Branch tree</h2>",
        _tree(events, rows),
        "<h2>Evidence</h2>",
        _evidence(verdict, rows),
        "<h2>Log</h2>",
        _log(events),
        _footer(run_id),
        "</body></html>",
    ]
    return "\n".join(parts)


def render_run(runs_dir: Path, run_id: str) -> Path:
    events = EventLog.read(runs_dir, run_id)
    result = EventLog.read_result(runs_dir, run_id)
    pr_body = runs_dir / run_id / "artifacts" / "pr_body.md"
    evidence_md = pr_body.read_text(encoding="utf-8") if pr_body.exists() else None
    out = runs_dir / run_id / "index.html"
    out.write_text(render_html(run_id, events, result, evidence_md), encoding="utf-8")
    return out
