"""The pipeline state machine: hunt → plan → fix → verify → pr as five explicit steps.

Each step reads the previous step's artifact and writes exactly one of its own under
runs/<id>/artifacts/. Artifact presence is the single source of truth for "done": a phase is
complete iff its MARKER file exists, so `warden resume` continues from the last marker and a
half-run phase (no marker) simply reruns. Implementations are injected through `Steps`; the demo
and the live pipeline run through the same machine.

Nothing here imports the UI or typer, and nothing here catches an exception: errors travel up to
the CLI, which owns the sanctioned translations (cost cap, PR open, not-ready).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from warden.config import Settings
from warden.runlog.events import EventLog
from warden.schemas import Candidate, Phase, Plan, RunEvent, Verdict

if TYPE_CHECKING:
    from warden.hunt.issue import IssueText
    from warden.sandbox.box import Box
    from warden.verify.gate import GateCommands
    from warden.verify.spec import SpecResult

PHASES: tuple[Phase, ...] = ("hunt", "plan", "fix", "verify", "pr")
MARKER: dict[Phase, str] = {
    "hunt": "issue.json",
    "plan": "plan.json",
    "fix": "candidate.json",
    "verify": "verdict.json",
    "pr": "pr.json",
}
Mode = Literal["demo", "live"]


class Progress(Protocol):
    """What the machine and the steps write to stderr; `ui.PhaseLog` satisfies it."""

    def phase(self, phase: Phase, text: str) -> None: ...
    def step(self, text: str) -> None: ...
    def cost(self, usd: float) -> None: ...
    def finish(self, text: str) -> None: ...


class NullProgress:
    """Null object for `progress=None`: same API as PhaseLog, writes nothing."""

    def phase(self, phase: Phase, text: str) -> None:
        pass

    def step(self, text: str) -> None:
        pass

    def cost(self, usd: float) -> None:
        pass

    def finish(self, text: str) -> None:
        pass


@dataclass(frozen=True)
class IssueRef:
    issue: int
    title: str


@dataclass(frozen=True)
class PrOutcome:
    body: str
    title: str
    label: str | None
    url: str | None


class NotReadyError(RuntimeError):
    """A step whose live implementation has not landed yet. `str(err)` is the human sentence."""

    def __init__(self, message: str, *, fix: str) -> None:
        super().__init__(message)
        self.fix = fix


@dataclass(frozen=True)
class StepContext:
    settings: Settings
    log: EventLog
    run_dir: Path
    artifacts: Path
    progress: Progress
    yes: bool
    json_out: bool
    requested_issue: int | None
    at_rev: str | None = None  # replay: plan/fix/verify against this commit, not HEAD
    issue: IssueText | None = None  # given issue text (eval tasks); None = fetch from GitHub

    def spent_usd(self) -> float:
        """Running total over events.jsonl (the log flushes per emit, so this session counts)."""
        return _spent(self.run_dir)


@dataclass(frozen=True)
class Steps:
    """Step implementations. Steps own their progress lines; the machine owns everything else."""

    mode: Mode
    hunt: Callable[[StepContext], IssueRef]
    plan: Callable[[StepContext, IssueRef], Plan]
    fix: Callable[[StepContext, Plan], Candidate]
    verify: Callable[[StepContext, Plan, Candidate], Verdict]
    pr: Callable[[StepContext, IssueRef, Plan, Candidate, Verdict], PrOutcome]


# ----------------------------------------------------------------------------- state


def completed_phases(run_dir: Path) -> list[Phase]:
    """Longest prefix of PHASES whose markers exist under run_dir/artifacts (a gap ends it)."""
    done: list[Phase] = []
    for phase in PHASES:
        if not (run_dir / "artifacts" / MARKER[phase]).exists():
            break
        done.append(phase)
    return done


def load_inputs(artifacts: Path, upto: Phase) -> dict[Phase, Any]:
    """Parse every marker up to and including `upto`; a missing one is a ValueError."""
    inputs: dict[Phase, Any] = {}
    for phase in PHASES[: PHASES.index(upto) + 1]:
        path = artifacts / MARKER[phase]
        if not path.exists():
            raise ValueError(f"cannot resume after {upto}: artifacts/{MARKER[phase]} missing")
        inputs[phase] = _parse(phase, path)
    return inputs


def _parse(phase: Phase, path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if phase == "hunt":
        return IssueRef(**json.loads(text))
    if phase == "plan":
        return Plan.model_validate_json(text)
    if phase == "fix":
        return Candidate.model_validate_json(text)
    if phase == "verify":
        return Verdict.model_validate_json(text)
    body = (path.parent / "pr_body.md").read_text(encoding="utf-8")
    return PrOutcome(body=body, **json.loads(text))


def _spent(run_dir: Path) -> float:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return 0.0
    total = 0.0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line:
            total += RunEvent.model_validate_json(line).usd or 0.0
    return round(total, 6)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_marker(artifacts: Path, phase: Phase, out: Any) -> None:
    """One artifact per phase; for pr the body lands first so pr.json stays the marker."""
    if phase == "hunt":
        text = json.dumps(asdict(out), indent=2)
    elif phase == "pr":
        _write_atomic(artifacts / "pr_body.md", out.body)
        text = json.dumps({"title": out.title, "label": out.label, "url": out.url}, indent=2)
    else:
        text = out.model_dump_json(indent=2)
    _write_atomic(artifacts / MARKER[phase], text)


# --------------------------------------------------------------------------- machine


def run_pipeline(
    settings: Settings,
    log: EventLog,
    steps: Steps,
    *,
    start_after: Phase | None,
    json_out: bool,
    yes: bool,
    progress: Progress | None,
    stop_after: Phase | None = None,
    requested_issue: int | None = None,
    at_rev: str | None = None,
    issue: IssueText | None = None,
) -> dict[str, Any]:
    """Run every phase after `start_after` up to `stop_after`; return the result dict.

    run.json is written once, when absent; an existing one is the run's manifest — its mode must
    match the Steps and its `issue` fills `requested_issue` when the caller passed None. Resume:
    `start_after` must be the last completed phase — the marker is the next phase's input, so the
    two coincide by construction.
    """
    prog: Progress = NullProgress() if progress is None else progress
    run_dir = log.dir
    artifacts = run_dir / "artifacts"
    manifest = run_dir / "run.json"
    if manifest.exists():
        recorded = json.loads(manifest.read_text(encoding="utf-8"))
        if recorded.get("mode") != steps.mode:
            raise ValueError(
                f"run {log.run_id} has mode {recorded.get('mode')!r}, steps are {steps.mode!r}"
            )
        if requested_issue is None:
            requested_issue = recorded.get("issue")
        if at_rev is None:
            at_rev = recorded.get("at")
    elif start_after is not None:
        raise ValueError(f"cannot resume {log.run_id}: run.json missing")
    else:
        record: dict[str, Any] = {"mode": steps.mode, "issue": requested_issue}
        if at_rev is not None:
            record["at"] = at_rev
        _write_atomic(manifest, json.dumps(record))
    ctx = StepContext(
        settings=settings,
        log=log,
        run_dir=run_dir,
        artifacts=artifacts,
        progress=prog,
        yes=yes,
        json_out=json_out,
        requested_issue=requested_issue,
        at_rev=at_rev,
        issue=issue,
    )
    done = completed_phases(run_dir)
    outputs: dict[Phase, Any] = {}
    if start_after is not None:
        if not done or start_after != done[-1]:
            raise ValueError(
                f"cannot resume after {start_after}: completed phases are "
                f"{', '.join(done) or 'none'}"
            )
        outputs = load_inputs(artifacts, start_after)
        prog.cost(_spent(run_dir))
        prog.step(f"resumed after {start_after}; skipping {', '.join(done)}")

    first = 0 if start_after is None else PHASES.index(start_after) + 1
    for phase in PHASES[first:]:
        if start_after is not None and phase == PHASES[first]:
            log.emit(
                phase, "note", {"text": f"resumed after {start_after}", "resumed_from": start_after}
            )
        out = _call(steps, phase, ctx, outputs)
        _write_marker(artifacts, phase, out)
        outputs[phase] = out
        log.emit(
            phase,
            "note",
            {"text": f"{phase} done → artifacts/{MARKER[phase]}", "phase_done": phase},
        )
        if phase == stop_after:
            break

    result = _result(outputs, run_dir, stop_after, start_after)
    if "pr" in outputs:
        prog.finish(
            f"{result['verdict']} · body in {settings.runs_dir}/{log.run_id}/artifacts/pr_body.md"
        )
    elif stop_after == "plan":
        prog.finish("plan printed (--dry-run)")
    else:
        prog.finish(f"stopped after {stop_after}")
    log.result(**result)
    return result


def _call(steps: Steps, phase: Phase, ctx: StepContext, outputs: dict[Phase, Any]) -> Any:
    if phase == "hunt":
        return steps.hunt(ctx)
    if phase == "plan":
        return steps.plan(ctx, outputs["hunt"])
    if phase == "fix":
        return steps.fix(ctx, outputs["plan"])
    if phase == "verify":
        return steps.verify(ctx, outputs["plan"], outputs["fix"])
    return steps.pr(ctx, outputs["hunt"], outputs["plan"], outputs["fix"], outputs["verify"])


def _result(
    outputs: dict[Phase, Any], run_dir: Path, stop_after: Phase | None, start_after: Phase | None
) -> dict[str, Any]:
    if "pr" in outputs:
        evidence = str(outputs["pr"].body)
    elif stop_after == "plan" and "plan" in outputs:
        evidence = str(outputs["plan"].model_dump_json(indent=2))
    else:
        evidence = ""
    return {
        "issue": outputs["hunt"].issue if "hunt" in outputs else None,
        "verdict": outputs["verify"].decision if "verify" in outputs else None,
        "cost_usd": _spent(run_dir),
        "evidence": evidence,
        "pr_url": outputs["pr"].url if "pr" in outputs else None,
        "resumed_from": start_after,
    }


# ------------------------------------------------------------------------------ live


def live_steps() -> Steps:
    """hunt → plan → fix → verify → pr on Nebius (Token Factory + Sandboxes) and GitHub."""
    return Steps(
        mode="live",
        hunt=_live_hunt,
        plan=_live_plan,
        fix=_live_fix,
        verify=_live_verify,
        pr=_live_pr,
    )


def _prompt(name: str) -> str:
    return (Path(__file__).resolve().parents[1] / "prompts" / name).read_text(encoding="utf-8")


def _base_box(ctx: StepContext, phase: Phase) -> Any:
    """The base image; for a replay (`--at`), a kept fork checked out at that commit."""
    import shlex

    from warden.sandbox.box import open_base

    s = ctx.settings
    base = open_base(s, ctx.log, phase=phase)
    if ctx.at_rev is None:
        return base
    co = base.run(f"git checkout -q --detach {shlex.quote(ctx.at_rev)}", cwd=s.workdir, keep=True)
    if not co.ok or co.box is None:
        raise NotReadyError(
            f"cannot check out {ctx.at_rev} in the base image: {co.stderr.strip()[:200]}",
            fix="pass a commit that exists in the target repo (git rev-parse it first)",
        )
    return co.box


def _live_hunt(ctx: StepContext) -> IssueRef:
    """For `--issue N`: fetch the issue (the Hunter proper lands in C5)."""
    from warden.hunt.issue import fetch_issue

    if ctx.requested_issue is None:
        raise NotReadyError("autonomous hunting lands in C5", fix="pass --issue N")
    issue = fetch_issue(ctx.settings.target_repo, ctx.requested_issue, include_comments=True)
    ctx.progress.step(f"#{issue.number} {issue.title}")
    return IssueRef(issue=issue.number, title=issue.title)


def _live_plan(ctx: StepContext, ref: IssueRef) -> Plan:
    from warden.hunt.issue import fetch_issue
    from warden.llm.client import LLM
    from warden.llm.cost import CostMeter
    from warden.plan.planner import FABRIC_LAYOUT, generic_layout, plan_issue, sandbox_context
    from warden.plan.research import research, tavily_client
    from warden.sandbox.box import open_base

    s = ctx.settings
    # A replay hides comments: on a closed issue they often hold the human's plan.
    issue = ctx.issue or fetch_issue(s.target_repo, ref.issue, include_comments=ctx.at_rev is None)
    layout = FABRIC_LAYOUT if s.gate_profile == "fabric" else generic_layout(s.workdir)
    meter = CostMeter(cap_usd=s.max_usd_per_issue - ctx.spent_usd())
    llm = LLM(s, meter, ctx.log)
    box = open_base(s, ctx.log, phase="plan")
    tav = tavily_client(s.tavily_api_key)
    plan = plan_issue(
        llm,
        issue,
        context=sandbox_context(
            lambda cmd: box.run(cmd, cwd="/", keep=False).stdout, rev=ctx.at_rev, layout=layout
        ),
        search=lambda queries: research(queries, client=tav, log=ctx.log),
        log=ctx.log,
        prompt=planner_prompt(s.gate_profile),
    )
    ctx.progress.cost(meter.spent_usd)  # the implementer reports its own; the planner doesn't
    ctx.progress.step(f"{len(plan.files_to_touch)} files, {len(plan.citations)} citations")
    return plan


def _live_fix(ctx: StepContext, plan: Plan) -> Candidate:
    from warden.fix.implementer import run_implementer
    from warden.fix.tools import ToolDispatcher
    from warden.llm.client import LLM
    from warden.llm.cost import CostMeter
    from warden.verify.gate import PROFILES

    s = ctx.settings
    base = _base_box(ctx, "fix")
    llm = LLM(s, CostMeter(cap_usd=s.max_usd_per_issue - ctx.spent_usd()), ctx.log)
    dispatcher = ToolDispatcher(ctx.log, cwd=s.workdir, shell_wrap=s.shell_wrap)
    profile = PROFILES[s.gate_profile]
    cand = run_implementer(
        llm,
        base,
        plan,
        ctx.log,
        dispatcher=dispatcher,
        max_tool_turns=s.max_tool_turns,
        progress=ctx.progress,
        lint=profile.lint,
        autofix=profile.autofix,
    )
    ctx.progress.step(
        f"{'done' if cand.done else 'blocked'} in {cand.tool_turns} turns, "
        f"{len(cand.tests_added)} new tests"
    )
    return cand


def planner_prompt(profile: str) -> str:
    """prompts/planner.md with the repo description for `profile` (Fabric's, or a generic one)."""
    repo = _prompt(f"repos/{'fabric' if profile == 'fabric' else 'generic'}.md").strip()
    return _prompt("planner.md").replace("{repo}", repo)


def _live_verify(ctx: StepContext, plan: Plan, cand: Candidate) -> Verdict:
    from warden.verify.gate import PROFILES

    return verify_candidate(ctx, cand, PROFILES[ctx.settings.gate_profile], issue_number=plan.issue)


def verify_candidate(
    ctx: StepContext,
    cand: Candidate,
    commands: GateCommands,
    *,
    issue_number: int = 0,
    regression_cmd: str = "",
    suite_timeout: int = 1800,
) -> Verdict:
    """Theater check on the base, the gate on the candidate, the regression check (when
    `regression_cmd` is set: the repo's own test command, `-rA` output), the spec review (when
    WARDEN_SPEC_CHECK is on), then the deterministic verdict."""
    import shlex

    from warden.verify.gate import run_gate
    from warden.verify.regression import regression_check
    from warden.verify.theater import theater_check
    from warden.verify.verifier import decide

    s = ctx.settings
    base = _base_box(ctx, "verify")
    candidate = base.sibling(cand.image_uuid)
    theater = None
    if cand.tests_added:
        files = sorted({n.split("::", 1)[0] for n in cand.tests_added})
        new_tests = {
            f: candidate.run(f"cat {shlex.quote(f)}", cwd=s.workdir, keep=False).stdout.encode()
            for f in files
        }
        theater = theater_check(
            base, new_tests, node_ids=cand.tests_added, cwd=s.workdir, wrap=s.shell_wrap
        )
        ctx.log.note("verify", f"theater: {theater.label}")
        ctx.progress.step(f"theater: {theater.label}")
    ran = [
        n
        for n, cmd in (
            ("suite", commands.suite),
            ("mypy", commands.mypy),
            ("ruff", commands.ruff_check),
            ("alembic", commands.alembic_upgrade),
            ("OpenAPI", commands.openapi_snapshot),
        )
        if cmd
    ]
    ctx.progress.step(f"gate: {', '.join(ran) or 'nothing configured'}")
    gate = run_gate(
        candidate,
        ctx.log,
        commands=commands,
        cwd=s.workdir,
        wrap=s.shell_wrap,
        timeout=suite_timeout,
    )
    ctx.log.artifact(
        "gate.txt",
        "\n\n".join(
            f"## {st.name} (exit {st.exit_code}, {st.ms / 1000:.1f} s)\n$ {st.shell}\n{st.tail}"
            for st in gate.steps
        ),
    )
    regression = None
    if regression_cmd:
        regression = regression_check(
            base, candidate, test_cmd=regression_cmd, cwd=s.workdir, wrap=s.shell_wrap, timeout=600
        )
        label = (
            regression.label if regression else "not run (no existing test names a changed module)"
        )
        ctx.log.note("verify", f"regression: {label}")
        ctx.progress.step(f"regression: {label}")
        if regression is not None:
            ctx.log.artifact(
                "regression.txt", f"files: {' '.join(regression.files)}\n\n{regression.output}"
            )
    spec = _spec_check(ctx, candidate, cand, issue_number) if s.spec_check else None
    verdict = decide(cand, theater, gate, regression=regression, spec=spec)
    ctx.progress.step(f"verdict: {verdict.decision}")
    return verdict


def _spec_check(ctx: StepContext, candidate: Box, cand: Candidate, issue_number: int) -> SpecResult:
    """One VERDICT call: the candidate diff against the issue's stated behaviours. Comments are
    never sent (on a replay they hold the human plan)."""
    from warden.hunt.issue import fetch_issue
    from warden.llm.client import LLM
    from warden.llm.cost import CostMeter
    from warden.verify.spec import spec_review

    s = ctx.settings
    issue = ctx.issue or fetch_issue(s.target_repo, issue_number, include_comments=False)
    diff = candidate.run("git add -A -N . && git diff HEAD", cwd=s.workdir, keep=False).stdout
    llm = LLM(s, CostMeter(cap_usd=s.max_usd_per_issue - ctx.spent_usd()), ctx.log)
    spec = spec_review(llm, issue, diff=diff, tests=cand.tests_added, system=_prompt("spec.md"))
    ctx.log.artifact("spec.json", spec.review.model_dump_json(indent=2))
    ctx.log.note("verify", f"spec: {spec.label}")
    ctx.progress.step(f"spec: {spec.label}")
    return spec


def _live_pr(
    ctx: StepContext, issue: IssueRef, plan: Plan, cand: Candidate, verdict: Verdict
) -> PrOutcome:
    from warden.integrate.branch import export_patch, push_fix_branch
    from warden.integrate.pr import confirm_pr, evidence_table, labels_for, open_draft_pr
    from warden.sandbox.box import open_base

    s = ctx.settings
    labels = labels_for(plan)
    note = (
        f"Replay of a closed issue at `{ctx.at_rev}`: re-solved from the issue text alone "
        "(comments hidden) on the code as it was before the human fix. Evidence, not for merging."
        if ctx.at_rev
        else None
    )
    calls: dict[str, int] = {}
    for line in (ctx.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
        ev = RunEvent.model_validate_json(line)
        if ev.kind == "llm":
            model = str(ev.data.get("model", "?"))
            calls[model] = calls.get(model, 0) + 1
    body = evidence_table(
        issue.issue,
        issue.title,
        plan,
        verdict,
        cost_usd=ctx.spent_usd(),
        calls_by_model=calls,
        note=note,
    )
    title = f"{'[Warden replay] ' if ctx.at_rev else '[Warden] '}#{issue.issue} {issue.title}"
    label = labels[0] if labels else None
    if verdict.decision == "FAIL":
        ctx.progress.step("verdict FAIL: no branch pushed, no PR opened (body in artifacts)")
        return PrOutcome(body=body, title=title, label=label, url=None)
    if not confirm_pr(ctx.yes, what=f"{s.target_repo}: {title}", run_id=ctx.log.run_id):
        return PrOutcome(body=body, title=title, label=label, url=None)

    candidate = open_base(s, ctx.log, phase="pr").sibling(cand.image_uuid)
    patch = export_patch(candidate, cwd=s.workdir, issue=issue.issue, title=issue.title)
    ctx.log.artifact("candidate.patch", patch)
    base_rev = None
    if ctx.at_rev is not None:
        sha = candidate.run(f"git rev-parse {ctx.at_rev}", cwd=s.workdir, keep=False)
        base_rev = sha.stdout.strip() if sha.ok else ctx.at_rev
    pushed = push_fix_branch(
        s.target_repo,
        patch,
        issue=issue.issue,
        run_id=ctx.log.run_id,
        base_rev=base_rev,
        cache_dir=s.runs_dir / ".repos" / s.target_repo.replace("/", "__"),
    )
    ctx.progress.step(f"pushed {pushed.head} on {pushed.base}")
    pr = open_draft_pr(
        s.target_repo, pushed.head, pushed.base, title, body, labels=labels, log=ctx.log
    )
    ctx.progress.step(f"draft PR {pr.url}")
    return PrOutcome(body=body, title=title, label=label, url=pr.url)
