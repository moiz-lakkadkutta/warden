# ruff: noqa: E501  (GATE_206 copies real tool output, long lines included)
"""Verify retry (WARDEN_VERIFY_RETRIES): a candidate whose repro is proven (fails on the unfixed
base, passes with the fix) but whose suite, mypy / ruff, regression check or spec review is red
goes back to the implementer once, from the candidate checkpoint, with a compact note, and
verify reruns. Fabric #196 / #206 (camp-replay-212-d) were exactly this: valid repro, 2 suite
failures plus mypy / ruff errors, thrown away.

Offline: the icalendar fake backend from the theater-retry tests, plus a `mypy` that is red while
the source carries an untyped `None` assignment. The LLM is scripted; the live fix / verify /
retry steps run for real against the fake sandbox.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import warden.pipeline as pipeline
from tests.fakes import finish_turn, tool_call, turn
from tests.test_theater_retry import (
    FIXED,
    REPRO,
    REPRO_NODE,
    SRC,
    IcalBackend,
    Live,
    T,
    _events,
    _verdict,
)
from tests.test_theater_retry import settings as settings  # noqa: F401  (fixture)
from warden.config import Settings
from warden.eval.report import compute, read_results
from warden.eval.swe_rebench import CSV_COLUMNS, append_row, make_row
from warden.fix.retry import (
    gate_sections,
    lint_errors,
    pytest_failures,
    verify_feedback,
    verify_retry_budget,
)
from warden.integrate.pr import evidence_table
from warden.pipeline import IssueRef
from warden.sandbox.fake import FakeImage
from warden.schemas import Candidate, Verdict
from warden.verify.verifier import (
    THEATER_PASSED,
    theater_retryable,
    verify_fixable,
    verify_retryable,
    with_verify_retry,
)

BAD = "\nCACHE: dict[str, int] = None\n"
MYPY_ERR = (
    f'{SRC}:4: error: Incompatible types in assignment (expression has type "None", '
    'variable has type "dict[str, int]")  [assignment]'
)

# The shape of camp-replay-212-d-206's gate.txt: a replay's alembic log on stderr before the
# pytest summary, mypy errors, ruff's full-format diagnostics.
GATE_206 = """## suite (exit 1, 654.4 s)
$ psql ... && cd backend && pytest -q -p no:cacheprovider
INFO  [alembic.runtime.migration] Running upgrade e5 -> cogs_sale_voucher_type, COGS-on-sale
........................................................................F.F...
=========================== short test summary info ============================
FAILED tests/test_dc_service.py::test_dc_partial_dispatch - AssertionError: assert 409 == 201
FAILED tests/test_sales_api.py::test_issue_dc_twice - app.exceptions.AppValidationError: Dispatch quantity 5 for item 7 would exceed ordered quantity 4
2 failed, 1506 passed in 640.12s (0:10:40)


## mypy (exit 1, 19.3 s)
$ cd backend && mypy .
app/service/sales_service.py:579: error: Generator has incompatible item type "Any | None"; expected "bool"  [misc]
tests/test_dc_service.py:692: error: Generator has incompatible item type "Any | None"; expected "bool"  [misc]
Found 2 errors in 2 files (checked 276 source files)


## ruff_check (exit 1, 1.4 s)
$ cd backend && ruff check .
E501 Line too long (123 > 100)
   --> app/service/sales_service.py:582:101
    |
582 |                     f"Dispatch quantity {dc_line.qty_dispatched} for item"
    |

Found 1 error.


## ruff_format (exit 0, 2.0 s)
$ cd backend && ruff format --check .
276 files already formatted
"""


def _verdict_206(**kw: Any) -> Verdict:
    base: dict[str, Any] = {
        "decision": "FAIL",
        "theater_failed_on_base": True,
        "theater_tests": {
            "backend/tests/test_dc_service.py::test_dc_over_dispatch_blocked": "repro: fails on "
            "base (Failed), passes with the fix"
        },
        "suite": {"failed": 2, "passed": 1506},
        "mypy_ok": False,
        "ruff_ok": False,
        "alembic_single_head": True,
        "openapi_drift": False,
        "reasons": [
            "implementer did not finish (budget or blocked)",
            "full suite not green ({'failed': 2, 'passed': 1506})",
            "mypy red",
            "ruff check / format red",
            THEATER_PASSED,
            "alembic upgrade + single head clean",
            "OpenAPI snapshot drift clean",
            "spec: 3 of 3 stated behaviours implemented",
        ],
    }
    base.update(kw)
    return Verdict(**base)


def _cand(**kw: Any) -> Candidate:
    data: dict[str, Any] = {
        "issue": 206,
        "cost_usd": 0.2,
        "image_uuid": "img",
        "tests_added": ["backend/tests/test_dc_service.py"],
        "tool_turns": 40,
        "done": False,
    }
    data.update(kw)
    return Candidate(**data)


# ------------------------------------------------------------------- when it applies


def test_206_is_verify_retryable_not_theater_retryable() -> None:
    v = _verdict_206()
    assert verify_fixable(v) == ["suite", "mypy", "ruff"]
    assert verify_retryable(_cand(), v)  # "did not finish" rides along: the retry may finish
    assert not theater_retryable(_cand(), v)


@pytest.mark.parametrize(
    ("why", "verdict", "cand"),
    [
        ("theater FAIL: the theater retry's case", _verdict_206(theater_failed_on_base=False), {}),
        ("already a PASS", _verdict_206(decision="PASS"), {}),
        ("no new tests", _verdict_206(), {"tests_added": []}),
        (
            "suite timed out: nothing to feed back",
            _verdict_206(
                suite={},
                mypy_ok=True,
                ruff_ok=True,
                inconclusive=True,
                reasons=["full suite timed out (no summary): inconclusive, not proof"],
            ),
            {},
        ),
        (
            "gate did not run",
            _verdict_206(reasons=["gate did not run", THEATER_PASSED], suite={}),
            {},
        ),
        (
            # SWE profile: mypy / ruff not configured leave their flags False with no red reason
            "only unconfigured checks look red",
            _verdict_206(
                suite={"passed": 3}, reasons=["implementer did not finish", THEATER_PASSED]
            ),
            {},
        ),
    ],
)
def test_not_verify_retryable(why: str, verdict: Verdict, cand: dict[str, Any]) -> None:
    assert not verify_retryable(_cand(**cand), verdict), why


def test_regressions_and_unmet_spec_are_retryable() -> None:
    green = {"suite": {"passed": 9}, "mypy_ok": True, "ruff_ok": True}
    reg = _verdict_206(
        **green, regressions=["t.py::test_old"], reasons=["regression: 1 test", THEATER_PASSED]
    )
    spec = _verdict_206(
        **green, spec_unmet=['"must 422"'], reasons=["spec: 1 of 2", THEATER_PASSED]
    )
    assert verify_fixable(reg) == ["regression"] and verify_retryable(_cand(done=True), reg)
    assert verify_fixable(spec) == ["spec"] and verify_retryable(_cand(done=True), spec)


def test_with_verify_retry_marks_the_verdict_and_never_changes_the_decision() -> None:
    fail = with_verify_retry(_verdict_206(), 1)
    assert fail.decision == "FAIL" and fail.verify_retry == 1
    assert fail.reasons[-1].startswith("verify retry 1: still red")
    ok = with_verify_retry(_verdict_206(decision="PASS", reasons=[THEATER_PASSED]), 1)
    assert ok.decision == "PASS" and ok.reasons[-1].startswith("verify retry 1: green")
    assert "verify_retry" not in json.loads(_verdict_206().model_dump_json())


# ---------------------------------------------------------------------- the feedback


def test_gate_txt_parsing_on_the_206_shape() -> None:
    sections = gate_sections(GATE_206)
    assert {k: v[0] for k, v in sections.items()} == {
        "suite": 1,
        "mypy": 1,
        "ruff_check": 1,
        "ruff_format": 0,
    }
    fails = pytest_failures(sections["suite"][1])
    assert fails[0] == "tests/test_dc_service.py::test_dc_partial_dispatch — AssertionError: " \
        "assert 409 == 201"  # fmt: skip
    assert fails[1].startswith("tests/test_sales_api.py::test_issue_dc_twice — app.exceptions")
    assert lint_errors("mypy", sections["mypy"][1]) == [
        'app/service/sales_service.py:579: Generator has incompatible item type "Any | None"; '
        'expected "bool" [misc]',
        'tests/test_dc_service.py:692: Generator has incompatible item type "Any | None"; '
        'expected "bool" [misc]',
    ]
    assert lint_errors("ruff_check", sections["ruff_check"][1]) == [
        "app/service/sales_service.py:582:101: E501 Line too long (123 > 100)"
    ]
    concise = "a.py:3:1: F401 `os` imported but unused\n"
    assert lint_errors("ruff_check", concise) == ["a.py:3:1: F401 `os` imported but unused"]
    assert lint_errors("ruff_format", "Would reformat: a.py\n") == [
        "a.py: not formatted (ruff format)"
    ]


def test_feedback_note_is_compact_and_actionable() -> None:
    note = verify_feedback(
        _verdict_206(spec_unmet=['"a cancelled SO cannot be dispatched"']),
        attempt=1,
        retries=1,
        gate_text=GATE_206,
        fix_diff="+x = 1\n",
        lint="cd backend && ruff check .",
        turns=8,
    )
    assert note.startswith("## Verifier feedback — verify retry 1 of 1")
    assert "only for: suite, mypy, ruff, spec" in note and "8 tool turns" in note
    assert "tests/test_dc_service.py::test_dc_partial_dispatch — AssertionError" in note
    assert "app/service/sales_service.py:579:" in note
    assert "app/service/sales_service.py:582:101: E501" in note
    assert "a cancelled SO cannot be dispatched" in note
    assert "alembic" not in note  # the replay's migration log is noise, never fed back
    assert "Do not delete, skip or xfail" in note and "type: ignore" in note
    assert "`cd backend && ruff check .`" in note
    assert len(note) < 3000


def test_feedback_lists_regressions_with_their_error() -> None:
    v = _verdict_206(regressions=["t/test_a.py::test_one", "t/test_a.py::test_two"])
    reg = (
        "$ pytest -rA\n\n# base\nPASSED t/test_a.py::test_one\n\n# candidate\n"
        "FAILED t/test_a.py::test_one - KeyError: 'id'\nPASSED t/test_b.py::test_x\n"
    )
    note = verify_feedback(v, attempt=1, retries=1, gate_text="", regression_text=reg)
    assert "- t/test_a.py::test_one — KeyError: 'id'" in note
    assert "- t/test_a.py::test_two" in note  # not in the tail: listed by id


def test_feedback_caps_long_lists() -> None:
    gate = "## suite (exit 1, 1.0 s)\n$ pytest\n" + "".join(
        f"FAILED t.py::test_{i} - AssertionError\n" for i in range(30)
    )
    note = verify_feedback(_verdict_206(), attempt=1, retries=1, gate_text=gate)
    assert "t.py::test_9 " in note and "t.py::test_10 " not in note and "… and 20 more" in note


def test_verify_retry_budget() -> None:
    assert verify_retry_budget(60) == 8 and verify_retry_budget(40) == 8
    assert verify_retry_budget(12) == 4 and verify_retry_budget(1) == 1


# ------------------------------------------------------------------ the live loop


class TypedBackend(IcalBackend):
    """`mypy .` is red while recur.py holds the untyped `CACHE = None` assignment."""

    def _interpret(self, img: FakeImage, shell: str, cwd: str) -> tuple[str, str, int]:
        if shell.startswith("mypy"):
            if b"= None" in img.files.get(f"/src/{SRC}", b""):
                return f"{MYPY_ERR}\nFound 1 error in 1 file\n", "", 1
            return "Success: no issues found in 3 source files\n", "", 0
        return super()._interpret(img, shell, cwd)


class TypedLive(Live):
    def __init__(self, monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> None:
        super().__init__(monkeypatch, script)
        self.backend = TypedBackend()
        import warden.sandbox.box as box_mod

        monkeypatch.setattr(box_mod, "ContreeBackend", lambda *a: self.backend)
        monkeypatch.setattr(pipeline, "_live_verify_fix", pipeline_live_verify_fix)


pipeline_live_verify_fix = pipeline._live_verify_fix


def attempt1() -> list[Any]:
    """A real repro and the fix, plus a module-level `CACHE = None` mypy rejects."""
    return [
        turn(tool_call("write_file", path=T, content=REPRO)),
        turn(tool_call("write_file", path=SRC, content=FIXED + BAD)),
        turn(tool_call("run_shell", command=f"pytest -q {T}")),
        finish_turn(done=True, tests_added=[T], notes="to_jcal uses sorted_items"),
    ]


def fix_turns() -> list[Any]:
    """With the note: drop the bad assignment at the file:line given, re-run, finish."""
    return [
        turn(tool_call("replace_in_file", path=SRC, old=BAD, new="\n")),
        turn(tool_call("run_shell", command=f"pytest -q {REPRO_NODE}")),
        finish_turn(done=True, tests_added=[T], notes="removed the untyped CACHE"),
    ]


def still_red_turns() -> list[Any]:
    return [
        turn(tool_call("run_shell", command=f"pytest -q {REPRO_NODE}")),
        finish_turn(done=True, tests_added=[T]),
    ]


def test_valid_repro_with_mypy_red_is_fixed_on_the_verify_retry(
    settings: Settings,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = TypedLive(monkeypatch, attempt1() + fix_turns())
    result = live.run(settings)
    art = settings.runs_dir / "r1" / "artifacts"

    first = _verdict(art / "verdict.attempt1.json")
    assert first.decision == "FAIL" and first.theater_failed_on_base and not first.mypy_ok
    assert "mypy red" in first.reasons
    assert (art / "gate.attempt1.txt").exists()
    cand1 = Candidate.model_validate_json((art / "candidate.attempt1.json").read_text())
    assert json.loads((art / "retries.json").read_text()) == ["verify"]

    note = live.llm.calls[len(attempt1())].messages[1]["content"]
    assert "verify retry 1 of 1" in note and "only for: mypy" in note
    assert f"{SRC}:4: Incompatible types in assignment" in note
    events = _events(settings.runs_dir / "r1")
    start = [e["data"] for e in events if e["data"].get("implementer") == "start"]
    assert len(start) == 2 and start[1]["base"] == cand1.image_uuid
    assert start[1]["max_tool_turns"] == 8

    final = _verdict(art / "verdict.json")
    assert result["verdict"] == "PASS" and final.decision == "PASS"
    assert final.verify_retry == 1 and final.theater_retry == 0 and final.mypy_ok
    assert final.reasons[-1].startswith("verify retry 1: green")
    stages = [e["data"]["verify_retry"] for e in events if "verify_retry" in e["data"]]
    assert stages == ["sent", "start", "outcome"]
    outcome = next(e["data"] for e in events if e["data"].get("verify_retry") == "outcome")
    assert outcome["decision"] == "PASS" and outcome["repro"] is True
    sent = next(e["data"] for e in events if e["data"].get("verify_retry") == "sent")
    assert sent["red"] == ["mypy"]
    table = evidence_table(1928, "t", _plan(), final, cost_usd=0.1, calls_by_model={})
    assert "verify retry 1" in table


def test_verify_retry_happens_once_and_still_red_stays_fail(
    settings: Settings,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = TypedLive(monkeypatch, attempt1() + still_red_turns())  # a 2nd retry would run dry
    live.run(settings)
    final = _verdict(settings.runs_dir / "r1" / "artifacts" / "verdict.json")
    assert final.decision == "FAIL" and final.verify_retry == 1 and "mypy red" in final.reasons
    assert final.reasons[-1].startswith("verify retry 1: still red")


def test_verify_retries_zero_keeps_the_fail(
    settings: Settings,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = TypedLive(monkeypatch, attempt1())
    s = settings.model_copy(update={"verify_retries": 0})
    result = live.run(s)
    art = s.runs_dir / "r1" / "artifacts"
    assert result["verdict"] == "FAIL" and not list(art.glob("*.attempt*"))
    assert "verify_retry" not in json.loads((art / "verdict.json").read_text())


def test_verify_retry_skipped_near_the_cost_cap(
    settings: Settings,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pricey = attempt1()
    pricey[0] = turn(tool_call("write_file", path=T, content=REPRO), pt=2_500_000)  # $2.50 of $3
    live = TypedLive(monkeypatch, pricey)
    result = live.run(settings)
    assert result["verdict"] == "FAIL"
    events = _events(settings.runs_dir / "r1")
    skipped = [e["data"] for e in events if e["data"].get("verify_retry") == "skipped"]
    assert len(skipped) == 1 and skipped[0]["left_usd"] < 0.6


def _plan() -> Any:
    from tests.test_theater_retry import _plan as plan

    return plan(None, IssueRef(1928, "t"))


# ------------------------------------------------------------------------ eval rows


def test_csv_has_a_verify_retry_column_and_old_csvs_upgrade(tmp_path: Path) -> None:
    from warden.eval.swe_rebench import Task

    assert CSV_COLUMNS[-1] == "verify_retry"
    old = tmp_path / "results.csv"
    prev = CSV_COLUMNS[:-1]
    old.write_text(",".join(prev) + "\n" + "t0,1,PASS,0,0,0,0,10,0.1000,60,,1,1,\n")
    task = Task.model_validate(
        {
            "instance_id": "t1",
            "repo": "o/r",
            "base_commit": "abc",
            "problem_statement": "p",
            "test_patch": "",
            "fail_to_pass": [],
            "pass_to_pass": [],
            "image": "img",
            "test_cmd": "pytest",
        }
    )
    row = make_row(
        task,
        verdict="PASS",
        tests_added=1,
        theater_failed_on_base=True,
        theater_inconclusive=False,
        tool_turns=12,
        resolved=True,
        usd=0.2,
        seconds=100,
        verify_retry="1:PASS;2:FAIL",
    )
    append_row(old, row)
    lines = old.read_text().splitlines()
    assert lines[0].endswith(",verify_retry") and lines[1].endswith(",")
    assert lines[2].endswith(",1:PASS;2:FAIL")
    m = compute(read_results([old]))
    assert m.verify_retries == 2 and m.verify_retry_pass == 1


def test_report_compare_table_puts_the_baseline_first(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from warden.cli import app

    header = ",".join(CSV_COLUMNS) + "\n"
    e1 = tmp_path / "e1.csv"
    e1.write_text(
        header + "a,1,PASS,0,0,0,0,10,0.5000,60,,3,1,1,\nb,0,PASS,1,0,0,0,10,0.5,90,,3,1,,\n"
    )
    e2 = tmp_path / "e2.csv"
    e2.write_text(
        header + "a,1,PASS,0,0,0,0,10,0.2,60,,3,1,1,1:PASS\nb,0,FAIL,0,0,0,0,9,0.3,30,,3,1,,\n"
    )
    res = CliRunner().invoke(
        app, ["report", str(e2), "--md", "--label", "E2", "--baseline", str(e1),
              "--baseline-label", "E1"],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    lines = res.stdout.splitlines()
    assert lines[0] == "| metric | E1 | E2 |"
    assert (
        "| Verifier PASS precision (PASS the hidden tests accept) | 1 of 2 PASS (50%) | 1 of 1 PASS (100%) |"
        in lines
    )
    assert any(
        ln.startswith("| Verify retries") and ln.endswith("| — | 1 candidates, 1 then PASS |")
        for ln in lines
    )
