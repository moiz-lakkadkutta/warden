"""Auto-finish (hunter replay 20261015-175548-8d5d: the chosen candidate had a service-layer fix
and a valid repro, went green at turn 58 of 60 and never called `finish`; 13 of 15 Fabric campaign
attempts used every turn). A candidate with new tests, a source edit, green tests and clean lint is
finished by Warden, recorded as an `auto_finish` note, and handed to the verifier."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import ScriptedLLM, TddBackend, finish_turn, tool_call, turn
from warden.fix.implementer import AUTO_FINISH_AFTER, FINISH_NUDGE_AFTER, run_implementer
from warden.fix.tools import ToolDispatcher
from warden.llm.client import ChatResult
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.schemas import Candidate, Plan

T = "tests/test_fix.py"
TEST_A = "def test_a():\n    assert fix() == 1\n"
IMPL = "def fix():\n    return 1\n"
LINT = "ruff check ."


@pytest.fixture
def log(tmp_path: Path) -> Iterator[EventLog]:
    log = EventLog(tmp_path / "runs", "t")
    yield log
    log.close()


class LintFixBackend(TddBackend):
    """`fixlint` (the autofix command) makes `ruff check` clean in the child it keeps."""

    def run(self, image: Any, **kw: Any) -> tuple[str, str, int, Any | None, str]:
        out = super().run(image, **kw)
        if kw["shell"] == "fixlint" and out[3] is not None:
            out[3].gate_results["ruff check"] = 0
        return out


def _plan() -> Plan:
    return Plan(
        issue=227,
        summary="fix() must return 1",
        files_to_touch=["src/fix.py"],
        tests_to_add=[T],
        landing_order=["src/fix.py"],
        risk_class=["security"],
        citations=[],
        acceptance=["test_fix passes"],
    )


def _run(
    log: EventLog,
    script: Sequence[ChatResult],
    *,
    max_tool_turns: int = 25,
    lint: str = "",
    autofix: str = "",
    lint_red: bool = False,
    t_passes_on_base: bool = False,
    unfixed: bool = False,
) -> tuple[ScriptedLLM, Candidate, TddBackend]:
    backend = LintFixBackend({"src/fix.py": T})
    base = Box.from_ref(backend, "warden/fabric-base:latest", log)
    img = backend.images[base.uuid]
    img.test_results.update({T: 0 if t_passes_on_base else 1})
    if lint_red:
        img.gate_results["ruff check"] = 1
    llm = ScriptedLLM(script, log=log)
    cand = run_implementer(
        llm,
        base,
        _plan(),
        log,
        dispatcher=ToolDispatcher(log),
        max_tool_turns=max_tool_turns,
        idle_nudge_after=0,
        lint=lint,
        autofix=autofix,
        unfixed=base if unfixed else None,
    )
    return llm, cand, backend


def _notes(log: EventLog, name: str) -> list[dict[str, Any]]:
    log.close()
    events = EventLog.read(log.dir.parent, log.run_id)
    return [e.data for e in events if e.kind == "note" and e.data.get("implementer") == name]


def _user_texts(llm: ScriptedLLM) -> list[str]:
    msgs = llm.last_messages or []
    return [str(m["content"]) for m in msgs if m["role"] == "user"]


def read() -> ChatResult:
    return turn(tool_call("list_files", path="src"))


def _green(*extra: ChatResult) -> list[ChatResult]:
    return [
        turn(tool_call("write_file", path=T, content=TEST_A)),
        turn(tool_call("run_shell", command=f"pytest -q {T}")),  # red
        turn(tool_call("write_file", path="src/fix.py", content=IMPL)),
        turn(tool_call("run_shell", command=f"pytest -q {T}")),  # green: turn 4
        *extra,
    ]


def test_auto_finish_after_a_quiet_green_run(log: EventLog) -> None:
    llm, cand, _ = _run(log, _green(*[read() for _ in range(20)]))
    assert FINISH_NUDGE_AFTER < AUTO_FINISH_AFTER == 4
    assert cand.done is True and cand.tool_turns == 4 + AUTO_FINISH_AFTER
    assert cand.tests_added == [T]
    (n,) = _notes(log, "auto_finish")
    assert n == {"implementer": "auto_finish", "reason": "green", "green_turn": 4, "tool_turns": 8}
    (done,) = _notes(log, "done")
    assert done["auto_finish"] == "green" and done["report"] is None
    assert _notes(log, "budget") == []
    assert len(llm.calls) == 8  # stopped calling the model


def test_the_model_finishing_itself_is_not_an_auto_finish(log: EventLog) -> None:
    _, cand, _ = _run(log, _green(read(), finish_turn()))
    assert cand.done is True and _notes(log, "auto_finish") == []


def test_auto_finish_at_the_cap_even_without_a_quiet_green_run(log: EventLog) -> None:
    # the 8d5d shape: the green run lands just before the cap
    script = [*[read() for _ in range(6)], *_green()]
    _, cand, _ = _run(log, script, max_tool_turns=10)
    assert cand.done is True and cand.tool_turns == 10
    (n,) = _notes(log, "auto_finish")
    assert n["reason"] == "cap"
    assert len(_notes(log, "budget")) == 1


def test_replay_8d5d_green_at_turn_58_of_60(log: EventLog) -> None:
    script = [
        *[read() for _ in range(54)],
        *_green(),  # turns 55-58
        turn(tool_call("run_shell", command="pytest -q tests")),  # 59, the whole file: green
        read(),  # 60
    ]
    _, cand, _ = _run(log, script, max_tool_turns=60)
    assert cand.done is True and cand.tool_turns == 60
    assert _notes(log, "auto_finish")[0]["reason"] == "cap"


def test_no_auto_finish_without_a_source_edit(log: EventLog) -> None:
    script = [
        turn(tool_call("write_file", path=T, content=TEST_A)),
        turn(tool_call("run_shell", command=f"pytest -q {T}")),  # green on base: a guard
        *[read() for _ in range(10)],
    ]
    _, cand, _ = _run(log, script, max_tool_turns=10, t_passes_on_base=True)
    assert cand.done is False and _notes(log, "auto_finish") == []


def test_no_auto_finish_without_new_tests(log: EventLog) -> None:
    script = [
        turn(tool_call("write_file", path="src/fix.py", content=IMPL)),
        turn(tool_call("run_shell", command=f"pytest -q {T}")),
        *[read() for _ in range(10)],
    ]
    _, cand, _ = _run(log, script, max_tool_turns=8)
    assert cand.done is False and _notes(log, "auto_finish") == []


def test_red_new_tests_at_the_cap_stay_blocked(log: EventLog) -> None:
    script = [
        turn(tool_call("write_file", path=T, content=TEST_A)),
        turn(tool_call("write_file", path="src/other.py", content="x = 1\n")),  # not the fix
        *[read() for _ in range(10)],
    ]
    _, cand, _ = _run(log, script, max_tool_turns=6)
    assert cand.done is False and _notes(log, "auto_finish") == []
    (declined,) = _notes(log, "auto_finish_declined")
    assert "exits 1" in declined["why"]


def test_red_lint_declines_and_tells_the_model(log: EventLog) -> None:
    script = _green(*[read() for _ in range(AUTO_FINISH_AFTER)], finish_turn(), read())
    llm, cand, _ = _run(log, script, lint=LINT, lint_red=True)
    assert cand.done is True  # by its own finish, after the note
    assert _notes(log, "auto_finish") == []
    (declined,) = _notes(log, "auto_finish_declined")
    assert declined["green_turn"] == 4 and LINT in declined["why"]
    notes = [t for t in _user_texts(llm) if t.startswith("Warden checked your work")]
    assert len(notes) == 1 and LINT in notes[0]


def test_the_check_runs_once_per_green_run(log: EventLog) -> None:
    script = _green(*[read() for _ in range(12)])
    _, cand, _ = _run(log, script, max_tool_turns=16, lint=LINT, lint_red=True)
    assert cand.done is False
    declined = _notes(log, "auto_finish_declined")
    assert [d["green_turn"] for d in declined] == [4, 4]  # once mid-run, once at the cap


def test_autofix_that_cleans_lint_is_kept(log: EventLog) -> None:
    script = _green(*[read() for _ in range(10)])
    _, cand, backend = _run(log, script, lint=LINT, autofix="fixlint", lint_red=True)
    assert cand.done is True
    assert _notes(log, "auto_finish")[0]["reason"] == "green"
    assert backend.images[cand.image_uuid].gate_results["ruff check"] == 0


def test_a_theater_retry_counts_its_base_fix(log: EventLog) -> None:
    # the retry's base is the candidate: the fix is already there, the model only rewrites tests
    script = [
        turn(tool_call("write_file", path=T, content=TEST_A)),
        turn(tool_call("run_shell", command=f"pytest -q {T}")),
        *[read() for _ in range(10)],
    ]
    _, cand, _ = _run(log, script, t_passes_on_base=True, unfixed=True)
    assert cand.done is True and _notes(log, "auto_finish")[0]["reason"] == "green"
