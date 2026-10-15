"""Large-file guards from the Fabric campaign (2026-10-15): Super never called `finish` in 13 of 15
attempts. It re-asked for the 14-line tail of an 813-line test file 40 times after being told it
had it (camp-replay-212-b-200), and re-sent a 926-line test file through write_file until the
8192-token completion limit cut the reply (camp-head-1-210, 7 of 40 turns)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import ScriptedLLM, TddBackend, finish_turn, tool_call, turn
from warden.fix.implementer import REREAD_ESCALATE_AFTER, run_implementer
from warden.fix.tools import LARGE_FILE_BYTES, LARGE_FILE_LINES, ToolDispatcher, too_large
from warden.llm.client import ChatResult
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.schemas import Candidate, Plan

T = "tests/test_grn.py"
SRC = "src/grn.py"
# an 813-line test file, as backend/tests/test_grn_service.py at 43f3851^
BIG_TEST = "".join(f"def test_case_{i}():\n    assert {i} == {i}\n\n" for i in range(271))


@pytest.fixture
def log(tmp_path: Path) -> Iterator[EventLog]:
    log = EventLog(tmp_path / "runs", "t")
    yield log
    log.close()


class Env:
    def __init__(self, log: EventLog) -> None:
        self.log = log
        self.backend = TddBackend({SRC: T})
        self.base = Box.from_ref(self.backend, "warden/fabric-base:latest", log)
        img = self.backend.images[self.base.uuid]
        img.files[f"/app/{T}"] = BIG_TEST.encode()
        img.files[f"/app/{SRC}"] = b"def receive():\n    return 1\n"
        self.plan = Plan(
            issue=200,
            summary="bound GRN over-receipt",
            files_to_touch=[SRC],
            tests_to_add=[T],
            landing_order=[SRC],
            risk_class=["money"],
            citations=[],
            acceptance=["over-receipt is rejected"],
        )
        self.dispatcher = ToolDispatcher(log)

    def run(
        self, script: Sequence[ChatResult], *, max_tool_turns: int = 60
    ) -> tuple[ScriptedLLM, Candidate]:
        llm = ScriptedLLM(script, log=self.log)
        cand = run_implementer(
            llm,
            self.base,
            self.plan,
            self.log,
            dispatcher=self.dispatcher,
            max_tool_turns=max_tool_turns,
            idle_nudge_after=0,
        )
        return llm, cand


def read(path: str = T, **kw: Any) -> ChatResult:
    return turn(tool_call("read_file", path=path, **kw))


def _notes(log: EventLog, name: str) -> list[dict[str, Any]]:
    log.close()
    events = EventLog.read(log.dir.parent, log.run_id)
    return [e.data for e in events if e.kind == "note" and e.data.get("implementer") == name]


def _tool_msgs(llm: ScriptedLLM) -> list[str]:
    assert llm.last_messages is not None
    return [m["content"] for m in llm.last_messages if m["role"] == "tool"]


def test_the_test_file_is_large() -> None:
    assert len(BIG_TEST.splitlines()) == 813


# ------------------------------------------------------------------ re-read escalation


def test_repeated_rereads_escalate_after_two(log: EventLog) -> None:
    env = Env(log)
    tail = {"start_line": 800, "end_line": 813}
    script = [read(**tail), *[read(**tail) for _ in range(4)], finish_turn(done=False, unmet=["x"])]
    llm, cand = env.run(script)
    assert env.dispatcher.calls == 1 and cand.tool_turns == 5
    msgs = _tool_msgs(llm)
    assert msgs[1].startswith("unchanged since turn 1") and msgs[2].startswith("unchanged since")
    assert msgs[3].startswith(f"STOP re-reading {T}: this is request 4")
    assert msgs[4].startswith(f"STOP re-reading {T}: this is request 5")
    assert "replace_in_file" in msgs[3] and "finish" in msgs[3]
    rereads = _notes(log, "reread")
    assert [(n["repeat"], n["escalated"]) for n in rereads] == [
        (1, False),
        (2, False),
        (3, True),
        (4, True),
    ]
    assert REREAD_ESCALATE_AFTER == 2


def test_alternating_tail_ranges_count_as_repeats_of_one_file(log: EventLog) -> None:
    # the b-200 loop alternated 800-813 and 810-813
    env = Env(log)
    script = [
        read(start_line=795),
        read(start_line=800, end_line=813),
        read(start_line=810, end_line=813),
        read(start_line=800, end_line=813),
        finish_turn(done=False, unmet=["x"]),
    ]
    llm, _ = env.run(script)
    assert _tool_msgs(llm)[3].startswith("STOP re-reading")


def test_an_edit_resets_the_repeat_count(log: EventLog) -> None:
    env = Env(log)
    tail = {"start_line": 811, "end_line": 813}
    old = "def test_case_270():\n    assert 270 == 270\n"
    new = old + "\n\ndef test_over_receipt():\n    assert receive() == 0\n"
    script = [
        read(**tail),
        read(**tail),
        read(**tail),
        turn(tool_call("replace_in_file", path=T, old=old, new=new)),
        read(**tail),  # changed: served again
        read(**tail),  # first repeat of the new version: the plain note
        finish_turn(done=False, unmet=["x"]),
    ]
    llm, _ = env.run(script)
    msgs = _tool_msgs(llm)
    assert not msgs[4].startswith(("unchanged", "STOP"))
    assert msgs[5].startswith("unchanged since turn 5")


def test_a_whole_file_reread_of_a_long_file_is_a_repeat_of_page_one(log: EventLog) -> None:
    env = Env(log)
    llm, _ = env.run([read(), read(), finish_turn(done=False, unmet=["x"])])
    assert env.dispatcher.calls == 1
    msgs = _tool_msgs(llm)
    assert "lines 1-200 of 813 shown" in msgs[0]
    assert msgs[1].startswith(f"unchanged since turn 1: {T} lines 1-200")


# ------------------------------------------------------------------ write_file on a large file


def test_too_large_thresholds() -> None:
    assert LARGE_FILE_LINES == 300 and LARGE_FILE_BYTES == 12_000
    assert not too_large(12_000, 300)
    assert too_large(12_001, 10) and too_large(100, 301)


def test_write_file_refuses_to_rewrite_a_large_existing_file(log: EventLog) -> None:
    env = Env(log)
    whole = BIG_TEST + "def test_new():\n    assert 0\n"
    res = env.dispatcher.dispatch(
        env.base, "write_file", {"path": T, "content": whole, "allow_shrink": True}
    )
    assert res.ok is False and res.box is env.base and res.exit_code is None
    assert "813 lines" in res.output and "replace_in_file" in res.output
    assert res.refused == {
        "reason": "large",
        "old_bytes": len(BIG_TEST.encode()),
        "old_lines": 813,
        "new_bytes": len(whole.encode()),
    }
    log.close()
    (ev,) = [e for e in EventLog.read(log.dir.parent, log.run_id) if e.kind == "tool"]
    assert ev.data["refused"]["reason"] == "large" and "813 lines" in ev.data["error"]


def test_write_file_still_creates_a_large_new_file_and_rewrites_a_small_one(
    log: EventLog,
) -> None:
    env = Env(log)
    created = env.dispatcher.dispatch(
        env.base, "write_file", {"path": "tests/test_new.py", "content": BIG_TEST}
    )
    assert created.ok
    small = env.dispatcher.dispatch(
        created.box, "write_file", {"path": SRC, "content": "def receive():\n    return 0\n"}
    )
    assert small.ok


def test_the_implementer_notes_a_large_refusal_and_appends_with_replace(log: EventLog) -> None:
    env = Env(log)
    old = "def test_case_270():\n    assert 270 == 270\n"
    script = [
        turn(tool_call("write_file", path=T, content=BIG_TEST + "def test_over():\n    pass\n")),
        turn(
            tool_call(
                "replace_in_file",
                path=T,
                old=old,
                new=old + "\n\ndef test_over_receipt():\n    assert 0\n",
            )
        ),
        finish_turn(done=False, unmet=["fix"]),
    ]
    llm, cand = env.run(script)
    assert cand.tool_turns == 2
    (n,) = _notes(log, "refused_large")
    assert n["path"] == T and n["old_lines"] == 813
    assert cand.tests_added == [f"{T}::test_over_receipt"]
    assert "already exists and is 813 lines" in _tool_msgs(llm)[0]


def test_the_tool_descriptions_state_the_limits() -> None:
    from warden.fix.tools import TOOL_SCHEMAS

    desc = {t["function"]["name"]: t["function"]["description"] for t in TOOL_SCHEMAS}
    assert "200" in desc["read_file"]
    assert "300 lines" in desc["write_file"] and "replace_in_file" in desc["write_file"]
