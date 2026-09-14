"""`warden run --demo`: the whole loop on the fake sandbox, no credentials.

It exists so a judge can see the five phases, the theater check and the evidence table in ten
seconds. The plan and verdict are fixtures; the sandbox operations are real calls to Box; the
phases run through the same state machine as the live pipeline (`warden.pipeline`), so
`warden resume` works on a demo run too.

Per-phase costs (0.12 + 0.31 + 0.80 + 0.15 + 0.04) sum to the result's 1.42; each phase puts
its cost on one note so cost.jsonl and the console timeline show it.
"""

from __future__ import annotations

import itertools
from typing import Any

from warden.config import Settings
from warden.integrate.pr import evidence_table, labels_for
from warden.pipeline import (
    PHASES,
    IssueRef,
    Progress,
    PrOutcome,
    StepContext,
    Steps,
    run_pipeline,
)
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.sandbox.fake import FakeBackend, FakeImage
from warden.schemas import Candidate, Citation, Phase, Plan, RaceResult, Verdict
from warden.verify.theater import theater_check

DEMO_ISSUE = 201
DEMO_TITLE = "double-posted voucher on concurrent invoice finalize"
DEMO_TEST: dict[str, bytes] = {
    "backend/tests/test_finalize_race.py": b"def test_finalize_is_serialised(): ...\n"
}
DEMO_IMPL: dict[str, bytes] = {"backend/app/service/invoice.py": b"# with_for_update() added\n"}
_TEST_PATH = next(iter(DEMO_TEST))


class DemoBackend(FakeBackend):
    """FakeBackend whose `use(ref)` is stable across processes.

    A ref seen before returns the same image; an unknown `img-NNNN` ref (a candidate uuid read
    back from candidate.json on resume) creates an image WITH that uuid and moves the counter
    past NNNN. So base=img-0001, candidate=img-0003, theater probe=img-0004 whether the run is
    one process or a crash plus `warden resume`.
    """

    def __init__(self) -> None:
        super().__init__()
        self._refs: dict[str, FakeImage] = {}
        self._hi = 0

    def _new(self, parent: FakeImage | None = None) -> FakeImage:
        img = super()._new(parent)
        self._hi = max(self._hi, int(img.uuid[4:]))
        return img

    def use(self, ref: str) -> Any:
        if ref in self._refs:
            return self._refs[ref]
        if ref in self.images:
            return self.images[ref]
        if ref.startswith("img-") and ref[4:].isdigit():
            n = int(ref[4:])
            img = FakeImage(ref)
            self.images[ref] = img
            if n > self._hi:
                self._hi = n
                self._ids = itertools.count(n + 1)
        else:
            img = super().use(ref)
        self._refs[ref] = img
        return img


def _plan_fixture() -> Plan:
    return Plan(
        issue=DEMO_ISSUE,
        summary="Lock the invoice row (SELECT … FOR UPDATE) before the status check in finalize().",
        files_to_touch=["backend/app/service/invoice.py"],
        tests_to_add=[_TEST_PATH],
        landing_order=["test", "service"],
        risk_class=["money", "concurrency"],
        citations=[
            Citation(
                url="https://docs.sqlalchemy.org/en/20/orm/queryguide/query.html#sqlalchemy.orm.Query.with_for_update",
                claim="with_for_update() emits SELECT … FOR UPDATE, serialising finalizers",
            )
        ],
        acceptance=["new test fails on base", "8-thread race yields exactly one voucher"],
    )


def demo_steps(*, crash_after: Phase | None = None) -> Steps:
    """The five demo steps over one DemoBackend.

    `crash_after`: the step after that phase raises before touching anything — for testing and
    demonstrating `warden resume`.
    """
    backend = DemoBackend()

    def _maybe_crash(phase: Phase) -> None:
        if crash_after is not None and PHASES.index(phase) == PHASES.index(crash_after) + 1:
            raise RuntimeError(f"simulated crash after {crash_after}")

    def hunt(ctx: StepContext) -> IssueRef:
        _maybe_crash("hunt")
        log, prog = ctx.log, ctx.progress
        prog.phase("hunt", "8 parallel POST /invoices/{id}/finalize")
        log.note("hunt", "8 parallel POST /invoices/{id}/finalize with distinct Idempotency-Key")
        prog.step("8 parallel POST /invoices/{id}/finalize with distinct Idempotency-Key")
        log.note(
            "hunt", "psql: SELECT count(*) FROM voucher WHERE reference_id=$INV → 3 (expected 1)"
        )
        prog.step("psql: SELECT count(*) FROM voucher WHERE reference_id=$INV → 3 (expected 1)")
        log.emit("hunt", "note", {"text": f"filed #{DEMO_ISSUE} [P0] {DEMO_TITLE}"}, usd=0.12)
        prog.cost(0.12)
        return IssueRef(DEMO_ISSUE, DEMO_TITLE)

    def plan(ctx: StepContext, issue: IssueRef) -> Plan:
        _maybe_crash("plan")
        plan = _plan_fixture()
        n = len(plan.citations)
        ctx.progress.phase(
            "plan", f"reading issue #{issue.issue}, {n} citation{'' if n == 1 else 's'}"
        )
        ctx.log.emit(
            "plan",
            "note",
            {"text": plan.summary, "citations": [c.url for c in plan.citations]},
            usd=0.31,
        )
        ctx.progress.cost(0.31)
        return plan

    def fix(ctx: StepContext, plan: Plan) -> Candidate:
        _maybe_crash("fix")
        log, prog = ctx.log, ctx.progress
        prog.phase("fix", "forking base → candidate")
        base = Box.from_ref(backend, ctx.settings.base_image, log, phase="fix")
        # On the unfixed base the new test fails; on the candidate it passes.
        backend.images[base.uuid].test_results[_TEST_PATH] = 1
        candidate = base.copy_in(DEMO_TEST).copy_in(DEMO_IMPL)
        backend.images[candidate.uuid].test_results[_TEST_PATH] = 0
        text = f"candidate checkpoint {candidate.uuid} after 7 tool turns"
        log.emit("fix", "note", {"text": text}, usd=0.80)
        prog.step(text)
        prog.cost(0.80)
        return Candidate(
            issue=plan.issue,
            image_uuid=candidate.uuid,
            tool_turns=7,
            tests_added=[_TEST_PATH],
            cost_usd=0.80,
        )

    def verify(ctx: StepContext, plan: Plan, cand: Candidate) -> Verdict:
        _maybe_crash("verify")
        log, prog = ctx.log, ctx.progress
        prog.phase("verify", "theater probe on unfixed base + full suite")
        base = Box.from_ref(backend, ctx.settings.base_image, log, phase="verify")
        candidate = Box.from_ref(backend, cand.image_uuid, log, phase="verify")
        # Re-seed so a resumed verify (fresh backend) sees the same outcomes as a fresh run.
        backend.images[base.uuid].test_results[_TEST_PATH] = 1
        backend.images[candidate.uuid].test_results[_TEST_PATH] = 0
        theater = theater_check(base, DEMO_TEST)
        suite = candidate.run("pytest -q backend/tests", keep=False)
        passed = 214 if suite.ok else 213
        log.note("verify", f"theater: {theater.label}")
        prog.step(f"theater: {theater.label}")
        log.emit("verify", "note", {"text": f"suite: {passed} passed"}, usd=0.15)
        prog.step(f"suite: {passed} passed")
        prog.cost(0.15)
        return Verdict(
            decision="PASS",
            theater_failed_on_base=theater.failed_on_base,
            suite={"passed": passed, "failed": 0 if suite.ok else 1},
            mypy_ok=True,
            ruff_ok=True,
            alembic_single_head=True,
            openapi_drift=False,
            race=RaceResult(threads=8, effects_on_base=3, effects_on_candidate=1),
            not_verified=["frontend", "e-invoice path"],
            reasons=[
                "new test fails on base and passes on candidate",
                "race probe returns 1 effect",
            ],
        )

    def pr(
        ctx: StepContext, issue: IssueRef, plan: Plan, cand: Candidate, verdict: Verdict
    ) -> PrOutcome:
        _maybe_crash("pr")
        s = ctx.settings
        labels = labels_for(plan)
        label = labels[0] if labels else None
        ctx.progress.phase("pr", "draft PR body written" + (f" · {label}" if label else ""))
        ctx.log.emit("pr", "note", {"text": "draft PR body written", "label": label}, usd=0.04)
        ctx.progress.cost(0.04)
        body = evidence_table(
            issue.issue,
            issue.title,
            plan,
            verdict,
            cost_usd=ctx.spent_usd(),
            calls_by_model={s.planner_model: 2, s.implementer_model: 11, s.tools_model: 37},
        )
        title = f"fix: {issue.title} (#{issue.issue})"
        return PrOutcome(body=body, title=title, label=label, url=None)

    return Steps(mode="demo", hunt=hunt, plan=plan, fix=fix, verify=verify, pr=pr)


def run_demo(
    s: Settings,
    log: EventLog,
    *,
    dry_run: bool = False,
    progress: Progress | None = None,
    yes: bool = False,
    json_out: bool = False,
) -> dict[str, Any]:
    """`warden run --demo`: the demo steps through the machine. `--dry-run` stops after plan."""
    return run_pipeline(
        s,
        log,
        demo_steps(),
        start_after=None,
        json_out=json_out,
        yes=yes,
        progress=progress,
        stop_after="plan" if dry_run else None,
    )
