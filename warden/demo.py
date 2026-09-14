"""`warden run --demo`: the whole loop on the fake sandbox, no credentials.

It exists so a judge can see the five phases, the theater check and the evidence table in ten
seconds. The plan and verdict are fixtures; the sandbox operations are real calls to Box.
"""

from __future__ import annotations

from typing import Any

from warden.config import Settings
from warden.integrate.pr import evidence_table
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.sandbox.fake import FakeBackend
from warden.schemas import Citation, Plan, RaceResult, Verdict
from warden.verify.theater import theater_check

DEMO_ISSUE = 201
DEMO_TITLE = "double-posted voucher on concurrent invoice finalize"


def run_demo(s: Settings, log: EventLog, *, dry_run: bool = False) -> dict[str, Any]:
    backend = FakeBackend()

    log.note("hunt", "8 parallel POST /invoices/{id}/finalize with distinct Idempotency-Key")
    log.note("hunt", "psql: SELECT count(*) FROM voucher WHERE reference_id=$INV → 3 (expected 1)")
    log.note("hunt", f"filed #{DEMO_ISSUE} [P0] {DEMO_TITLE}")

    plan = Plan(
        issue=DEMO_ISSUE,
        summary="Lock the invoice row (SELECT … FOR UPDATE) before the status check in finalize().",
        files_to_touch=["backend/app/service/invoice.py"],
        tests_to_add=["backend/tests/test_finalize_race.py"],
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
    log.emit("plan", "note", {"text": plan.summary, "citations": [c.url for c in plan.citations]})
    if dry_run:
        return {
            "issue": DEMO_ISSUE,
            "verdict": None,
            "cost_usd": 0.0,
            "evidence": plan.model_dump_json(indent=2),
        }

    base = Box.from_ref(backend, s.base_image, log, phase="fix")
    # On the unfixed base the new test fails; on the candidate it passes.
    base_img = backend.images[base.uuid]
    base_img.test_results["backend/tests/test_finalize_race.py"] = 1

    new_test = {"backend/tests/test_finalize_race.py": b"def test_finalize_is_serialised(): ...\n"}
    candidate = base.copy_in(new_test)
    candidate = candidate.copy_in(
        {"backend/app/service/invoice.py": b"# with_for_update() added\n"}
    )
    backend.images[candidate.uuid].test_results["backend/tests/test_finalize_race.py"] = 0
    log.note("fix", f"candidate checkpoint {candidate.uuid} after 7 tool turns")

    base.phase = "verify"
    candidate.phase = "verify"
    theater = theater_check(base, new_test)
    suite = candidate.run("pytest -q backend/tests", keep=False)
    log.note("verify", f"theater: {theater.label}")

    verdict = Verdict(
        decision="PASS",
        theater_failed_on_base=theater.failed_on_base,
        suite={"passed": 214 if suite.ok else 213, "failed": 0 if suite.ok else 1},
        mypy_ok=True,
        ruff_ok=True,
        alembic_single_head=True,
        openapi_drift=False,
        race=RaceResult(threads=8, effects_on_base=3, effects_on_candidate=1),
        not_verified=["frontend", "e-invoice path"],
        reasons=["new test fails on base and passes on candidate", "race probe returns 1 effect"],
    )
    evidence = evidence_table(
        DEMO_ISSUE,
        DEMO_TITLE,
        plan,
        verdict,
        cost_usd=1.42,
        calls_by_model={s.planner_model: 2, s.implementer_model: 11, s.tools_model: 37},
    )
    log.artifact("pr_body.md", evidence)
    log.emit(
        "pr",
        "note",
        {"text": "draft PR body written", "label": "PENDING-HUMAN" if plan.needs_human else None},
    )
    return {
        "issue": DEMO_ISSUE,
        "verdict": verdict.decision,
        "cost_usd": 1.42,
        "evidence": evidence,
    }
