"""E2: a campaign on a real codebase — plan every issue, wave them, fix + verify + PR each wave in
parallel, merge the wave (D2), gate the integrated image, and write campaign.csv + campaign.md.

One invocation = one base: the target's live head (`at=None`, for open issues) or one replay
commit (`at=<rev>`, for closed issues re-solved from their text alone). Every issue is an ordinary
pipeline run (runs/<campaign>-<issue>/), so `warden resume` semantics and artifacts are unchanged;
the campaign only decides which image each run starts from:

    prepared base ──wave 1 fixes──▶ merge ▶ integration₁ ──wave 2 fixes──▶ merge ▶ integration₂

Rows for the same (issue, at) replace older ones, so the CSV accumulates several invocations and
campaign.md is rendered from the whole CSV. Per-issue failures (cost cap included) end that issue
as an ERROR row; the account running out of money (ProviderBudgetError) ends the campaign.
"""

from __future__ import annotations

import csv
import json
import shlex
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from warden.integrate.merger import WaveResult, head_sha, merge_wave
from warden.runlog.events import EventLog
from warden.schemas import HUMAN_GATE, Candidate, Phase, Plan, Verdict

if TYPE_CHECKING:
    from warden.config import Settings
    from warden.pipeline import Steps
    from warden.sandbox.box import Box

CSV_NAME = "campaign.csv"
MD_NAME = "campaign.md"
WAVES_NAME = "waves.json"
HUNT_LABEL = "warden-hunt"
BUNDLE_PATH = "/tmp/warden-campaign.bundle"
# After checking out a newer commit than the image was built at: migrate both databases.
MIGRATE: dict[str, str] = {
    "fabric": (
        "cd backend && alembic upgrade head && "
        'DATABASE_URL="$DEMO_DATABASE_URL" MIGRATION_DATABASE_URL="$DEMO_DATABASE_URL" '
        "alembic upgrade head"
    ),
}


@dataclass
class Row:
    issue: int
    source: str  # backlog | hunter
    verdict: str  # PASS | PASS_WITH_CONCERNS | FAIL | ERROR | SKIPPED
    pending_human: str  # "n" or "y: money, tax"
    wave: int | None
    merged: str  # y | n | — (not a merge candidate)
    pr_url: str
    usd: float
    seconds: int
    at: str = ""  # replay commit; "" = live head
    repro: str = ""  # y = a new test failed on the unfixed base (theater confirmed the bug)
    run_id: str = ""
    campaign: str = ""
    title: str = ""
    note: str = ""


COLUMNS = [f.name for f in fields(Row)]


# ------------------------------------------------------------------------------ progress


class LineProgress:
    """`Progress` for parallel runs: one stderr line per call, prefixed with the issue."""

    _lock = threading.Lock()

    def __init__(self, prefix: str, out: Any = None) -> None:
        self.prefix = prefix
        self._out = out
        self.spent = 0.0

    def _w(self, text: str) -> None:
        with self._lock:
            print(f"{self.prefix} {text}", file=self._out or sys.stderr, flush=True)

    def phase(self, phase: Phase, text: str) -> None:
        self._w(f"{phase}: {text}")

    def step(self, text: str) -> None:
        self._w(f"  {text}")

    def cost(self, usd: float) -> None:
        self.spent = usd

    def finish(self, text: str) -> None:
        self._w(f"done: {text}")


# --------------------------------------------------------------------------- the base


def prepare_base(
    base: Box,
    *,
    rev: str | None,
    cwd: str,
    wrap: str,
    migrate: str,
    bundle: Callable[[str, str | None], tuple[str, bytes]],
) -> tuple[Box, str]:
    """A kept checkpoint of `base` at `rev` (None = the target's live head). A commit the image
    lacks arrives as a git bundle from the host (`bundle(image_head, rev)`); when the new commit
    descends from the image's, `migrate` brings its databases up to the new head."""
    image_head = head_sha(base, cwd=cwd)
    known = ""
    if rev is not None:
        probe = base.run(
            f"git rev-parse --verify -q {shlex.quote(rev + '^{commit}')}", cwd=cwd, keep=False
        )
        known = probe.stdout.strip() if probe.ok else ""
    box = base
    if known:
        sha = known
    else:
        sha, data = bundle(image_head, rev)
        fetched = base.run(
            f"git fetch -q {BUNDLE_PATH} refs/warden/campaign:refs/warden/campaign",
            cwd=cwd,
            keep=True,
            files={BUNDLE_PATH: data},
        )
        if not fetched.ok or fetched.box is None:
            raise RuntimeError(f"cannot fetch the campaign bundle: {fetched.stderr.strip()[:300]}")
        box = fetched.box
    if sha != image_head:
        co = box.run(f"git checkout -q --detach {sha}", cwd=cwd, keep=True)
        if not co.ok or co.box is None:
            raise RuntimeError(f"cannot check out {sha}: {co.stderr.strip()[:300]}")
        box = co.box
        newer = box.run(f"git merge-base --is-ancestor {image_head} {sha}", cwd=cwd, keep=False)
        if migrate and newer.ok:
            cmd = f"{wrap} {shlex.quote(migrate)}" if wrap else migrate
            mig = box.run(cmd, cwd=cwd, keep=True, timeout=900)
            if not mig.ok or mig.box is None:
                raise RuntimeError(f"migrations failed at {sha[:7]}: {mig.stderr.strip()[-300:]}")
            box = mig.box
    return box, sha


# ------------------------------------------------------------------------------ runs


@dataclass
class IssueRun:
    issue: int
    run_id: str
    source: str
    t0: float = field(default_factory=time.perf_counter)
    seconds: float = 0.0
    plan: Plan | None = None
    error: str = ""
    title: str = ""


def _artifact(run_dir: Path, name: str) -> Path:
    return run_dir / "artifacts" / name


def _spent(run_dir: Path) -> float:
    from warden.pipeline import _spent as spent

    return spent(run_dir)


def _run_stage(
    settings: Settings,
    steps: Steps,
    ir: IssueRun,
    *,
    stop_after: Phase | None,
    at: str | None,
    yes: bool,
    pr_base: str | None,
    progress_out: Any,
) -> None:
    """One issue's pipeline from its last marker to `stop_after`. Every error but the account
    running dry becomes `ir.error` (that issue ends, the campaign goes on)."""
    from warden.llm.client import ProviderBudgetError
    from warden.pipeline import completed_phases, run_pipeline

    log = EventLog(settings.runs_dir, ir.run_id)
    t0 = time.perf_counter()
    try:
        done = completed_phases(log.dir)
        if stop_after is not None and stop_after in done:
            return
        if "pr" in done:
            return
        run_pipeline(
            settings,
            log,
            steps,
            start_after=done[-1] if done else None,
            json_out=False,
            yes=yes,
            progress=LineProgress(f"#{ir.issue}", progress_out),
            stop_after=stop_after,
            requested_issue=ir.issue,
            at_rev=at,
            base_ready=True,
            pr_base=pr_base,
        )
    except ProviderBudgetError:
        raise
    except Exception as err:  # noqa: BLE001 — one issue's failure is a row, not the campaign's end
        ir.error = f"{type(err).__name__}: {str(err)[:300]}"
        log.emit("pr", "error", {"text": ir.error, "campaign": True})
    finally:
        ir.seconds += time.perf_counter() - t0
        log.close()


def _parallel(fn: Callable[[IssueRun], None], runs: Sequence[IssueRun], workers: int) -> None:
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for fut in [pool.submit(fn, ir) for ir in runs]:
            fut.result()  # re-raises ProviderBudgetError


def pending_reason(plan: Plan | None) -> str:
    if plan is None:
        return "n"
    hits = [r for r in plan.risk_class if r in HUMAN_GATE]
    return f"y: {', '.join(hits)}" if hits else "n"


def _read(path: Path, model: Any) -> Any:
    return model.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None


def _pr_url(run_dir: Path) -> str:
    path = _artifact(run_dir, "pr.json")
    if not path.exists():
        return ""
    return str(json.loads(path.read_text(encoding="utf-8")).get("url") or "")


@dataclass(frozen=True)
class CampaignDeps:
    """What differs between a live campaign and a test: the steps, how a ref becomes a Box,
    how the base is prepared, where an issue comes from, how a fix leaves its sandbox, and how
    an integration branch is pushed (None = never: later waves PR against the default base)."""

    steps: Steps
    open_box: Callable[[str, EventLog], Box]
    prepare: Callable[[Box], tuple[Box, str]]
    source_of: Callable[[int], str]
    export: Callable[[Box, int, str], str]  # candidate box, issue, title → patch text
    push_integration: Callable[[str, list[str], str], str] | None = None  # start, heads, name


def run_campaign(
    issues: Sequence[int],
    settings: Settings,
    deps: CampaignDeps,
    *,
    campaign_id: str,
    at: str | None,
    out_dir: Path,
    yes: bool,
    budget_usd: float,
    progress_out: Any = None,
) -> list[Row]:
    """Plan all → waves → per wave: fix/verify/PR in parallel, merge, gate → rows (+ files)."""
    from warden.integrate.waves import plan_waves, render_waves
    from warden.verify.gate import PROFILES

    out_dir.mkdir(parents=True, exist_ok=True)
    clog = EventLog(settings.runs_dir, campaign_id)
    say = LineProgress("campaign", progress_out)
    try:
        base = deps.open_box(settings.base_image, clog)
        prepared, base_sha = deps.prepare(base)
        say.step(f"base {prepared.uuid[:8]} at {base_sha[:7]} ({at or 'live head'})")
        clog.emit("pr", "note", {"text": f"campaign base {base_sha}", "base_sha": base_sha})
        s0 = settings.model_copy(update={"base_image": prepared.uuid})
        runs = [IssueRun(i, f"{campaign_id}-{i}", deps.source_of(i)) for i in issues]

        def plan_one(ir: IssueRun) -> None:
            _run_stage(
                s0,
                deps.steps,
                ir,
                stop_after="plan",
                at=at,
                yes=yes,
                pr_base=None,
                progress_out=progress_out,
            )
            run_dir = settings.runs_dir / ir.run_id
            ir.plan = _read(_artifact(run_dir, "plan.json"), Plan)
            issue = _artifact(run_dir, "issue.json")
            if issue.exists():
                ir.title = str(json.loads(issue.read_text(encoding="utf-8")).get("title", ""))

        _parallel(plan_one, runs, settings.max_forks)
        planned = [ir for ir in runs if ir.plan is not None and not ir.error]
        waves = plan_waves([ir.plan for ir in planned if ir.plan], max_wave_size=settings.max_forks)
        say.step("waves:\n" + render_waves(waves))
        by_issue = {ir.issue: ir for ir in runs}
        wave_of: dict[int, int] = {}
        merged_of: dict[int, str] = {}
        notes: dict[int, str] = {}
        records: list[dict[str, Any]] = []
        integration = prepared
        integration_sha = base_sha
        pr_base: str | None = None
        for wave in waves:
            members = [by_issue[i] for i in wave.issues]
            for ir in members:
                wave_of[ir.issue] = wave.number
            spent = sum(_spent(settings.runs_dir / ir.run_id) for ir in runs)
            if spent + settings.max_usd_per_issue * len(members) > budget_usd:
                for ir in members:
                    ir.error = ir.error or f"skipped: budget ${budget_usd:.2f} (spent ${spent:.2f})"
                    notes[ir.issue] = ir.error
                say.step(f"wave {wave.number} skipped: budget")
                continue
            sw = settings.model_copy(update={"base_image": integration.uuid})
            listed = " ".join(f"#{i}" for i in wave.issues)
            say.step(f"wave {wave.number}: {listed} on {integration.uuid[:8]}")

            def fix_one(ir: IssueRun, sw: Settings = sw, pr_base: str | None = pr_base) -> None:
                _run_stage(
                    sw,
                    deps.steps,
                    ir,
                    stop_after=None,
                    at=at,
                    yes=yes,
                    pr_base=pr_base,
                    progress_out=progress_out,
                )

            _parallel(fix_one, members, settings.max_forks)
            patches: dict[int, str] = {}
            for ir in members:
                run_dir = settings.runs_dir / ir.run_id
                verdict = _read(_artifact(run_dir, "verdict.json"), Verdict)
                cand = _read(_artifact(run_dir, "candidate.json"), Candidate)
                if verdict is None or cand is None or verdict.decision == "FAIL":
                    continue
                patch_file = _artifact(run_dir, "candidate.patch")
                if patch_file.exists():
                    patches[ir.issue] = patch_file.read_text(encoding="utf-8")
                    continue
                try:
                    patches[ir.issue] = deps.export(
                        integration.sibling(cand.image_uuid), ir.issue, ir.title
                    )
                except Exception as err:  # noqa: BLE001 — no patch = not merged, said in the row
                    notes[ir.issue] = f"no patch: {str(err)[:200]}"
            commands = PROFILES[settings.gate_profile]
            if settings.test_cmd:
                commands = replace(commands, suite=settings.test_cmd)
            res: WaveResult = merge_wave(
                integration,
                wave,
                patches,
                clog,
                cwd=settings.workdir,
                gate=commands,
                wrap=settings.shell_wrap,
            )
            for o in res.outcomes:
                if o.issue in patches:
                    merged_of[o.issue] = "y" if o.merged else "n"
                    if not o.merged:
                        notes[o.issue] = o.reason
            gate_md = _gate_summary(res)
            if res.gate is not None:
                clog.artifact(
                    f"wave-{wave.number}-gate.txt",
                    "\n\n".join(
                        f"## {st.name} (exit {st.exit_code}, {st.ms / 1000:.1f} s)\n"
                        f"$ {st.shell}\n{st.tail}"
                        for st in res.gate.steps
                    ),
                )
            say.step(f"wave {wave.number}: merged {res.wave.merged or 'none'} · gate {gate_md}")
            integration, integration_sha = res.box, res.sha
            pushed = ""
            later = wave.number < len(waves)
            heads = [
                f"warden/fix-{i}-{by_issue[i].run_id}"
                for i in res.wave.merged
                if _pr_url(settings.runs_dir / by_issue[i].run_id)
            ]
            if later and heads and deps.push_integration is not None:
                name = f"warden/campaign-{campaign_id}-w{wave.number}"
                start = pr_base or base_sha
                try:
                    deps.push_integration(start, heads, name)
                    pr_base, pushed = name, name
                except Exception as err:  # noqa: BLE001 — later PRs fall back to the default base
                    say.step(f"integration branch not pushed: {str(err)[:200]}")
            records.append(
                {
                    "campaign": campaign_id,
                    "at": at or "",
                    "base_sha": base_sha,
                    "wave": wave.number,
                    "issues": wave.issues,
                    "merged": res.wave.merged,
                    "not_merged": {o.issue: o.reason for o in res.outcomes if not o.merged},
                    "image": res.box.uuid,
                    "sha": integration_sha,
                    "gate": gate_md,
                    "gate_ok": res.gate.ok if res.gate is not None else None,
                    "integration_branch": pushed,
                }
            )
        rows = []
        for ir in runs:
            run_dir = settings.runs_dir / ir.run_id
            verdict = _read(_artifact(run_dir, "verdict.json"), Verdict)
            if verdict is not None:
                decision = verdict.decision
            else:
                decision = "SKIPPED" if ir.error.startswith("skipped") else "ERROR"
            note = notes.get(ir.issue) or ir.error
            if verdict is not None and verdict.decision == "FAIL" and not note:
                note = "; ".join(verdict.reasons[:2])[:300]
            rows.append(
                Row(
                    issue=ir.issue,
                    source=ir.source,
                    verdict=decision,
                    pending_human=pending_reason(ir.plan),
                    wave=wave_of.get(ir.issue),
                    merged=merged_of.get(ir.issue, "—"),
                    pr_url=_pr_url(run_dir),
                    usd=round(_spent(run_dir), 4),
                    seconds=int(ir.seconds),
                    at=at or "",
                    repro="y" if verdict is not None and verdict.theater_failed_on_base else "n",
                    run_id=ir.run_id,
                    campaign=campaign_id,
                    title=ir.title,
                    note=note,
                )
            )
        write_rows(out_dir / CSV_NAME, rows)
        _append_waves(out_dir / WAVES_NAME, records)
        write_markdown(
            out_dir / MD_NAME,
            render_markdown(read_rows(out_dir / CSV_NAME), _load_waves(out_dir / WAVES_NAME)),
        )
        return rows
    finally:
        clog.close()


def _gate_summary(res: WaveResult) -> str:
    g = res.gate
    if g is None:
        return "not run (nothing merged)"
    suite = ", ".join(f"{n} {k}" for k, n in g.suite.items() if n) or "no summary"
    marks = [
        f"suite {suite}",
        f"mypy {'✓' if g.mypy_ok else '✗'}",
        f"ruff {'✓' if g.ruff_ok else '✗'}",
        f"alembic 1 head {'✓' if g.alembic_single_head else '✗'}",
        f"OpenAPI drift {'✗' if g.openapi_drift else 'none'}",
    ]
    return " · ".join(marks)


# ------------------------------------------------------------------------------ files


def read_rows(path: Path) -> list[Row]:
    if not path.exists():
        return []
    rows = []
    with path.open(newline="", encoding="utf-8") as fh:
        for rec in csv.DictReader(fh):
            wave = rec.get("wave") or ""
            text: dict[str, Any] = {k: rec.get(k) or "" for k in COLUMNS}
            text.update(
                issue=int(rec["issue"]),
                wave=int(wave) if wave else None,
                usd=float(rec.get("usd") or 0),
                seconds=int(float(rec.get("seconds") or 0)),
            )
            rows.append(Row(**text))
    return rows


def write_rows(path: Path, new: Sequence[Row]) -> list[Row]:
    """Merge `new` into the CSV: a row for the same (issue, at) replaces the old one."""
    keep = {(r.issue, r.at): r for r in read_rows(path)}
    for r in new:
        keep[(r.issue, r.at)] = r
    rows = sorted(keep.values(), key=lambda r: (r.at == "", r.at, r.issue))
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            rec = asdict(r)
            rec["wave"] = "" if r.wave is None else r.wave
            w.writerow(rec)
    return rows


def _load_waves(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data) if isinstance(data, list) else []


def _append_waves(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    campaigns = {r["campaign"] for r in records}
    old = [r for r in _load_waves(path) if r.get("campaign") not in campaigns]
    path.write_text(json.dumps(old + records, indent=2) + "\n", encoding="utf-8")


def tally(rows: Sequence[Row]) -> dict[str, Any]:
    """found / confirmed / PASS / PWC / FAIL / PENDING-HUMAN / USD for a set of rows."""
    return {
        "found": len(rows),
        "confirmed": sum(r.repro == "y" for r in rows),
        "PASS": sum(r.verdict == "PASS" for r in rows),
        "PASS_WITH_CONCERNS": sum(r.verdict == "PASS_WITH_CONCERNS" for r in rows),
        "FAIL": sum(r.verdict in ("FAIL", "ERROR") for r in rows),
        "PENDING-HUMAN": sum(r.pending_human.startswith("y") for r in rows),
        "merged": sum(r.merged == "y" for r in rows),
        "PRs": sum(bool(r.pr_url) for r in rows),
        "USD": round(sum(r.usd for r in rows), 2),
    }


BEGIN = "<!-- campaign:begin -->"
END = "<!-- campaign:end -->"
_MD_HEAD = """# Fabric campaign (E2)

Warden on a real codebase: Fabric ERP (FastAPI + SQLAlchemy 2 + Postgres RLS, double-entry
ledger, Indian GST). Each issue runs plan → fix → verify → draft PR; issues are grouped into
waves by the files their plans touch, each wave's passing fixes are merged with
`git merge --no-ff` into one integration image in the sandbox, and the full gate runs on it.

"""


def write_markdown(path: Path, tables: str) -> None:
    """Replace the generated block between the markers; everything else (notes) is kept."""
    block = f"{BEGIN}\n{tables}{END}\n"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if BEGIN in text and END in text:
            pre, _, rest = text.partition(BEGIN)
            _, _, post = rest.partition(END)
            path.write_text(pre + block + post.lstrip("\n"), encoding="utf-8")
            return
    path.write_text(_MD_HEAD + block + "\n## Notes\n\n(to be written)\n", encoding="utf-8")


def _pr_cell(url: str) -> str:
    if not url:
        return "—"
    return f"[#{url.rstrip('/').rsplit('/', 1)[-1]}]({url})"


def render_markdown(rows: Sequence[Row], waves: Sequence[dict[str, Any]]) -> str:
    """The results tables (generated from campaign.csv + waves.json; notes live in the doc)."""
    groups: dict[str, list[Row]] = {}
    for r in rows:
        label = (
            "hunter"
            if r.source == "hunter"
            else (f"backlog replay @ `{r.at}`" if r.at else "backlog (live head)")
        )
        groups.setdefault(label, []).append(r)
    out = [
        "<!-- generated by `warden campaign` from campaign.csv + waves.json; do not edit -->",
        "",
        "### Summary",
        "",
        *_table(
            "set found confirmed PASS PASS_WITH_CONCERNS FAIL PENDING-HUMAN merged PRs USD".split()
        ),
    ]
    for label, rs in [*groups.items(), ("**total**", list(rows))]:
        t = tally(rs)
        cells = [str(t[k]) for k in list(t)[:-1]]
        out.append(_tr([label, *cells, f"${t['USD']:.2f}"]))
    out += [
        "",
        "found = issues attempted · confirmed = a new test failed on the unfixed code (theater "
        "check) · FAIL includes ERROR · merged = merged cleanly into the wave's integration image",
        "",
        "### Issues",
        "",
        *_table(
            "issue source at verdict repro PENDING-HUMAN wave merged PR $ min run note".split()
        ),
    ]
    for r in rows:
        out.append(
            _tr(
                [
                    f"#{r.issue} {_cell(r.title, 70)}",
                    r.source,
                    _at(r.at),
                    r.verdict,
                    r.repro or "—",
                    r.pending_human,
                    "—" if r.wave is None else str(r.wave),
                    r.merged,
                    _pr_cell(r.pr_url),
                    f"{r.usd:.2f}",
                    f"{r.seconds / 60:.0f}",
                    f"`{r.run_id}`",
                    _cell(r.note, 160) or "—",
                ]
            )
        )
    if waves:
        out += [
            "",
            "### Waves (D2: merged with `git merge --no-ff` in the sandbox, gate on the result)",
            "",
            *_table("campaign at wave issues merged not-merged image gate".split()),
        ]
        for w in waves:
            nm = "; ".join(f"#{i}: {why}" for i, why in (w.get("not_merged") or {}).items())
            out.append(
                _tr(
                    [
                        f"`{w['campaign']}`",
                        _at(str(w.get("at") or "")),
                        str(w["wave"]),
                        _issues(w["issues"]),
                        _issues(w["merged"]) or "—",
                        _cell(nm, 200) or "—",
                        f"`{str(w.get('image', ''))[:8]}`",
                        str(w.get("gate", "")),
                    ]
                )
            )
    return "\n".join(out) + "\n"


def _tr(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _table(header: Sequence[str]) -> list[str]:
    return [_tr(header), "|" + "---|" * len(header)]


def _cell(text: str, n: int) -> str:
    return text.replace("|", "/").replace("\n", " ")[:n]


def _at(at: str) -> str:
    return f"`{at}`" if at else "head"


def _issues(issues: Sequence[int]) -> str:
    return " ".join(f"#{i}" for i in issues)


# ------------------------------------------------------------------------------ live


def issue_source(repo: str, number: int) -> str:
    """hunter when the issue carries the Hunter's label, else backlog."""
    import subprocess

    res = subprocess.run(
        ["gh", "issue", "view", str(number), "-R", repo, "--json", "labels"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return "backlog"
    labels = {str(lb.get("name", "")) for lb in json.loads(res.stdout).get("labels", [])}
    return "hunter" if HUNT_LABEL in labels else "backlog"


def live_deps(settings: Settings, *, at: str | None) -> CampaignDeps:
    """Token Factory + Sandboxes + GitHub (host credentials; no token enters a sandbox)."""
    from warden.integrate.branch import bundle_rev, export_patch, push_integration_branch
    from warden.pipeline import live_steps
    from warden.sandbox.box import Box, ContreeBackend

    s = settings
    repo = s.target_repo
    cache = s.runs_dir / ".repos" / repo.replace("/", "__")

    def open_box(ref: str, log: EventLog) -> Box:
        backend = ContreeBackend(s.nebius_api_key, s.contree_base_url, s.nebius_project_id)
        return Box.from_ref(backend, ref, log, phase="pr")

    def prepare(base: Box) -> tuple[Box, str]:
        return prepare_base(
            base,
            rev=at,
            cwd=s.workdir,
            wrap=s.shell_wrap,
            migrate=MIGRATE.get(s.gate_profile, ""),
            bundle=lambda head, rev: bundle_rev(
                repo, rev=rev, exclude=head, cache_dir=cache, base_branch=s.base_branch
            ),
        )

    return CampaignDeps(
        steps=live_steps(),
        open_box=open_box,
        prepare=prepare,
        source_of=lambda n: issue_source(repo, n),
        export=lambda box, issue, title: export_patch(box, cwd=s.workdir, issue=issue, title=title),
        push_integration=lambda start, heads, name: push_integration_branch(
            repo, start=start, heads=heads, name=name, cache_dir=cache
        ),
    )
