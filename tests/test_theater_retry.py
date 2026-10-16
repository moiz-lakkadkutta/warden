"""Theater retry (WARDEN_THEATER_RETRIES): a FAIL only because no new test reproduces the issue on
the unfixed base goes back to the implementer once, from the candidate checkpoint, and verify
reruns.

The replay is run 20261014-131452-cd9b (icalendar#1928): the right one-line fix (to_jcal:
self.items() → self.sorted_items()), a green suite, and four new tests that all built the rule
from "FREQ=DAILY;COUNT=10" — FREQ already first — so all four passed on the unfixed base. Offline:
`IcalBackend` is a FakeBackend whose pytest runs the test functions it finds against the source
in the image; the LLM is scripted; the live fix / verify / retry steps run for real.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import warden.llm.client as client_mod
import warden.pipeline as pipeline
import warden.sandbox.box as box_mod
from tests.fakes import ScriptedLLM, finish_turn, tool_call, turn
from warden.config import Settings
from warden.fix.retry import feedback, merge_tests, retry_budget
from warden.integrate.pr import evidence_table
from warden.llm.client import ChatResult
from warden.llm.cost import CostCapExceededError, CostMeter
from warden.pipeline import IssueRef, live_steps, run_pipeline

# the conftest swaps these module attributes for offline stubs; keep the real ones
from warden.pipeline import _live_fix as live_fix
from warden.pipeline import _live_retry_fix as live_retry_fix
from warden.pipeline import _live_verify as live_verify
from warden.runlog.events import EventLog
from warden.sandbox.fake import FakeBackend, FakeImage
from warden.schemas import Candidate, Plan, Verdict
from warden.verify.verifier import THEATER_PASSED, theater_retryable, with_theater_retry

SRC = "src/icalendar/prop/recur/recur.py"
T = "src/icalendar/tests/prop/recur/test_recur.py"
BASE_REF = "icalendar-base"
BUGGY = "def to_jcal(self):\n    return [self.items()]\n"
FIXED = "def to_jcal(self):\n    return [self.sorted_items()]\n"
FREQ_FIRST = "FREQ=DAILY;COUNT=10"
FREQ_LAST = "COUNT=10;FREQ=DAILY"

# attempt 1: the four cd9b tests, FREQ already first in every input
THEATER_TESTS = "".join(
    f'def {name}():\n    assert to_jcal("{FREQ_FIRST}")[0] == "FREQ"\n\n\n'
    for name in (
        "test_vrecur_to_jcal_freq_first",
        "test_vrecur_to_xcal_freq_first",
        "test_vrecur_to_ical_freq_first_regression",
        "test_vrecur_other_keys_after_freq",
    )
)
REPRO = (
    "def test_to_jcal_puts_freq_first_when_it_is_last_in_the_source():\n"
    f'    assert to_jcal("{FREQ_LAST}")[0] == "FREQ"\n'
)
REPRO_NODE = f"{T}::test_to_jcal_puts_freq_first_when_it_is_last_in_the_source"
_DEF = re.compile(r"^def (test_\w+)\(\):\n((?:    .*\n?)+)", re.MULTILINE)


class IcalBackend(FakeBackend):
    """`use(uuid)` reopens an existing image (Contree does too); `pytest` runs the `def test_*`
    functions in the target files: a test passes unless its input has FREQ last and the image's
    recur.py is still the buggy one. A bare `pytest -q` is the full suite: 21315 passed."""

    def __init__(self) -> None:
        super().__init__()
        self.base = self._new()
        self.base.files[f"/src/{SRC}"] = BUGGY.encode()
        self.pytest_runs: list[tuple[str, str]] = []  # (image uuid, command)

    def use(self, ref: str) -> Any:
        if ref == BASE_REF:
            return self.base
        return self.images[ref] if ref in self.images else super().use(ref)

    def _interpret(self, img: FakeImage, shell: str, cwd: str) -> tuple[str, str, int]:
        plain = shell.startswith("pytest") and " > " not in shell  # keep_tail: super unwraps
        parts = shlex.split(shell) if plain else []
        if not parts:
            return super()._interpret(img, shell, cwd)
        self.pytest_runs.append((img.uuid, shell))
        targets = [p for p in parts[1:] if not p.startswith("-") and p != "no:cacheprovider"]
        if not targets:
            return "===== 21315 passed, 42 skipped in 812.00s =====\n", "", 0
        fixed = b"sorted_items" in img.files.get(f"/src/{SRC}", b"")
        lines: list[str] = []
        for target in targets:
            path, _, only = target.partition("::")
            text = img.files.get(f"{cwd.rstrip('/')}/{path}", b"").decode()
            for name, body in _DEF.findall(text):
                if only and name != only:
                    continue
                ok = fixed or FREQ_LAST not in body
                lines.append(
                    f"PASSED {path}::{name}"
                    if ok
                    else f"FAILED {path}::{name} - AssertionError: assert 'COUNT' == 'FREQ'"
                )
        failed = sum(ln.startswith("FAILED") for ln in lines)
        passed = len(lines) - failed
        summary = ", ".join(
            x for x in (f"{failed} failed" if failed else "", f"{passed} passed") if x
        )
        out = (
            "=========================== short test summary info ============================\n"
            + "".join(f"{ln}\n" for ln in lines)
            + f"{summary} in 0.61s\n"
        )
        return out, "", 1 if failed else 0


class MeteredLLM:
    """ScriptedLLM behind the real CostMeter, built the way pipeline builds `LLM(...)`."""

    def __init__(self, scripted: ScriptedLLM, meter: CostMeter, log: EventLog) -> None:
        self.scripted, self.meter = scripted, meter
        if meter.log is None:
            meter.log = log  # as LLM.__init__: a cap hit lands in events.jsonl

    def chat(self, role: Any, phase: Any, messages: Any, *, tools: Any = None) -> ChatResult:
        res = self.scripted.chat(role, phase, messages, tools=tools)
        self.meter.charge(res.model, res.prompt_tokens, res.completion_tokens, phase)
        return res


# ------------------------------------------------------------------------------- scripts


def attempt1() -> list[ChatResult | BaseException]:
    """cd9b: write the FREQ-first tests, make the fix, run them green, finish."""
    return [
        turn(tool_call("write_file", path=T, content=THEATER_TESTS)),
        turn(tool_call("replace_in_file", path=SRC, old="self.items()", new="self.sorted_items()")),
        turn(tool_call("run_shell", command=f"pytest -q {T}")),
        finish_turn(done=True, tests_added=[T], notes="to_jcal uses sorted_items"),
    ]


def retry_turns() -> list[ChatResult | BaseException]:
    """With the feedback: add a FREQ-last test, see it fail on the unfixed source, pass here."""
    return [
        turn(tool_call("write_file", path=T, content=THEATER_TESTS + REPRO)),
        turn(tool_call("run_tests_unfixed", tests=[REPRO_NODE])),
        turn(tool_call("run_shell", command=f"pytest -q {T}")),
        finish_turn(done=True, tests_added=[T], notes="FREQ-last input reproduces #1928"),
    ]


def _plan(_ctx: Any, ref: IssueRef) -> Plan:
    return Plan(
        issue=ref.issue,
        summary="vRecur.to_jcal must put FREQ first",
        files_to_touch=[SRC],
        tests_to_add=[T],
        landing_order=["test", "fix"],
        risk_class=["none"],
        citations=[],
        acceptance=["to_jcal() lists FREQ first whatever the source order"],
    )


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key in list(os.environ):
        if key.startswith("WARDEN_"):
            monkeypatch.delenv(key)
    return Settings(
        _env_file=None,
        WARDEN_RUNS_DIR=str(tmp_path / "runs"),
        WARDEN_BASE_IMAGE=BASE_REF,
        WARDEN_WORKDIR="/src",
        WARDEN_GATE_PROFILE="generic",
        WARDEN_SHELL_WRAP="",
        WARDEN_SPEC_CHECK="0",
        WARDEN_MAX_TOOL_TURNS="60",
    )


class Live:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> None:
        self.backend = IcalBackend()
        self.llm = ScriptedLLM(script)
        monkeypatch.setattr(box_mod, "ContreeBackend", lambda *a: self.backend)
        monkeypatch.setattr(
            client_mod, "LLM", lambda s, meter, log, **kw: MeteredLLM(self._log(log), meter, log)
        )
        monkeypatch.setattr(pipeline, "_live_fix", live_fix)
        monkeypatch.setattr(pipeline, "_live_verify", live_verify)
        monkeypatch.setattr(pipeline, "_live_retry_fix", live_retry_fix)

    def _log(self, log: EventLog) -> ScriptedLLM:
        self.llm._log = log  # its llm events (with usd) feed the run's spend
        return self.llm

    def run(self, s: Settings, run_id: str = "r1", **kw: Any) -> dict[str, Any]:
        log = EventLog(s.runs_dir, run_id)
        steps = replace(
            live_steps(), hunt=lambda ctx: IssueRef(issue=1928, title="to_jcal FREQ"), plan=_plan
        )
        try:
            return run_pipeline(
                s,
                log,
                steps,
                start_after=kw.pop("start_after", None),
                json_out=False,
                yes=True,
                progress=None,
                stop_after="verify",
                **kw,
            )
        finally:
            log.close()


def _events(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in (run_dir / "events.jsonl").read_text().splitlines() if ln]


def _verdict(path: Path) -> Verdict:
    return Verdict.model_validate_json(path.read_text())


# ---------------------------------------------------------------------- the cd9b replay


def test_cd9b_replay_theater_fail_then_repro_on_retry_passes(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = Live(monkeypatch, attempt1() + retry_turns())
    result = live.run(settings)
    art = settings.runs_dir / "r1" / "artifacts"

    # attempt 1: FAIL, theater only; kept as *.attempt1.*
    first = _verdict(art / "verdict.attempt1.json")
    assert first.decision == "FAIL"
    assert first.reasons[0] == "theater: every new test passes on the unfixed base"
    assert first.theater_tests == {
        f"{T}::{n}": "guard: passes on base" for n in re.findall(r"def (test_\w+)", THEATER_TESTS)
    }
    assert (art / "theater.attempt1.txt").exists() and (art / "gate.attempt1.txt").exists()
    cand1 = Candidate.model_validate_json((art / "candidate.attempt1.json").read_text())

    # the retry continued from the candidate checkpoint, with the verifier's feedback
    retry_call = live.llm.calls[len(attempt1())]
    note = retry_call.messages[1]["content"]
    assert "theater retry 1 of 1" in note and "guard: passes on base" in note
    assert "Keep the fix" in note and "run_tests_unfixed" in note
    assert "4 passed in 0.61s" in note  # the base pytest output, trimmed
    assert "run_tests_unfixed" in {t["function"]["name"] for t in retry_call.tools or []}
    events = _events(settings.runs_dir / "r1")
    start = [e["data"] for e in events if e["data"].get("implementer") == "start"]
    assert len(start) == 2 and start[1]["base"] == cand1.image_uuid
    assert start[1]["max_tool_turns"] == 20 and start[1]["prior_tests"] == [T]
    unfixed = next(e["data"] for e in events if e["data"].get("implementer") == "unfixed_run")
    assert unfixed["tests"] == [REPRO_NODE] and unfixed["exit_code"] == 1
    tool_reply = live.llm.calls[len(attempt1()) + 2].messages[-1]["content"]
    assert tool_reply.startswith("exit 1 on the UNFIXED source") and "FAILED" in tool_reply

    # attempt 2: PASS, the FREQ-last test is the repro; the verdict says it came on retry 1
    final = _verdict(art / "verdict.json")
    assert result["verdict"] == "PASS" and final.decision == "PASS"
    assert final.theater_failed_on_base and final.theater_retry == 1
    assert f"{THEATER_PASSED} — repro found on retry 1" in final.reasons
    assert final.theater_tests[REPRO_NODE] == (
        "repro: fails on base (AssertionError), passes with the fix"
    )
    cand = Candidate.model_validate_json((art / "candidate.json").read_text())
    assert cand.tests_added == [T] and cand.done is True
    assert cand.image_uuid != cand1.image_uuid and cand.tool_turns == cand1.tool_turns + 3
    retries = [e["data"] for e in events if e["data"].get("retry") == "theater"]
    assert [r["attempt"] for r in retries] == [2, 2, 2]  # verify note, fix note, outcome note
    assert retries[-1]["repro"] is True and retries[-1]["decision"] == "PASS"
    table = evidence_table(1928, "t", _plan(None, IssueRef(1928, "t")), final, cost_usd=0.1,
                           calls_by_model={})  # fmt: skip
    assert "FAIL — as required (repro found on retry 1)" in table


def test_retries_zero_keeps_the_theater_fail(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = Live(monkeypatch, attempt1())  # no retry turns: an extra LLM call would fail fast
    s = settings.model_copy(update={"theater_retries": 0})
    result = live.run(s)
    art = s.runs_dir / "r1" / "artifacts"
    final = _verdict(art / "verdict.json")
    assert result["verdict"] == "FAIL"
    assert final.reasons[0] == "theater: every new test passes on the unfixed base"
    assert final.theater_retry == 0 and "theater_retry" not in json.loads(
        (art / "verdict.json").read_text()
    )
    assert not list(art.glob("*.attempt*"))
    assert not [e for e in _events(s.runs_dir / "r1") if e["data"].get("retry")]


def test_retry_that_still_finds_no_repro_is_a_fail_that_says_so(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    still = [
        turn(tool_call("run_tests_unfixed", tests=[T])),
        finish_turn(done=True, tests_added=[T]),
    ]
    live = Live(monkeypatch, attempt1() + still)
    live.run(settings)
    final = _verdict(settings.runs_dir / "r1" / "artifacts" / "verdict.json")
    assert final.decision == "FAIL" and final.theater_retry == 1
    assert final.reasons[0] == (
        "theater: every new test passes on the unfixed base (still, after 1 theater retry)"
    )


def test_cost_cap_during_the_retry_stops_the_run(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pricey = turn(tool_call("run_tests_unfixed", tests=[T]), pt=5_000_000)  # $5 at $1/M
    live = Live(monkeypatch, [*attempt1(), pricey])
    with pytest.raises(CostCapExceededError):
        live.run(settings)
    art = settings.runs_dir / "r1" / "artifacts"
    assert (art / "verdict.attempt1.json").exists()
    assert not (art / "verdict.json").exists() and not (art / "candidate.attempt1.json").exists()
    events = _events(settings.runs_dir / "r1")
    assert events[-1]["kind"] == "cost_cap"


def test_cost_cap_during_the_retry_exits_4(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from warden import ui
    from warden.cli import app

    pricey = turn(tool_call("run_tests_unfixed", tests=[T]), pt=5_000_000)
    Live(monkeypatch, [*attempt1(), pricey])
    monkeypatch.setattr(pipeline, "_live_plan", lambda ctx, ref: _plan(ctx, ref))
    monkeypatch.chdir(tmp_path)
    ui.configure(ui.UIConfig(color=False, animate=False, debug=False))
    env = {
        "NEBIUS_API_KEY": "x",
        "CONTREE_BASE_URL": "http://c",
        "NEBIUS_PROJECT_ID": "p",
        "WARDEN_RUNS_DIR": str(settings.runs_dir),
        "WARDEN_BASE_IMAGE": BASE_REF,
        "WARDEN_WORKDIR": "/src",
        "WARDEN_GATE_PROFILE": "generic",
        "WARDEN_SHELL_WRAP": "",
        "WARDEN_SPEC_CHECK": "0",
        "WARDEN_MAX_TOOL_TURNS": "60",
    }
    res = CliRunner().invoke(app, ["run", "--issue", "1928", "--no-pr"], env=env)
    assert res.exit_code == 4, res.stderr
    assert "reached at Fix; stopping" in res.stderr and "theater retry 1" in res.stderr


def test_resume_after_a_retry_never_retries_again(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the retry's implementer pass, before its verify finished: resume verifies the
    retry candidate (candidate.json) and does not spend a second retry, even though it is still
    a theater FAIL."""
    still = [finish_turn(done=True, tests_added=[T])]  # the retry changes nothing
    live = Live(monkeypatch, attempt1() + still)
    live.run(settings)
    art = settings.runs_dir / "r1" / "artifacts"
    retry_cand = (art / "candidate.json").read_text()
    (art / "verdict.json").unlink()
    live.llm = ScriptedLLM([])  # resume makes no LLM call at all (an extra one fails fast)
    result = live.run(settings, start_after="fix")
    final = _verdict(art / "verdict.json")
    assert result["verdict"] == "FAIL" and final.theater_retry == 1
    assert (art / "candidate.json").read_text() == retry_cand
    assert not (art / "candidate.attempt2.json").exists()


def test_demo_is_unaffected(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from warden.demo import run_demo

    result = run_demo(settings, EventLog(settings.runs_dir, "d"), stop_after="verify")
    assert result["verdict"] == "PASS"
    assert not list((settings.runs_dir / "d" / "artifacts").glob("*.attempt*"))


def test_best_of_n_retries_per_candidate(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each best-of-N candidate gets its own theater retry; its attempt files stay in its own
    artifacts/candidate-<k>/ and the winner's are copied up."""
    live = Live(monkeypatch, attempt1() + retry_turns())  # candidate 2: early stop, no calls
    s = settings.model_copy(update={"candidates": 2})
    result = live.run(s)
    art = s.runs_dir / "r1" / "artifacts"
    assert result["verdict"] == "PASS"
    assert _verdict(art / "candidate-1" / "verdict.attempt1.json").decision == "FAIL"
    assert (art / "verdict.attempt1.json").exists()  # the winner's, copied up
    best = _verdict(art / "best_of_n.json")
    assert best.chosen == 1 and best.theater_retry == 1
    assert [c.status for c in best.candidates] == ["verified", "skipped"]
    cand = Candidate.model_validate_json((art / "candidate.json").read_text())
    assert cand.tests_added == [T] and cand.done is True and cand.tool_turns == 6


def test_a_red_suite_never_retries(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    live = Live(monkeypatch, attempt1())

    def red_suite(img: FakeImage, shell: str, cwd: str) -> tuple[str, str, int]:
        if shell.startswith("pytest -q") and shell.strip() == "pytest -q":
            return "===== 3 failed, 21312 passed in 812.00s =====\n", "", 1
        return IcalBackend._interpret(live.backend, img, shell, cwd)

    monkeypatch.setattr(live.backend, "_interpret", red_suite)
    result = live.run(settings)
    art = settings.runs_dir / "r1" / "artifacts"
    assert result["verdict"] == "FAIL" and not list(art.glob("*.attempt*"))
    assert "full suite not green" in " ".join(_verdict(art / "verdict.json").reasons)


# --------------------------------------------------------------------------------- units


def _v(**kw: Any) -> Verdict:
    base: dict[str, Any] = {
        "decision": "FAIL",
        "theater_failed_on_base": False,
        "theater_tests": {f"{T}::test_a": "guard: passes on base"},
        "suite": {"passed": 21315, "failed": 0},
        "mypy_ok": True,
        "ruff_ok": True,
        "alembic_single_head": True,
        "openapi_drift": False,
        "reasons": ["theater: every new test passes on the unfixed base"],
    }
    return Verdict(**{**base, **kw})


CAND = Candidate(issue=1, image_uuid="c", tool_turns=9, tests_added=[T], cost_usd=0.1, done=True)


@pytest.mark.parametrize(
    ("kw", "cand", "ok"),
    [
        ({}, CAND, True),
        ({"decision": "PASS"}, CAND, False),
        # a non-FAIL whose main-behaviour tests are all guards (E1 checkov-831) retries once
        ({"decision": "PASS_WITH_CONCERNS", "main_guard": [f"{T}::test_a"]}, CAND, True),
        (
            {"decision": "PASS_WITH_CONCERNS", "main_guard": [f"{T}::test_a"]},
            CAND.model_copy(update={"done": False}),
            False,
        ),
        ({"theater_failed_on_base": True}, CAND, False),
        ({"theater_inconclusive": True}, CAND, False),
        ({"suite": {"passed": 10, "failed": 2}}, CAND, False),
        ({"suite": {}}, CAND, False),
        ({"regressions": [f"{T}::test_old"]}, CAND, False),
        ({"spec_unmet": ["FREQ first in xcal"]}, CAND, False),
        ({"mypy_ok": False}, CAND, False),
        ({"inconclusive": True}, CAND, False),
        ({}, CAND.model_copy(update={"done": False}), False),
        ({}, CAND.model_copy(update={"tests_added": []}), False),
        # invalid repro: the new test is red with the fix too, and is the suite's one failure
        (
            {
                "theater_invalid": True,
                "theater_tests": {
                    f"{T}::test_a": "invalid: fails on base (AssertionError), and still fails "
                    "with the fix"
                },
                "suite": {"passed": 21314, "failed": 1},
            },
            CAND,
            True,
        ),
    ],
)
def test_theater_retryable(kw: dict[str, Any], cand: Candidate, ok: bool) -> None:
    assert theater_retryable(cand, _v(**kw)) is ok


def test_with_theater_retry_and_budget() -> None:
    passed = _v(decision="PASS", theater_failed_on_base=True, reasons=[THEATER_PASSED, "x"])
    assert with_theater_retry(passed, 0) is passed
    assert with_theater_retry(passed, 1).reasons == [
        f"{THEATER_PASSED} — repro found on retry 1",
        "x",
    ]
    assert with_theater_retry(_v(), 2).reasons == [
        "theater: every new test passes on the unfixed base (still, after 2 theater retries)"
    ]
    assert [retry_budget(n) for n in (60, 30, 2, 100)] == [20, 10, 1, 20]
    assert merge_tests([T], [f"{T}::test_b", "t2.py"]) == [T, "t2.py"]


def test_feedback_has_reasons_output_diff_and_the_instruction() -> None:
    text = feedback(_v(), attempt=1, retries=1, base_output="x" * 5000 + "TAIL", fix_diff="+fix")
    assert f"{T}::test_a: guard: passes on base" in text
    assert "TAIL" in text and "x" * 3000 not in text  # trimmed to its tail
    assert "+fix" in text and "Keep the fix" in text and "run_tests_unfixed" in text


def test_feedback_for_a_guarded_main_behaviour_asks_for_the_issues_own_scenario() -> None:
    v = _v(
        decision="PASS_WITH_CONCERNS",
        theater_failed_on_base=True,
        main_guard=[f"{T}::test_a"],
        reasons=[THEATER_PASSED],
    )
    text = feedback(v, attempt=1, retries=1, base_output="", fix_diff="+fix")
    assert "MAIN behaviour" in text and f"{T}::test_a" in text
    assert "issue's own scenario" in text and "rejected the candidate" not in text
