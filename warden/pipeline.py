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
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from warden.config import Settings
from warden.runlog.events import EventLog
from warden.schemas import Candidate, CandidateSummary, Phase, Plan, RunEvent, Verdict

if TYPE_CHECKING:
    from warden.hunt.issue import IssueText
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
# A repo's own suite (WARDEN_TEST_CMD) gets 10 min; a hang past that is inconclusive, not red.
EXTERNAL_SUITE_TIMEOUT = 600


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
    # best-of-N: this step runs for candidate `candidate` of `candidates` (0 of 1 = no best-of-N);
    # `variant` is the note the machine hands the implementer so candidates differ
    candidate: int = 0
    candidates: int = 1
    variant: str = ""

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
        if ctx.settings.candidates > 1:
            return best_of_n(steps, ctx, outputs["plan"], ctx.settings.candidates)
        return steps.fix(ctx, outputs["plan"])
    if phase == "verify":
        chosen = ctx.artifacts / BEST_OF_N
        if chosen.exists():  # best-of-N verified every candidate already; this is the winner's
            return Verdict.model_validate_json(chosen.read_text(encoding="utf-8"))
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


# ------------------------------------------------------------------------- best-of-N

BEST_OF_N = "best_of_n.json"  # the winner's Verdict with every candidate listed
TEMPERATURES = (0.1, 0.5, 0.8)  # candidate 1 keeps the single-run default; later ones reuse 0.8
_TIERS = {"PASS": 0, "PASS_WITH_CONCERNS": 1, "FAIL": 2}
RANK_RULE = (
    "verdict (PASS > PASS_WITH_CONCERNS > inconclusive FAIL > FAIL), then a new test that failed "
    "on the unfixed base, then fewer regressions + unmet spec behaviours, then fewer concerns, "
    "then a smaller diff, then lower cost, then the earlier candidate"
)
_CRITERIA = (
    "verdict",
    "theater",
    "regressions + unmet spec",
    "concerns",
    "diff size",
    "cost",
    "order",
)


def candidate_temperature(k: int) -> float:
    return TEMPERATURES[min(max(k, 1), len(TEMPERATURES)) - 1]


def rank_key(c: CandidateSummary) -> tuple[int, int, int, int, int, float, int]:
    """Best-of-N order, lowest first (RANK_RULE). A skipped candidate sorts after every
    verified one; an unmeasured diff after every measured one."""
    if c.status != "verified" or c.decision is None:
        tier = 9
    else:
        tier = _TIERS[c.decision] + (1 if c.decision == "FAIL" and not c.inconclusive else 0)
    return (
        tier,
        0 if c.theater_failed_on_base else 1,
        c.regressions + c.spec_unmet,
        c.concerns,
        c.diff_lines if c.diff_lines is not None else 10**9,
        round(c.cost_usd, 4),
        c.index,
    )


def _label(c: CandidateSummary) -> str:
    if c.decision is None:
        return "skipped"
    return c.decision + (" (inconclusive)" if c.decision == "FAIL" and c.inconclusive else "")


def why_chosen(winner: CandidateSummary, others: list[CandidateSummary]) -> str:
    """The winner, then per other verified candidate the first rank criterion that separated
    them, then which candidates never ran and why."""
    wk = rank_key(winner)
    parts: list[str] = []
    for o in others:
        if o.status != "verified":
            continue
        ok = rank_key(o)
        i = next(i for i, (a, b) in enumerate(zip(wk, ok, strict=True)) if a != b)
        if i == 0:
            detail = f"{_label(winner)} vs {_label(o)}"
        elif i == 1:
            detail = "its new test fails on the unfixed base"
        elif i == 4:
            other = o.diff_lines if o.diff_lines is not None else "?"
            detail = f"{winner.diff_lines} vs {other} lines"
        elif i == 5:
            detail = f"${winner.cost_usd:.2f} vs ${o.cost_usd:.2f}"
        elif i == 6:
            detail = "a tie; the earlier one"
        else:
            detail = f"{wk[i]} vs {ok[i]}"
        parts.append(f"over #{o.index} on {_CRITERIA[i]} ({detail})")
    skipped = [o for o in others if o.status != "verified"]
    if skipped:
        ids = ", ".join(f"#{o.index}" for o in skipped)
        parts.append(f"{ids} not run ({skipped[0].skipped_because})")
    head = f"candidate #{winner.index} ({_label(winner)})"
    return head + (": " + "; ".join(parts) if parts else "")


def _summary(k: int, cand: Candidate, verdict: Verdict, usd: float) -> CandidateSummary:
    return CandidateSummary(
        index=k,
        image_uuid=cand.image_uuid,
        decision=verdict.decision,
        theater_failed_on_base=verdict.theater_failed_on_base,
        inconclusive=verdict.inconclusive,
        regressions=len(verdict.regressions),
        spec_unmet=len(verdict.spec_unmet),
        concerns=len(verdict.concerns),
        diff_lines=verdict.diff_lines,
        cost_usd=round(usd, 6),
        tool_turns=cand.tool_turns,
        tests_added=len(cand.tests_added),
        reasons=verdict.reasons[:6],
    )


def candidate_note(k: int, n: int, earlier: list[CandidateSummary]) -> str:
    """The implementer's note for candidate k > 1: who it is and how earlier ones were judged,
    so it does not repeat their approach. Candidate 1 gets none (the single-run prompt)."""
    if k <= 1:
        return ""
    lines = [
        f"Note: you are candidate {k} of {n} for this issue, on your own fork of the unfixed "
        "code. The verifier judged the earlier candidates:"
    ]
    for c in earlier:
        lines.append(f"- #{c.index}: {_label(c)}: {'; '.join(c.reasons[:3]) or 'no reasons'}")
    lines.append(
        "Take a different approach where they fell short, and re-check the plan's assumptions "
        "in the code before following it."
    )
    return "\n".join(lines)


def best_of_n(steps: Steps, ctx: StepContext, plan: Plan, n: int) -> Candidate:
    """Fix + verify up to `n` candidates, each forked from the same base, one after another;
    return the best by `rank_key` and write its Verdict, every candidate listed, to BEST_OF_N.

    Each candidate's events carry `candidate: k`; its artifacts land in artifacts/candidate-<k>/
    and the winner's are copied up. Early stop: once a candidate is a clean PASS (PASS, and a new
    test failed on the unfixed base) the rest are skipped: only a smaller diff could outrank it.
    Budget: a candidate is skipped when the spend so far plus the costliest candidate so far
    would pass the per-issue cap. The cap itself (CostMeter, fed by events.jsonl) covers every
    candidate combined and is never caught."""
    done: list[CandidateSummary] = []
    pairs: dict[int, tuple[Candidate, Verdict]] = {}
    costliest = 0.0
    skip = ""
    cap = ctx.settings.max_usd_per_issue
    for k in range(1, n + 1):
        last = done[-1] if done else None
        if not skip and last and last.decision == "PASS" and last.theater_failed_on_base:
            skip = f"early stop: #{last.index} is a clean PASS"
        if not skip and last and ctx.spent_usd() + costliest > cap:
            skip = (
                f"budget: ${ctx.spent_usd():.2f} spent + ${costliest:.2f} for a candidate "
                f"> ${cap:.2f} cap"
            )
        if skip:
            done.append(CandidateSummary(index=k, status="skipped", skipped_because=skip))
            ctx.log.emit("fix", "note", {"text": f"candidate {k}/{n} skipped: {skip}"})
            continue
        sub = replace(
            ctx,
            log=ctx.log.tagged(candidate=k),
            candidate=k,
            candidates=n,
            variant=candidate_note(k, n, done),
        )
        sub.log.emit("fix", "note", {"text": f"candidate {k}/{n}: a fork of the base"})
        ctx.progress.step(f"candidate {k}/{n}")
        before = ctx.spent_usd()
        cand = steps.fix(sub, plan)
        verdict = steps.verify(sub, plan, cand)
        usd = ctx.spent_usd() - before
        costliest = max(costliest, usd)
        pairs[k] = (cand, verdict)
        done.append(_summary(k, cand, verdict, usd))
        ctx.progress.step(f"candidate {k}/{n}: {verdict.decision} (${usd:.2f})")

    winner = min(done, key=rank_key)
    why = why_chosen(winner, [c for c in done if c.index != winner.index])
    cand, verdict = pairs[winner.index]
    for f in sorted((ctx.artifacts / f"candidate-{winner.index}").glob("*")):
        if f.is_file():
            shutil.copyfile(f, ctx.artifacts / f.name)
    final = verdict.model_copy(
        update={"candidates": done, "chosen": winner.index, "chosen_why": why}
    )
    _write_atomic(ctx.artifacts / BEST_OF_N, final.model_dump_json(indent=2))
    ctx.log.emit(
        "verify",
        "note",
        {
            "text": f"best of {n}: {why}",
            "best_of_n": n,
            "chosen": winner.index,
            "candidates": [
                {"index": c.index, "status": c.status, "decision": c.decision} for c in done
            ],
        },
    )
    ctx.progress.step(f"chose {why}")
    return cand


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
    """For `--issue N`: fetch the issue (the Hunter proper lands in C5). With `--from-event` the
    issue text came from the GitHub event payload: no `gh` call, and no comments."""
    from warden.hunt.issue import fetch_issue

    if ctx.issue is not None:
        ctx.progress.step(f"#{ctx.issue.number} {ctx.issue.title}")
        return IssueRef(issue=ctx.issue.number, title=ctx.issue.title)
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
    # best-of-N candidates differ by temperature and by the machine's note (candidate 1 = default)
    llm = LLM(
        s,
        CostMeter(cap_usd=s.max_usd_per_issue - ctx.spent_usd()),
        ctx.log,
        temperature=candidate_temperature(ctx.candidate),
    )
    dispatcher = ToolDispatcher(ctx.log, cwd=s.workdir, shell_wrap=s.shell_wrap)
    profile = PROFILES[s.gate_profile]
    cand = run_implementer(
        llm,
        base,
        plan,
        ctx.log,
        dispatcher=dispatcher,
        max_tool_turns=s.max_tool_turns,
        idle_nudge_after=s.idle_nudge_after,
        read_cache=s.read_cache,
        progress=ctx.progress,
        lint=profile.lint,
        autofix=profile.autofix,
        variant=ctx.variant,
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
    from dataclasses import replace

    from warden.verify.gate import PROFILES

    s = ctx.settings
    profile = PROFILES[s.gate_profile]
    if not s.test_cmd:
        return verify_candidate(ctx, cand, profile, issue_number=plan.issue)
    # The repo's own test command: the gate's suite and the regression check (per-test outcomes).
    return verify_candidate(
        ctx,
        cand,
        replace(profile, suite=s.test_cmd),
        issue_number=plan.issue,
        regression_cmd=f"{s.test_cmd} -rA",
        suite_timeout=EXTERNAL_SUITE_TIMEOUT,
    )


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
            base,
            new_tests,
            node_ids=cand.tests_added,
            cwd=s.workdir,
            wrap=s.shell_wrap,
            candidate=candidate,
            issue_text=f"{ctx.issue.title}\n{ctx.issue.body}" if ctx.issue else "",
        )
        ctx.log.artifact(
            "theater.txt",
            "".join(f"{n}: {r}\n" for n, r in theater.reasons.items())
            + f"\n# base (exit {theater.exit_code})\n{theater.stdout}",
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
    shown = candidate.run("git add -A -N . && git diff HEAD", cwd=s.workdir, keep=False)
    diff = shown.stdout
    spec = _spec_check(ctx, diff, cand, issue_number) if s.spec_check else None
    verdict = decide(cand, theater, gate, regression=regression, spec=spec)
    verdict = verdict.model_copy(update={"diff_lines": diff_lines(diff) if shown.ok else None})
    ctx.progress.step(f"verdict: {verdict.decision}")
    return verdict


def diff_lines(diff: str) -> int:
    """Lines added + removed in a unified diff (file headers not counted)."""
    return sum(
        1
        for line in diff.splitlines()
        if line[:1] in "+-" and line[:1] and not line.startswith(("+++", "---"))
    )


def _spec_check(ctx: StepContext, diff: str, cand: Candidate, issue_number: int) -> SpecResult:
    """One VERDICT call: the candidate diff against the issue's stated behaviours. Comments are
    never sent (on a replay they hold the human plan)."""
    from warden.hunt.issue import fetch_issue
    from warden.llm.client import LLM
    from warden.llm.cost import CostMeter
    from warden.verify.spec import spec_review

    s = ctx.settings
    issue = ctx.issue or fetch_issue(s.target_repo, issue_number, include_comments=False)
    llm = LLM(s, CostMeter(cap_usd=s.max_usd_per_issue - ctx.spent_usd()), ctx.log)
    spec = spec_review(llm, issue, diff=diff, tests=cand.tests_added, system=_prompt("spec.md"))
    ctx.log.artifact("spec.json", spec.review.model_dump_json(indent=2))
    ctx.log.note("verify", f"spec: {spec.label}")
    ctx.progress.step(f"spec: {spec.label}")
    return spec


def pr_body(ctx: StepContext, ref: IssueRef, evidence: str, calls: dict[str, int]) -> str:
    """The PR body: WARDEN_PR_TEMPLATE (or the built-in one) with the evidence + AI disclosure."""
    from warden.integrate.pr import disclosure, render_pr_body

    s = ctx.settings
    template = None
    if s.pr_template:
        path = Path(s.pr_template).expanduser()
        if not path.is_file():
            raise NotReadyError(
                f"WARDEN_PR_TEMPLATE {s.pr_template} is not a file",
                fix="point WARDEN_PR_TEMPLATE at a markdown file, or unset it",
            )
        template = path.read_text(encoding="utf-8")
    routing = s.routing_table()
    return render_pr_body(
        template,
        evidence=evidence,
        disclosure_md=disclosure(routing, calls),
        issue=ref.issue,
        title=ref.title,
        repo=s.target_repo,
        run_id=ctx.log.run_id,
        models=", ".join(dict.fromkeys(m for _, m in routing)),
    )


def _live_pr(
    ctx: StepContext, issue: IssueRef, plan: Plan, cand: Candidate, verdict: Verdict
) -> PrOutcome:
    from warden.integrate.branch import export_patch, push_fix_branch, refuse_protected
    from warden.integrate.fork import resolve_push_target
    from warden.integrate.pr import (
        PENDING_LABEL,
        PrOpenError,
        confirm_pr,
        evidence_table,
        labels_for,
        open_draft_pr,
    )
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
    evidence = evidence_table(
        issue.issue,
        issue.title,
        plan,
        verdict,
        cost_usd=ctx.spent_usd(),
        calls_by_model=calls,
        note=note,
    )
    body = pr_body(ctx, issue, evidence, calls)
    title = f"{'[Warden replay] ' if ctx.at_rev else '[Warden] '}#{issue.issue} {issue.title}"
    label = labels[0] if labels else None
    if verdict.decision == "FAIL":
        ctx.progress.step("verdict FAIL: no branch pushed, no PR opened (body in artifacts)")
        return PrOutcome(body=body, title=title, label=label, url=None)
    if not confirm_pr(ctx.yes, what=f"{s.target_repo}: {title}", run_id=ctx.log.run_id):
        return PrOutcome(body=body, title=title, label=label, url=None)

    # Push only where GitHub grants push access; otherwise to the user's own fork.
    target = resolve_push_target(s.target_repo, log=ctx.log)
    if target.forked and ctx.at_rev is not None:
        raise PrOpenError(
            f"a replay PR needs push access to {s.target_repo} (its base branch lives there).",
            fix="replay on a repo you can push to, or drop --at",
        )
    candidate = open_base(s, ctx.log, phase="pr").sibling(cand.image_uuid)
    patch = export_patch(candidate, cwd=s.workdir, issue=issue.issue, title=issue.title)
    ctx.log.artifact("candidate.patch", patch)
    refuse_protected(patch)  # a fix never rewrites CI: issue text may be a prompt injection
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
        push_url=target.push_url,
        base_branch=s.base_branch,
    )
    ctx.progress.step(f"pushed {pushed.head} to {target.repo}, base {pushed.base}")
    pr_labels = labels
    if target.forked and labels:
        # A fork's author cannot label the upstream PR: the label goes in the title instead.
        pr_labels = []
        title = f"[{PENDING_LABEL}] {title}"
    pr = open_draft_pr(
        s.target_repo,
        target.head(pushed.head),
        pushed.base,
        title,
        body,
        labels=pr_labels,
        log=ctx.log,
    )
    ctx.progress.step(f"draft PR {pr.url}")
    return PrOutcome(body=body, title=title, label=label, url=pr.url)
