"""Console page: branch tree, timeline, evidence, header pill — all replayed from events.jsonl."""

import html
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from warden.runlog.events import EventLog
from warden.runlog.render import (
    build_tree,
    parse_evidence,
    phase_durations_ms,
    render_html,
    render_run,
    verdict_label,
)
from warden.sandbox.box import Box
from warden.sandbox.fake import FakeBackend
from warden.schemas import RunEvent
from warden.verify.theater import theater_check

COST_ROW = (
    "| Cost | $1.42 · Nemotron-3-Ultra-550b-a55b 2 · nemotron-3-super-120b-a12b 11 · "
    "Nemotron-3_5-Lightning 37 |"
)
PR_BODY = f"""## Warden evidence — #201 double-posted voucher on concurrent invoice finalize

**Verdict: PASS**

| Check | Result |
|---|---|
| New tests on unfixed base | FAIL — as required |
| Full suite on candidate | 214 passed, 0 failed |
| Race probe | 8 threads → 1 effect(s) (was 3) |
| Gate | mypy clean · ruff clean · 1 alembic head · no openapi drift |
| Not verified | frontend, e-invoice path |
| Risk class | money, concurrency → **PENDING-HUMAN** |
{COST_ROW}

**Plan citations**
- [with_for_update() emits SELECT … FOR UPDATE](https://example.invalid/with_for_update)
"""


def _demo_events(
    tmp_path: Path, *, base_fails: bool = True, suite_ok: bool = True
) -> list[RunEvent]:
    """Same sandbox choreography as `warden run --demo`, without importing demo.py."""
    log = EventLog(tmp_path / "runs", "t")
    b = FakeBackend()
    base = Box.from_ref(b, "warden/fabric-base:latest", log, phase="fix")
    new_test = {"backend/tests/test_finalize_race.py": b"def test_x(): ...\n"}
    b.images[base.uuid].test_results["backend/tests/test_finalize_race.py"] = 1 if base_fails else 0
    candidate = base.copy_in(new_test).copy_in({"backend/app/service/invoice.py": b"# fix\n"})
    b.images[candidate.uuid].test_results["backend/tests/test_finalize_race.py"] = 0
    b.images[candidate.uuid].test_results["backend/tests"] = 0 if suite_ok else 1
    base.phase = "verify"
    candidate.phase = "verify"
    theater_check(base, new_test)
    candidate.run("pytest -q backend/tests", keep=False)
    log.emit("pr", "note", {"text": "draft PR body written", "label": "PENDING-HUMAN"})
    log.close()
    return EventLog.read(tmp_path / "runs", "t")


@pytest.fixture
def demo_events(tmp_path: Path) -> list[RunEvent]:
    return _demo_events(tmp_path)


def _ev(phase: str, kind: str, data: dict[str, Any], **kw: Any) -> RunEvent:
    return RunEvent(phase=phase, kind=kind, data=data, **kw)  # type: ignore[arg-type]


# --- branch tree -----------------------------------------------------------------------------


def test_build_tree_from_demo_shape(demo_events: list[RunEvent]) -> None:
    tree = build_tree(demo_events)
    assert (tree.base, tree.candidate, tree.theater) == ("img-0001", "img-0003", "img-0004")
    assert len(tree.edges) == 3
    assert len(tree.runs) == 2
    assert tree.children("img-0001") == ["img-0002", "img-0004"]
    probe = tree.test_run("img-0004")
    assert probe is not None and probe.exit_code == 1
    suite = tree.test_run("img-0003")
    assert suite is not None and suite.exit_code == 0


def test_build_tree_ignores_keep_false_and_empty_to() -> None:
    events = [
        _ev("fix", "sandbox", {"shell": "true", "exit_code": 0, "from": "a", "to": "b"}, ms=1),
        _ev("fix", "sandbox", {"shell": "pytest -q", "exit_code": 0, "from": "b", "to": ""}, ms=1),
        _ev("fix", "sandbox", {"shell": "cat x", "exit_code": 0, "from": "b", "to": "b"}, ms=1),
    ]
    tree = build_tree(events)
    assert tree.base == "a"
    assert tree.candidate == "b"
    assert [e.child for e in tree.edges] == ["b"]
    assert [r.node for r in tree.runs] == ["b", "b"]
    assert tree.theater is None


def test_candidate_is_last_fix_edge_even_if_suite_fails(tmp_path: Path) -> None:
    events = _demo_events(tmp_path, suite_ok=False)
    tree = build_tree(events)
    assert tree.candidate == "img-0003"
    suite = tree.test_run("img-0003")
    assert suite is not None and suite.exit_code == 1
    page = render_html("t", events, None, None)
    assert 'class="edge fail"' in page
    assert "failed (exit 1)" in page


def test_theater_pass_is_flagged_as_theater(tmp_path: Path) -> None:
    events = _demo_events(tmp_path, base_fails=False)
    page = render_html("t", events, None, None)
    assert "tests are theater" in page
    assert "as required" not in page


def test_theater_fail_reads_as_required(demo_events: list[RunEvent]) -> None:
    page = render_html("t", demo_events, None, PR_BODY)
    assert "failed on unfixed base — as required" in page
    assert "214 passed, 0 failed" in page
    assert 'class="edge ok"' in page


def test_build_tree_empty_events() -> None:
    tree = build_tree([])
    assert (tree.base, tree.candidate, tree.theater) == (None, None, None)
    assert tree.edges == () and tree.runs == ()
    assert tree.children("x") == [] and tree.test_run("x") is None
    page = render_html("empty", [], None, None)
    assert 'id="tree"' in page
    assert "not run" in page


# --- timeline -------------------------------------------------------------------------------


def test_timeline_scaled_by_duration() -> None:
    t0 = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    events = [
        _ev("fix", "note", {"text": "a"}, ts=t0),
        _ev("fix", "note", {"text": "b"}, ts=t0 + timedelta(seconds=3)),
        _ev("verify", "note", {"text": "c"}, ts=t0 + timedelta(seconds=3)),
    ]
    durs = phase_durations_ms(events)
    assert durs["fix"] == 3000 and durs["verify"] == 0 and durs["hunt"] == 0
    page = render_html("t", events, None, None)
    fix_bar = page.index('href="#phase-fix"')
    verify_bar = page.index('href="#phase-verify"')
    assert 'style="flex-grow:3001"' in page[fix_bar : fix_bar + 200]
    assert 'style="flex-grow:1"' in page[verify_bar : verify_bar + 200]


# --- evidence -------------------------------------------------------------------------------


def test_parse_evidence_rows() -> None:
    verdict, rows = parse_evidence(PR_BODY)
    assert verdict == "PASS"
    assert len(rows) == 7
    row = dict(rows)
    assert row["Full suite on candidate"] == "214 passed, 0 failed"
    assert row["New tests on unfixed base"] == "FAIL — as required"
    assert rows[0][0] == "New tests on unfixed base"


def test_parse_evidence_handles_missing_table() -> None:
    assert parse_evidence("") == (None, [])
    assert parse_evidence("**Verdict: FAIL**\n\nno table") == ("FAIL", [])


def test_evidence_table_matches_pr_body(demo_events: list[RunEvent]) -> None:
    page = render_html("t", demo_events, None, PR_BODY)
    start = page.index('id="evidence"')
    section = page[start:]
    assert "<table" in section
    for check, result in parse_evidence(PR_BODY)[1]:
        assert html.escape(check) in section
        assert html.escape(result) in section


# --- header -----------------------------------------------------------------------------------


def test_header_pill_precedence() -> None:
    result = {"verdict": "PASS", "cost_usd": 1.0}
    pending = [_ev("pr", "note", {"text": "x", "label": "PENDING-HUMAN"})]
    assert verdict_label([], result, []) == "PASS"
    assert verdict_label([], {"verdict": "PASS_WITH_CONCERNS"}, []) == "PASS WITH CONCERNS"
    assert verdict_label(pending, result, []) == "PENDING-HUMAN"
    assert verdict_label([], result, [("Risk class", "money → **PENDING-HUMAN**")]) == (
        "PENDING-HUMAN"
    )
    capped = [*pending, _ev("fix", "cost_cap", {"spent_usd": 3.1, "cap_usd": 3.0})]
    assert verdict_label(capped, result, []) == "COST CAP"
    assert verdict_label([], None, []) == "RUNNING"
    assert verdict_label([], {"verdict": None}, []) == "RUNNING"
    page = render_html("t", pending, result, None)
    assert 'data-verdict="PENDING-HUMAN"' in page


def test_header_cost_time_and_chips(demo_events: list[RunEvent]) -> None:
    page = render_html("t", demo_events, {"verdict": "PASS", "cost_usd": 1.42}, PR_BODY)
    header = page[page.index('id="header"') : page.index('id="timeline"')]
    assert "$1.42" in header
    for name in (
        "Nemotron-3-Ultra-550b-a55b",
        "nemotron-3-super-120b-a12b",
        "Nemotron-3_5-Lightning",
    ):
        assert name in header
    llm = [
        _ev(
            "plan",
            "llm",
            {"model": "nvidia/Nemotron-3-Ultra-550b-a55b", "role": "planner"},
            usd=0.5,
        ),
        _ev("fix", "llm", {"model": "nvidia/nemotron-3-super-120b-a12b", "role": "impl"}, usd=0.25),
    ]
    page = render_html("t", llm, None, None)
    header = page[page.index('id="header"') : page.index('id="timeline"')]
    assert "$0.75" in header
    assert header.count("nvidia/Nemotron-3-Ultra-550b-a55b") == 1
    assert "nvidia/nemotron-3-super-120b-a12b" in header


# --- footer / css / end to end -------------------------------------------------------------


def test_footer_has_resume_and_events_link(demo_events: list[RunEvent]) -> None:
    page = render_html("20260914-1200-ab", demo_events, None, None)
    footer = page[page.index('id="footer"') :]
    assert "warden resume 20260914-1200-ab" in footer
    assert 'href="events.jsonl"' in footer


def test_tree_stacks_on_phone(demo_events: list[RunEvent]) -> None:
    page = render_html("t", demo_events, None, None)
    assert "@media (max-width:600px){#tree{flex-direction:column}}" in page


def test_render_run_writes_index_html(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    log = EventLog(runs, "r1")
    log.note("hunt", "count <3 vouchers & 1 <b>bold</b>")
    log.artifact("pr_body.md", PR_BODY)
    log.result(verdict="PASS", cost_usd=1.42)
    log.close()
    out = render_run(runs, "r1")
    assert out == runs / "r1" / "index.html"
    page = out.read_text(encoding="utf-8")
    assert "r1" in page
    assert "&lt;3 vouchers &amp; 1 &lt;b&gt;bold&lt;/b&gt;" in page
    assert "<b>bold</b>" not in page
    assert "<script" not in page and "https://" not in page  # single file, no external assets
