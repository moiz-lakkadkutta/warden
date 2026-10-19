"""E2 campaign driver on the fake sandbox: plan → waves → fix/verify/PR → merge → gate → rows."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from tests.test_merger import GitFake
from warden.config import Settings
from warden.eval import campaign as cp
from warden.pipeline import IssueRef, PrOutcome, StepContext, Steps
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.schemas import Candidate, Plan, RiskClass, Verdict

FILES = {196: "a.py", 197: "b.py", 200: "a.py", 205: "c.py", 207: "d.py"}
RISK: dict[int, list[RiskClass]] = {197: ["money"]}
FAILS = {207}


class World:
    """Scripted steps over one GitFake backend; records where each fix started."""

    def __init__(self) -> None:
        self.backend = GitFake()
        self.fix_base: dict[int, str] = {}
        self.pr_base: dict[int, str | None] = {}
        self.pushed: list[tuple[str, list[str], str]] = []
        self.lock = threading.Lock()

    def box(self, ref: str, log: EventLog) -> Box:
        return Box.from_ref(self.backend, ref, log)

    def steps(self) -> Steps:
        def hunt(ctx: StepContext) -> IssueRef:
            assert ctx.requested_issue is not None
            return IssueRef(issue=ctx.requested_issue, title=f"bug {ctx.requested_issue}")

        def plan(ctx: StepContext, ref: IssueRef) -> Plan:
            return Plan(
                issue=ref.issue,
                summary="s",
                files_to_touch=[FILES[ref.issue]],
                tests_to_add=[],
                landing_order=[],
                risk_class=RISK.get(ref.issue, ["none"]),
                citations=[],
                acceptance=[],
            )

        def fix(ctx: StepContext, p: Plan) -> Candidate:
            assert ctx.base_ready
            with self.lock:
                self.fix_base[p.issue] = ctx.settings.base_image
                self.pr_base[p.issue] = ctx.pr_base
                child = self.box(ctx.settings.base_image, ctx.log).run("true", keep=True).box
            assert child is not None
            return Candidate(
                issue=p.issue,
                image_uuid=child.uuid,
                tool_turns=3,
                tests_added=[f"tests/test_{p.issue}.py::t"],
                cost_usd=0.0,
            )

        def verify(ctx: StepContext, p: Plan, c: Candidate) -> Verdict:
            ctx.log.emit("verify", "llm", {"model": "m"}, usd=0.1)
            return Verdict(
                decision="FAIL" if p.issue in FAILS else "PASS",
                theater_failed_on_base=p.issue not in FAILS,
                mypy_ok=True,
                ruff_ok=True,
                alembic_single_head=True,
                openapi_drift=False,
                reasons=["new tests do not fail on base"] if p.issue in FAILS else [],
            )

        def pr(ctx: StepContext, ref: IssueRef, p: Plan, c: Candidate, v: Verdict) -> PrOutcome:
            if v.decision == "FAIL":
                return PrOutcome(body="b", title="t", label=None, url=None)
            ctx.log.artifact("candidate.patch", f"From x\nSubject: fix #{ref.issue}\n")
            url = f"https://github.com/o/r/pull/{1000 + ref.issue}"
            return PrOutcome(body="b", title="t", label=None, url=url)

        return Steps(mode="live", hunt=hunt, plan=plan, fix=fix, verify=verify, pr=pr)

    def deps(self) -> cp.CampaignDeps:
        def push(start: str, heads: list[str], name: str) -> str:
            self.pushed.append((start, heads, name))
            return "pushed-sha"

        return cp.CampaignDeps(
            steps=self.steps(),
            open_box=self.box,
            prepare=lambda base: (base, "base0"),
            source_of=lambda n: "hunter" if n == 205 else "backlog",
            export=lambda box, issue, title: f"From y\nSubject: #{issue}\n",
            push_integration=push,
        )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(_env_file=None, WARDEN_RUNS_DIR=str(tmp_path / "runs"))  # type: ignore[call-arg]
    s.max_forks = 4
    s.max_usd_per_issue = 1.5
    s.gate_profile = "generic"
    s.shell_wrap = ""
    s.test_cmd = ""
    return s


def test_campaign_waves_merge_and_stack(tmp_path: Path, settings: Settings) -> None:
    world = World()
    out = tmp_path / "evals"
    rows = cp.run_campaign(
        [196, 197, 200, 205, 207],
        settings,
        world.deps(),
        campaign_id="c1",
        at="abc^",
        out_dir=out,
        yes=True,
        budget_usd=15.0,
        progress_out=open(tmp_path / "progress.txt", "w"),  # noqa: SIM115
    )
    by = {r.issue: r for r in rows}
    # 196 and 200 share a.py: 200 waits for wave 2; the rest fit in wave 1 (≤ max_forks)
    assert {i: by[i].wave for i in by} == {196: 1, 197: 1, 200: 2, 205: 1, 207: 1}
    # wave 1 merged its three passing fixes; 207 FAILed so it never reached the merge
    assert [by[i].merged for i in (196, 197, 205, 207)] == ["y", "y", "y", "—"]
    assert by[207].verdict == "FAIL" and by[207].pr_url == ""
    assert "new tests do not fail on base" in by[207].note
    # wave 2 forked wave 1's integration image and stacked its PR on the pushed branch
    wave1_base = world.fix_base[196]
    assert world.fix_base[200] != wave1_base
    assert world.backend.images[world.fix_base[200]].files["/git/HEAD"] == b"m205"
    assert world.pr_base[196] is None and world.pr_base[200] == "warden/campaign-c1-w1"
    assert world.pushed == [
        (
            "base0",
            ["warden/fix-196-c1-196", "warden/fix-197-c1-197", "warden/fix-205-c1-205"],
            "warden/campaign-c1-w1",
        )
    ]
    assert by[200].merged == "y"
    # rows: PENDING-HUMAN from the plan's risk, source, cost, run ids
    assert by[197].pending_human == "y: money" and by[196].pending_human == "n"
    assert by[205].source == "hunter" and by[196].source == "backlog"
    assert by[196].usd == pytest.approx(0.1) and by[196].run_id == "c1-196"
    assert by[196].repro == "y" and by[207].repro == "n"
    # files
    csv_rows = cp.read_rows(out / cp.CSV_NAME)
    assert [r.issue for r in csv_rows] == [196, 197, 200, 205, 207]
    md = (out / cp.MD_NAME).read_text()
    assert "| **final** (best attempt per issue) | 5 | 4 | 4 | 0 | 1 | 1 | 4 | 4 | $0.50 |" in md
    assert "[#1196](https://github.com/o/r/pull/1196)" in md
    assert cp.BEGIN in md and "## Notes" in md
    gate = settings.runs_dir / "c1" / "artifacts" / "wave-1-gate.txt"
    assert gate.exists() and "## suite" in gate.read_text()


def test_rerun_resumes_and_keeps_notes(tmp_path: Path, settings: Settings) -> None:
    world = World()
    out = tmp_path / "evals"
    args = dict(campaign_id="c2", at=None, out_dir=out, yes=True, budget_usd=15.0)
    cp.run_campaign([196], settings, world.deps(), **args)  # type: ignore[arg-type]
    md = out / cp.MD_NAME
    md.write_text(md.read_text().replace("(to be written)", "hand-written notes"))
    world.fix_base.clear()
    rows = cp.run_campaign([196], settings, world.deps(), **args)  # type: ignore[arg-type]
    assert world.fix_base == {}  # every phase already done: nothing ran again
    assert rows[0].verdict == "PASS" and rows[0].pr_url.endswith("/1196")
    assert "hand-written notes" in md.read_text()
    assert len(cp.read_rows(out / cp.CSV_NAME)) == 1


def test_budget_skips_a_wave(tmp_path: Path, settings: Settings) -> None:
    world = World()
    rows = cp.run_campaign(
        [196, 197],
        settings,
        world.deps(),
        campaign_id="c3",
        at=None,
        out_dir=tmp_path / "evals",
        yes=True,
        budget_usd=2.0,  # 2 issues x $1.5 cap > $2
    )
    assert {r.verdict for r in rows} == {"SKIPPED"}
    assert world.fix_base == {}


def test_best_rows_keeps_the_best_attempt_and_csv_keeps_both(tmp_path: Path) -> None:
    def row(campaign: str, verdict: str, usd: float) -> cp.Row:
        return cp.Row(197, "backlog", verdict, "n", 1, "—", "", usd, 60, at="x^", campaign=campaign)

    path = tmp_path / "c.csv"
    cp.write_rows(path, [row("a", "FAIL", 0.2)])
    rows = cp.write_rows(path, [row("b", "PASS", 0.3)])
    assert len(rows) == 2
    assert [r.campaign for r in cp.best_rows(rows)] == ["b"]
    md = cp.render_markdown(rows, [])
    assert "| **final** (best attempt per issue) | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | $0.50 |" in md


def test_best_rows_prefers_a_confirmed_repro_on_a_verdict_tie() -> None:
    def row(campaign: str, repro: str) -> cp.Row:
        return cp.Row(206, "backlog", "FAIL", "y", 1, "—", "", 0.2, 60, at="x^", campaign=campaign,
                      repro=repro)  # fmt: skip

    assert [r.campaign for r in cp.best_rows([row("d", "y"), row("e", "n")])] == ["d"]
    assert [r.campaign for r in cp.best_rows([row("d", "y"), row("e", "y")])] == ["e"]


def test_pending_reason() -> None:
    p = Plan(
        issue=1,
        summary="s",
        files_to_touch=[],
        tests_to_add=[],
        landing_order=[],
        risk_class=["tax", "concurrency"],
        citations=[],
        acceptance=[],
    )
    assert cp.pending_reason(p) == "y: tax"
    assert cp.pending_reason(None) == "n"
