"""The hand-written state machine: markers, resume, atomic writes, live placeholder."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any

import pytest

from warden.config import Settings
from warden.pipeline import (
    MARKER,
    PHASES,
    IssueRef,
    NotReadyError,
    PrOutcome,
    StepContext,
    Steps,
    completed_phases,
    live_steps,
    load_inputs,
    run_pipeline,
)
from warden.runlog.events import EventLog
from warden.schemas import Candidate, Phase, Plan, Verdict

# ------------------------------------------------------------------------- scripted steps


class Recorder:
    """Progress that records every call; `crash_at` makes that step raise."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def phase(self, phase: Phase, text: str) -> None:
        self.calls.append(("phase", (phase, text)))

    def step(self, text: str) -> None:
        self.calls.append(("step", text))

    def cost(self, usd: float) -> None:
        self.calls.append(("cost", usd))

    def finish(self, text: str) -> None:
        self.calls.append(("finish", text))


def _plan() -> Plan:
    return Plan(
        issue=7,
        summary="lock the row",
        files_to_touch=["a.py"],
        tests_to_add=["tests/test_a.py"],
        landing_order=["test", "a"],
        risk_class=["none"],
        citations=[],
        acceptance=["x"],
    )


def _verdict() -> Verdict:
    return Verdict(
        decision="PASS",
        theater_failed_on_base=True,
        suite={"passed": 3, "failed": 0},
        mypy_ok=True,
        ruff_ok=True,
        alembic_single_head=True,
        openapi_drift=False,
    )


def scripted(ran: list[Phase], *, crash_at: Phase | None = None, usd: float = 0.5) -> Steps:
    """Steps that append their phase to `ran`, emit one priced note each, and return fixtures."""

    def _go(ctx: StepContext, phase: Phase) -> None:
        if phase == crash_at:
            raise RuntimeError(f"boom at {phase}")
        ran.append(phase)
        ctx.log.emit(phase, "note", {"text": f"{phase} work"}, usd=usd)

    def hunt(ctx: StepContext) -> IssueRef:
        _go(ctx, "hunt")
        return IssueRef(7, "seven")

    def plan(ctx: StepContext, issue: IssueRef) -> Plan:
        assert issue == IssueRef(7, "seven")
        _go(ctx, "plan")
        return _plan()

    def fix(ctx: StepContext, plan: Plan) -> Candidate:
        assert plan == _plan()
        _go(ctx, "fix")
        return Candidate(issue=7, image_uuid="img-9", tool_turns=1, tests_added=[], cost_usd=0.5)

    def verify(ctx: StepContext, plan: Plan, cand: Candidate) -> Verdict:
        assert cand.image_uuid == "img-9"
        _go(ctx, "verify")
        return _verdict()

    def pr(
        ctx: StepContext, issue: IssueRef, plan: Plan, cand: Candidate, verdict: Verdict
    ) -> PrOutcome:
        assert verdict.decision == "PASS"
        _go(ctx, "pr")
        return PrOutcome(body="## evidence\n", title="fix: seven", label=None, url=None)

    return Steps(mode="demo", hunt=hunt, plan=plan, fix=fix, verify=verify, pr=pr)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key in list(os.environ):
        if key.startswith("WARDEN_"):
            monkeypatch.delenv(key)
    return Settings(_env_file=None, WARDEN_RUNS_DIR=str(tmp_path / "runs"))


def _notes(run_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
        if line and json.loads(line)["kind"] == "note"
    ]


# ------------------------------------------------------------------------------- tests


def test_all_phases_write_markers_and_result(settings: Settings) -> None:
    ran: list[Phase] = []
    log = EventLog(settings.runs_dir)
    prog = Recorder()
    result = run_pipeline(
        settings, log, scripted(ran), start_after=None, json_out=False, yes=False, progress=prog
    )
    log.close()
    run_dir = settings.runs_dir / log.run_id
    assert ran == list(PHASES)
    artifacts = run_dir / "artifacts"
    markers = [artifacts / MARKER[p] for p in PHASES]
    assert all(m.exists() for m in markers)
    mtimes = [m.stat().st_mtime_ns for m in markers]
    assert mtimes == sorted(mtimes)
    assert (artifacts / "pr_body.md").read_text() == "## evidence\n"
    assert json.loads((run_dir / "run.json").read_text()) == {"mode": "demo", "issue": None}
    assert set(result) == {"issue", "verdict", "cost_usd", "evidence", "pr_url", "resumed_from"}
    assert result["issue"] == 7 and result["verdict"] == "PASS" and result["pr_url"] is None
    assert result["evidence"] == "## evidence\n" and result["resumed_from"] is None
    assert result["cost_usd"] == pytest.approx(2.5)
    saved = json.loads((run_dir / "result.json").read_text())
    assert saved["run_id"] == log.run_id and saved["verdict"] == "PASS"
    finishes = [c for c in prog.calls if c[0] == "finish"]
    assert len(finishes) == 1
    assert finishes[0][1] == f"PASS · body in {settings.runs_dir}/{log.run_id}/artifacts/pr_body.md"
    done_notes = [n for n in _notes(run_dir) if "phase_done" in n["data"]]
    assert [n["data"]["phase_done"] for n in done_notes] == list(PHASES)
    assert done_notes[0]["data"]["text"] == "hunt done → artifacts/issue.json"
    assert completed_phases(run_dir) == list(PHASES)
    assert not list(artifacts.glob("*.tmp"))


def test_stop_after_plan(settings: Settings) -> None:
    ran: list[Phase] = []
    log = EventLog(settings.runs_dir)
    prog = Recorder()
    result = run_pipeline(
        settings,
        log,
        scripted(ran),
        start_after=None,
        json_out=False,
        yes=False,
        progress=prog,
        stop_after="plan",
    )
    log.close()
    artifacts = settings.runs_dir / log.run_id / "artifacts"
    assert ran == ["hunt", "plan"]
    assert sorted(p.name for p in artifacts.iterdir()) == ["issue.json", "plan.json"]
    assert result["verdict"] is None and result["pr_url"] is None
    assert result["evidence"] == _plan().model_dump_json(indent=2)
    assert ("finish", "plan printed (--dry-run)") in prog.calls
    assert completed_phases(settings.runs_dir / log.run_id) == ["hunt", "plan"]


def test_crash_then_resume(settings: Settings) -> None:
    ran: list[Phase] = []
    log = EventLog(settings.runs_dir)
    with pytest.raises(RuntimeError, match="boom at fix"):
        run_pipeline(
            settings,
            log,
            scripted(ran, crash_at="fix"),
            start_after=None,
            json_out=False,
            yes=False,
            progress=None,
        )
    log.close()
    run_dir = settings.runs_dir / log.run_id
    assert ran == ["hunt", "plan"]
    assert completed_phases(run_dir) == ["hunt", "plan"]
    assert not (run_dir / "result.json").exists()

    ran2: list[Phase] = []
    log2 = EventLog(settings.runs_dir, log.run_id)
    prog = Recorder()
    result = run_pipeline(
        settings,
        log2,
        scripted(ran2),
        start_after="plan",
        json_out=False,
        yes=False,
        progress=prog,
    )
    log2.close()
    assert ran2 == ["fix", "verify", "pr"]
    assert result["resumed_from"] == "plan" and result["verdict"] == "PASS"
    assert result["cost_usd"] == pytest.approx(2.5)  # 2 notes from session one + 3 now
    assert prog.calls[0] == ("cost", 1.0)  # pre-charged with what session one spent
    assert prog.calls[1] == ("step", "resumed after plan; skipping hunt, plan")
    resumed = [n for n in _notes(run_dir) if "resumed_from" in n["data"]]
    assert len(resumed) == 1 and resumed[0]["phase"] == "fix"
    assert resumed[0]["data"] == {"text": "resumed after plan", "resumed_from": "plan"}
    assert completed_phases(run_dir) == list(PHASES)
    assert json.loads((run_dir / "result.json").read_text())["resumed_from"] == "plan"


def test_resume_guards(settings: Settings) -> None:
    ran: list[Phase] = []
    log = EventLog(settings.runs_dir)
    run_pipeline(
        settings,
        log,
        scripted(ran),
        start_after=None,
        json_out=False,
        yes=False,
        progress=None,
        stop_after="plan",
    )
    log.close()
    run_dir = settings.runs_dir / log.run_id

    live = live_steps()
    log2 = EventLog(settings.runs_dir, log.run_id)
    with pytest.raises(ValueError, match="mode"):
        run_pipeline(
            settings, log2, live, start_after="plan", json_out=False, yes=False, progress=None
        )
    with pytest.raises(ValueError, match="hunt"):
        run_pipeline(
            settings,
            log2,
            scripted(ran),
            start_after="hunt",
            json_out=False,
            yes=False,
            progress=None,
        )
    log2.close()
    with pytest.raises(
        ValueError, match="cannot resume after fix: artifacts/candidate.json missing"
    ):
        load_inputs(run_dir / "artifacts", "fix")

    fresh = EventLog(settings.runs_dir)
    with pytest.raises(ValueError, match="run.json"):
        run_pipeline(
            settings,
            fresh,
            scripted(ran),
            start_after="plan",
            json_out=False,
            yes=False,
            progress=None,
        )
    fresh.close()


def test_completed_phases_prefix(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    assert completed_phases(tmp_path) == []
    (artifacts / "issue.json").write_text("{}")
    (artifacts / "candidate.json").write_text("{}")  # gap at plan.json
    assert completed_phases(tmp_path) == ["hunt"]
    (artifacts / "plan.json").write_text("{}")
    assert completed_phases(tmp_path) == ["hunt", "plan", "fix"]
    assert completed_phases(tmp_path / "nope") == []


def test_atomic_marker(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[Phase] = []
    log = EventLog(settings.runs_dir)
    real_replace = os.replace

    def flaky(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if str(dst).endswith("plan.json"):
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr("warden.pipeline.os.replace", flaky)
    with pytest.raises(OSError, match="disk full"):
        run_pipeline(
            settings, log, scripted(ran), start_after=None, json_out=False, yes=False, progress=None
        )
    log.close()
    artifacts = settings.runs_dir / log.run_id / "artifacts"
    assert (artifacts / "issue.json").exists()
    assert not (artifacts / "plan.json").exists()
    assert completed_phases(settings.runs_dir / log.run_id) == ["hunt"]


def test_live_steps_not_ready(settings: Settings) -> None:
    steps = live_steps()
    assert steps.mode == "live"
    log = EventLog(settings.runs_dir)
    with pytest.raises(NotReadyError) as ei:
        run_pipeline(
            settings,
            log,
            steps,
            start_after=None,
            json_out=False,
            yes=False,
            progress=None,
            requested_issue=42,
        )
    log.close()
    assert ei.value.fix == "use --demo for now"
    assert "Phase B" in str(ei.value)
    run_dir = settings.runs_dir / log.run_id
    assert json.loads((run_dir / "run.json").read_text()) == {"mode": "live", "issue": 42}
    assert list((run_dir / "artifacts").iterdir()) == []


def test_spent_usd_reads_events(settings: Settings) -> None:
    log = EventLog(settings.runs_dir)
    run_dir = settings.runs_dir / log.run_id
    ctx = StepContext(
        settings=settings,
        log=log,
        run_dir=run_dir,
        artifacts=run_dir / "artifacts",
        progress=Recorder(),
        yes=False,
        json_out=False,
        requested_issue=None,
    )
    assert ctx.spent_usd() == 0.0
    log.emit("hunt", "note", {"text": "a"}, usd=0.1)
    log.emit("plan", "llm", {"model": "m"}, usd=0.2)
    log.note("fix", "free")
    assert ctx.spent_usd() == pytest.approx(0.3)
    log.close()


def test_no_except_in_pipeline() -> None:
    import warden.pipeline

    src = Path(str(warden.pipeline.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Try | ast.TryStar)]
    assert "import typer" not in src and "from warden import ui" not in src
    assert "warden.ui" not in src


def test_existing_manifest_is_never_rewritten(settings: Settings) -> None:
    log = EventLog(settings.runs_dir)
    run_dir = settings.runs_dir / log.run_id
    manifest = run_dir / "run.json"
    manifest.write_text('{"mode": "demo", "issue": 7}')  # zero markers, e.g. crashed in hunt
    before = manifest.read_bytes()
    seen: list[int | None] = []
    ran: list[Phase] = []
    base = scripted(ran)

    def hunt(ctx: StepContext) -> IssueRef:
        seen.append(ctx.requested_issue)
        return base.hunt(ctx)

    steps = Steps(
        mode="demo", hunt=hunt, plan=base.plan, fix=base.fix, verify=base.verify, pr=base.pr
    )
    result = run_pipeline(
        settings, log, steps, start_after=None, json_out=False, yes=False, progress=None
    )
    log.close()
    assert manifest.read_bytes() == before
    assert seen == [7]
    assert result["verdict"] == "PASS" and ran == list(PHASES)


def test_existing_manifest_mode_guard_on_fresh_start(settings: Settings) -> None:
    log = EventLog(settings.runs_dir)
    manifest = settings.runs_dir / log.run_id / "run.json"
    manifest.write_text('{"mode": "live", "issue": 7}')
    ran: list[Phase] = []
    with pytest.raises(ValueError, match="mode"):
        run_pipeline(
            settings, log, scripted(ran), start_after=None, json_out=False, yes=False, progress=None
        )
    log.close()
    assert ran == []
    assert json.loads(manifest.read_text()) == {"mode": "live", "issue": 7}
