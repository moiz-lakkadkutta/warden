"""A deployable console site: <out>/index.html lists runs, <out>/<id>/ holds each run's page.

Each run page is the single self-contained file from render.py; next to it go exactly the files
its footer links to (events.jsonl, cost.jsonl, artifacts/pr_body.md, timeline.gif). Nothing else
from runs/<id>/ is copied: gate output and sandbox transcripts stay local. Static hosts (GitHub
Pages) serve the directory as is; `.nojekyll` stops Pages from dropping files.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from warden.runlog.render import RunView, _css, _e, _fmt_ms, load_run, render_view

PUBLISHED = ("events.jsonl", "cost.jsonl", "artifacts/pr_body.md")


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    title: str | None
    issue: str | None
    label: str
    usd: float
    wall_ms: int
    n_events: int
    has_gif: bool = False
    mode: str | None = None


def list_runs(runs_dir: Path) -> list[str]:
    """Run ids with an events.jsonl, oldest first (run ids sort by start time)."""
    if not runs_dir.exists():
        return []
    return sorted(p.name for p in runs_dir.iterdir() if (p / "events.jsonl").is_file())


def write_gif(runs_dir: Path, run_id: str) -> Path:
    """runs/<id>/timeline.gif. Raises gif.GifUnavailableError without Pillow."""
    from warden.runlog.gif import render_gif

    view = load_run(runs_dir, run_id)
    return render_gif(
        view.events,
        runs_dir / run_id / "timeline.gif",
        run_id=run_id,
        title=view.title,
        verdict=view.label,
    )


def build_site(runs_dir: Path, run_ids: list[str], out: Path, *, gif: bool = True) -> list[str]:
    """Render `run_ids` into `out`; returns warnings (e.g. GIF skipped) for stderr."""
    from warden.runlog.gif import GifUnavailableError

    warnings: list[str] = []
    out.mkdir(parents=True, exist_ok=True)
    (out / ".nojekyll").write_text("", encoding="utf-8")
    summaries: list[RunSummary] = []
    for run_id in run_ids:
        src, dst = runs_dir / run_id, out / run_id
        dst.mkdir(parents=True, exist_ok=True)
        has_gif = False
        if gif:
            try:
                shutil.copyfile(write_gif(runs_dir, run_id), dst / "timeline.gif")
                has_gif = True
            except GifUnavailableError as exc:
                if not warnings:
                    warnings.append(f"{exc}; skipped timeline.gif → {exc.fix}")
        for rel in PUBLISHED:
            if (src / rel).is_file():
                (dst / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src / rel, dst / rel)
        view = load_run(runs_dir, run_id)
        page = render_view(view, has_gif=has_gif, index_href="../index.html")
        (dst / "index.html").write_text(page, encoding="utf-8")
        summaries.append(summarize(view, has_gif=has_gif))
    (out / "index.html").write_text(render_index(summaries), encoding="utf-8")
    return warnings


def summarize(view: RunView, *, has_gif: bool = False) -> RunSummary:

    evs = view.events
    if view.result and view.result.get("cost_usd") is not None:
        usd = float(view.result["cost_usd"])
    else:
        usd = sum(ev.usd or 0.0 for ev in evs)
    wall = int((evs[-1].ts - evs[0].ts).total_seconds() * 1000) if evs else 0
    issue = view.result.get("issue") if view.result else None
    return RunSummary(
        run_id=view.run_id,
        title=view.title,
        issue=str(issue) if issue is not None else None,
        label=view.label,
        usd=usd,
        wall_ms=wall,
        n_events=len(evs),
        has_gif=has_gif,
        mode=view.mode,
    )


def _mode_tag(mode: str | None) -> str:
    if mode == "demo":
        return ' <span class="chip">demo · fake sandbox</span>'
    if mode == "live":
        return ' <span class="chip">live · Nebius</span>'
    return ""


def render_index(runs: list[RunSummary]) -> str:
    """The landing page: newest run first; the empty state names the command that makes one."""
    if runs:
        rows = "".join(
            "<tr>"
            f'<td><a class="mono" href="{_e(r.run_id)}/index.html">{_e(r.run_id)}</a><br>'
            f'<span class="pill" data-verdict="{_e(r.label)}">{_e(r.label)}</span></td>'
            f'<td class="issue">{_e(("#" + r.issue + " ") if r.issue else "")}'
            f"{_e(r.title or '—')}{_mode_tag(r.mode)}</td>"
            f'<td class="num">${r.usd:.2f}<br>{_e(_fmt_ms(r.wall_ms))}<br>'
            f"{r.n_events} events</td>"
            "</tr>"
            for r in sorted(runs, key=lambda r: r.run_id, reverse=True)
        )
        body = (
            '<div class="scroll"><table class="runs"><thead><tr><th>Run · verdict</th>'
            "<th>Issue</th><th>Cost · time</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
        )
    else:
        body = '<p class="empty">no runs yet — <code>warden run --demo</code></p>'
    return "\n".join(
        [
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width,initial-scale=1'>",
            "<meta name='color-scheme' content='light dark'><link rel='icon' href='data:,'>",
            "<title>warden console</title>",
            _css(),
            "</head><body>",
            '<header id="header"><h1>warden · console</h1>'
            '<p class="subtitle">hunt → plan → fix → verify → PR, replayed from each run’s '
            "<code>events.jsonl</code></p></header>",
            f"<h2>Runs</h2>{body}",
            "</body></html>",
        ]
    )
