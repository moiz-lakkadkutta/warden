"""D4 best-of-N: N candidates on forks of one base, the verifier ranks, the winner goes on."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from warden.config import Settings
from warden.fix.implementer import build_messages
from warden.integrate.pr import evidence_table
from warden.llm.cost import CostCapExceededError
from warden.pipeline import (
    BEST_OF_N,
    IssueRef,
    PrOutcome,
    StepContext,
    Steps,
    candidate_note,
    candidate_temperature,
    diff_lines,
    rank_key,
    run_pipeline,
    why_chosen,
)
from warden.runlog.events import EventLog
from warden.runlog.render import best_of_n_choice, render_html
from warden.sandbox.box import Box
from warden.sandbox.fake import FakeBackend
from warden.schemas import Candidate, CandidateSummary, Decision, Plan, RunEvent, Verdict


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


def _verdict(decision: Decision, **kw: Any) -> Verdict:
    base: dict[str, Any] = {
        "decision": decision,
        "theater_failed_on_base": decision != "FAIL",
        "mypy_ok": True,
        "ruff_ok": True,
        "alembic_single_head": True,
        "openapi_drift": False,
        "reasons": [f"reason for {decision}"],
    }
    return Verdict(**{**base, **kw})


def _s(index: int, decision: Decision | None, **kw: Any) -> CandidateSummary:
    return CandidateSummary(index=index, decision=decision, **kw)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key in list(os.environ):
        if key.startswith("WARDEN_"):
            monkeypatch.delenv(key)
    return Settings(_env_file=None, WARDEN_RUNS_DIR=str(tmp_path / "runs"))  # type: ignore[call-arg]


class World:
    """Scripted steps over a FakeBackend: candidate k forks the one base image, writes a fix,
    spends `usd[k]`, and is judged `verdicts[k]`."""

    def __init__(self, verdicts: dict[int, Verdict], usd: dict[int, float] | None = None) -> None:
        self.backend = FakeBackend()
        self.base_image = self.backend.use("base")
        self.base_uuid = self.backend.uuid_of(self.base_image)
        self.verdicts = verdicts
        self.usd = usd or {}
        self.fixed: list[int] = []
        self.verified: list[int] = []
        self.contexts: list[StepContext] = []

    def steps(self) -> Steps:
        def hunt(ctx: StepContext) -> IssueRef:
            return IssueRef(7, "seven")

        def plan(ctx: StepContext, ref: IssueRef) -> Plan:
            return _plan()

        def fix(ctx: StepContext, plan: Plan) -> Candidate:
            k = ctx.candidate
            self.fixed.append(k)
            self.contexts.append(ctx)
            base = Box(self.backend, self.base_image, ctx.log, phase="fix")
            child = base.run(f"echo fix{k} > a.py", keep=True).box
            assert child is not None
            ctx.log.emit("fix", "llm", {"model": "m"}, usd=self.usd.get(k, 0.1))
            return Candidate(
                issue=7, image_uuid=child.uuid, tool_turns=k, tests_added=["t.py"], cost_usd=0.1
            )

        def verify(ctx: StepContext, plan: Plan, cand: Candidate) -> Verdict:
            self.verified.append(ctx.candidate)
            ctx.log.artifact("gate.txt", f"gate of candidate {ctx.candidate}")
            return self.verdicts[ctx.candidate]

        def pr(
            ctx: StepContext, issue: IssueRef, plan: Plan, cand: Candidate, verdict: Verdict
        ) -> PrOutcome:
            return PrOutcome(body="body", title="t", label=None, url=None)

        return Steps(mode="demo", hunt=hunt, plan=plan, fix=fix, verify=verify, pr=pr)


def _run(settings: Settings, world: World, **kw: Any) -> tuple[EventLog, dict[str, Any]]:
    log = EventLog(settings.runs_dir)
    result = run_pipeline(
        settings,
        log,
        world.steps(),
        start_after=None,
        json_out=False,
        yes=True,
        progress=None,
        **kw,
    )
    log.close()
    return log, result


def _events(log: EventLog) -> list[RunEvent]:
    return EventLog.read(log.dir.parent, log.run_id)


# ------------------------------------------------------------------------------- ranking


def test_rank_orders_by_verdict_then_theater_then_regressions_concerns_diff_cost_index() -> None:
    order = [
        _s(9, "PASS", theater_failed_on_base=True, diff_lines=50, cost_usd=0.9),
        _s(8, "PASS", theater_failed_on_base=False, diff_lines=1),
        _s(7, "PASS_WITH_CONCERNS", theater_failed_on_base=True, concerns=1),
        _s(6, "PASS_WITH_CONCERNS", theater_failed_on_base=True, concerns=2),
        _s(5, "FAIL", inconclusive=True),
        _s(1, "FAIL", regressions=0, spec_unmet=1, diff_lines=3),
        _s(2, "FAIL", regressions=1, spec_unmet=1, diff_lines=3),
        _s(3, "FAIL", regressions=2, diff_lines=3, cost_usd=0.1),
        _s(4, "FAIL", regressions=2, diff_lines=3, cost_usd=0.2),
        _s(0, None, status="skipped"),
    ]
    assert sorted(reversed(order), key=rank_key) == order


def test_within_a_tier_a_smaller_diff_wins_then_cost_then_the_earlier_one() -> None:
    a = _s(1, "PASS", theater_failed_on_base=True, diff_lines=40, cost_usd=0.1)
    b = _s(2, "PASS", theater_failed_on_base=True, diff_lines=12, cost_usd=0.5)
    c = _s(3, "PASS", theater_failed_on_base=True, diff_lines=12, cost_usd=0.5)
    unmeasured = _s(4, "PASS", theater_failed_on_base=True, diff_lines=None)
    assert min([a, b, c, unmeasured], key=rank_key) is b
    assert rank_key(a) < rank_key(unmeasured)


def test_why_names_the_first_criterion_that_separated_each_loser() -> None:
    win = _s(2, "PASS", theater_failed_on_base=True, diff_lines=12)
    why = why_chosen(
        win,
        [
            _s(1, "FAIL"),
            _s(3, "PASS", theater_failed_on_base=True, diff_lines=40),
            _s(4, None, status="skipped", skipped_because="early stop: #2 is a clean PASS"),
        ],
    )
    assert why.startswith("candidate #2 (PASS): ")
    assert "over #1 on verdict (PASS vs FAIL)" in why
    assert "over #3 on diff size (12 vs 40 lines)" in why
    assert "#4 not run (early stop: #2 is a clean PASS)" in why


# ------------------------------------------------------------------------------- machine


def test_n1_is_the_single_candidate_pipeline(settings: Settings) -> None:
    world = World({0: _verdict("PASS")})
    log, result = _run(settings, world)
    assert world.fixed == [0] and world.verified == [0]
    assert not (log.dir / "artifacts" / BEST_OF_N).exists()
    verdict = json.loads((log.dir / "artifacts" / "verdict.json").read_text())
    assert "candidates" not in verdict and "chosen" not in verdict and "diff_lines" not in verdict
    assert not any("candidate" in ev.data for ev in _events(log))
    assert result["verdict"] == "PASS"


def test_three_candidates_fork_the_same_base_and_the_best_is_chosen(settings: Settings) -> None:
    settings.candidates = 3
    world = World(
        {
            1: _verdict("FAIL"),
            2: _verdict("PASS_WITH_CONCERNS", concerns=["theater inconclusive"]),
            3: _verdict("PASS_WITH_CONCERNS", concerns=["a", "b"]),
        }
    )
    log, result = _run(settings, world)
    art = log.dir / "artifacts"
    assert world.fixed == [1, 2, 3] and world.verified == [1, 2, 3]

    # every candidate's first checkpoint is a child of the same base image: a fork
    forks = [
        ev for ev in _events(log) if ev.kind == "sandbox" and ev.data["from"] == world.base_uuid
    ]
    assert [ev.data["candidate"] for ev in forks] == [1, 2, 3]
    assert len({ev.data["to"] for ev in forks}) == 3

    cand = Candidate.model_validate_json((art / "candidate.json").read_text())
    verdict = Verdict.model_validate_json((art / "verdict.json").read_text())
    assert cand.tool_turns == 2 and verdict.chosen == 2  # fewer concerns than #3
    decisions = [c.decision for c in verdict.candidates]
    assert decisions == ["FAIL", "PASS_WITH_CONCERNS", "PASS_WITH_CONCERNS"]
    assert (
        "over #1 on verdict" in verdict.chosen_why and "over #3 on concerns" in verdict.chosen_why
    )
    assert result["verdict"] == "PASS_WITH_CONCERNS"
    # per-candidate artifacts kept apart; the winner's copied up
    assert (art / "candidate-3" / "gate.txt").read_text() == "gate of candidate 3"
    assert (art / "gate.txt").read_text() == "gate of candidate 2"
    # cost is the sum over candidates, and every event says which candidate it belongs to
    assert result["cost_usd"] == pytest.approx(0.3)
    llm = [ev for ev in _events(log) if ev.kind == "llm"]
    assert [ev.data["candidate"] for ev in llm] == [1, 2, 3]
    assert best_of_n_choice(_events(log)) == (
        2,
        {1: "FAIL", 2: "PASS_WITH_CONCERNS", 3: "PASS_WITH_CONCERNS"},
    )


def test_later_candidates_get_a_different_temperature_and_the_earlier_verdicts(
    settings: Settings,
) -> None:
    settings.candidates = 2
    world = World(
        {1: _verdict("FAIL", reasons=["theater: every new test passes"]), 2: _verdict("PASS")}
    )
    _run(settings, world)
    first, second = world.contexts
    assert (first.candidate, first.candidates, first.variant) == (1, 2, "")
    assert second.candidate == 2 and "candidate 2 of 2" in second.variant
    assert "#1: FAIL: theater: every new test passes" in second.variant
    assert candidate_temperature(0) == candidate_temperature(1) == 0.1
    assert len({candidate_temperature(k) for k in (1, 2, 3)}) == 3
    assert candidate_temperature(8) == candidate_temperature(3)


def test_a_clean_pass_stops_early(settings: Settings) -> None:
    settings.candidates = 3
    world = World({1: _verdict("PASS"), 2: _verdict("PASS"), 3: _verdict("PASS")})
    log, _ = _run(settings, world)
    assert world.fixed == [1]
    verdict = Verdict.model_validate_json((log.dir / "artifacts" / "verdict.json").read_text())
    assert verdict.chosen == 1
    assert [c.status for c in verdict.candidates] == ["verified", "skipped", "skipped"]
    assert verdict.candidates[1].skipped_because == "early stop: #1 is a clean PASS"


def test_a_pass_whose_theater_was_inconclusive_does_not_stop_early(settings: Settings) -> None:
    settings.candidates = 2
    world = World({1: _verdict("PASS", theater_failed_on_base=False), 2: _verdict("PASS")})
    log, _ = _run(settings, world)
    assert world.fixed == [1, 2]
    verdict = Verdict.model_validate_json((log.dir / "artifacts" / "verdict.json").read_text())
    assert verdict.chosen == 2 and "on theater" in verdict.chosen_why


def test_a_candidate_that_could_pass_the_cap_is_skipped(settings: Settings) -> None:
    settings.candidates = 3
    settings.max_usd_per_issue = 1.0
    world = World({1: _verdict("FAIL"), 2: _verdict("FAIL"), 3: _verdict("PASS")}, {1: 0.3, 2: 0.4})
    log, _ = _run(settings, world)
    # 0.3 + 0.3 ≤ 1.0 → #2 runs; 0.7 + 0.4 > 1.0 → #3 is skipped
    assert world.fixed == [1, 2]
    verdict = Verdict.model_validate_json((log.dir / "artifacts" / "verdict.json").read_text())
    assert verdict.candidates[2].skipped_because.startswith("budget: $0.70 spent")


def test_the_cost_cap_is_never_caught_to_try_another_candidate(settings: Settings) -> None:
    settings.candidates = 3
    world = World({1: _verdict("FAIL"), 2: _verdict("PASS")})
    steps = world.steps()
    inner = steps.fix

    def capped(ctx: StepContext, plan: Plan) -> Candidate:
        if ctx.candidate == 2:
            raise CostCapExceededError(1.1, 1.0, "fix")
        return inner(ctx, plan)

    log = EventLog(settings.runs_dir)
    with pytest.raises(CostCapExceededError):
        run_pipeline(
            settings,
            log,
            Steps(steps.mode, steps.hunt, steps.plan, capped, steps.verify, steps.pr),
            start_after=None,
            json_out=False,
            yes=True,
            progress=None,
        )
    assert world.fixed == [1]
    assert not (log.dir / "artifacts" / "candidate.json").exists()


def test_resume_after_fix_reuses_the_ranked_verdict(settings: Settings) -> None:
    settings.candidates = 2
    world = World({1: _verdict("FAIL"), 2: _verdict("PASS_WITH_CONCERNS", concerns=["c"])})
    log = EventLog(settings.runs_dir)
    run_pipeline(
        settings,
        log,
        world.steps(),
        start_after=None,
        json_out=False,
        yes=True,
        progress=None,
        stop_after="fix",
    )
    assert world.verified == [1, 2]
    result = run_pipeline(
        settings,
        log,
        world.steps(),
        start_after="fix",
        json_out=False,
        yes=True,
        progress=None,
    )
    log.close()
    assert world.verified == [1, 2]  # not re-verified
    assert result["verdict"] == "PASS_WITH_CONCERNS"


# ---------------------------------------------------------------------- evidence and console


def test_pr_body_says_chosen_from_n_candidates() -> None:
    verdict = _verdict(
        "PASS",
        candidates=[_s(1, "FAIL"), _s(2, "PASS"), _s(3, None, status="skipped")],
        chosen=2,
        chosen_why="candidate #2 (PASS): over #1 on verdict (PASS vs FAIL)",
    )
    body = evidence_table(7, "seven", _plan(), verdict, cost_usd=0.5, calls_by_model={})
    assert (
        "| Best-of-N | #2 chosen from 3 candidates (#1 FAIL · #2 PASS · #3 skipped): "
        "candidate #2 (PASS): over #1 on verdict (PASS vs FAIL) |" in body
    )
    single = evidence_table(7, "seven", _plan(), _verdict("PASS"), cost_usd=0.5, calls_by_model={})
    assert "Best-of-N" not in single


def test_console_draws_a_card_per_candidate_and_tags_log_lines(settings: Settings) -> None:
    settings.candidates = 2
    world = World({1: _verdict("FAIL"), 2: _verdict("PASS")})
    log, result = _run(settings, world)
    page = render_html(log.run_id, _events(log), result, None)
    assert "candidate #1" in page and "candidate #2" in page
    assert "PASS · chosen" in page
    assert "data-candidate='2'" in page


def test_tagged_log_shares_the_files_and_separates_artifacts(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    view = log.tagged(candidate=3)
    view.emit("fix", "note", {"text": "x"}, usd=0.5)
    log.emit("fix", "note", {"text": "y"})
    view.artifact("gate.txt", "g")
    log.close()
    events = EventLog.read(tmp_path, log.run_id)
    assert [ev.data.get("candidate") for ev in events] == [3, None]
    assert (log.dir / "artifacts" / "candidate-3" / "gate.txt").read_text() == "g"
    assert '"candidate": 3' in (log.dir / "cost.jsonl").read_text()


def test_implementer_note_is_appended_to_the_user_message() -> None:
    msgs = build_messages("p", _plan(), 10, variant="Note: you are candidate 2 of 3")
    assert msgs[1]["content"].endswith("Note: you are candidate 2 of 3")
    assert build_messages("p", _plan(), 10)[1]["content"].endswith("1. x")


def test_candidate_note_is_empty_for_the_first() -> None:
    assert candidate_note(1, 3, []) == ""


def test_diff_lines_counts_changes_not_headers() -> None:
    diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n-a\n+b\n+c\n d\n"
    assert diff_lines(diff) == 3
    assert diff_lines("") == 0
