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
from typing import Any, Literal, Protocol

from warden.config import Settings
from warden.runlog.events import EventLog
from warden.schemas import Candidate, Phase, Plan, RunEvent, Verdict

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
) -> dict[str, Any]:
    """Run every phase after `start_after` up to `stop_after`; return the result dict.

    Fresh start (`start_after is None`): writes run.json. Resume: run.json must exist with the
    Steps' mode and `start_after` must be the last completed phase — the marker is the next
    phase's input, so the two coincide by construction.
    """
    prog: Progress = NullProgress() if progress is None else progress
    run_dir = log.dir
    artifacts = run_dir / "artifacts"
    manifest = run_dir / "run.json"
    ctx = StepContext(
        settings=settings,
        log=log,
        run_dir=run_dir,
        artifacts=artifacts,
        progress=prog,
        yes=yes,
        json_out=json_out,
        requested_issue=requested_issue,
    )
    done = completed_phases(run_dir)
    outputs: dict[Phase, Any] = {}
    if start_after is None:
        _write_atomic(manifest, json.dumps({"mode": steps.mode, "issue": requested_issue}))
    else:
        if not manifest.exists():
            raise ValueError(f"cannot resume {log.run_id}: run.json missing")
        mode = json.loads(manifest.read_text(encoding="utf-8")).get("mode")
        if mode != steps.mode:
            raise ValueError(f"run {log.run_id} has mode {mode!r}, steps are {steps.mode!r}")
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
    """Placeholder until the live steps land (Phase B): every step raises NotReadyError."""

    def _not_ready(*_: Any) -> Any:
        raise NotReadyError(
            "live pipeline lands in Phase B (see docs/warden-plan.md §5)", fix="use --demo for now"
        )

    return Steps(
        mode="live",
        hunt=_not_ready,
        plan=_not_ready,
        fix=_not_ready,
        verify=_not_ready,
        pr=_not_ready,
    )
