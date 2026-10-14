"""Read-loop guards and tolerant arguments in the B3 implementer loop, replayed offline.

The scripts mirror three live runs on collective/icalendar#1928 (runs/20261014-*): Super burning
turns on rejected `end_line` arguments (122844-b9ee), re-reading the same files for 26 of 40
turns without editing (123340-85cd), and needing 52 turns to finish (123938-6f4c).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import ScriptedLLM, TddBackend, finish_turn, tool_call, turn
from warden.config import Settings
from warden.fix.implementer import ELIDE_MIN, KEEP_RECENT, ReadMemory, run_implementer
from warden.fix.tools import ToolDispatcher
from warden.llm.client import ChatResult
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.schemas import Candidate, Plan

T = "tests/test_fix.py"
SRC = "src/recur.py"
LINES = [f"line {i:03d}\n" for i in range(1, 301)]  # 300 short lines: ~2.7 KB, > ELIDE_MIN


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
        image = self.backend.images[self.base.uuid]
        image.test_results[T] = 1
        image.files[f"/app/{SRC}"] = "".join(LINES).encode()
        image.files["/app/src/other.py"] = b"x = 1\n"
        self.plan = Plan(
            issue=1928,
            summary="FREQ first in to_jcal",
            files_to_touch=[SRC],
            tests_to_add=[T],
            landing_order=[SRC],
            risk_class=["none"],
            citations=[],
            acceptance=["FREQ is first"],
        )
        self.dispatcher = ToolDispatcher(log)

    def run(
        self, script: Sequence[ChatResult], *, max_tool_turns: int = 60, **kw: Any
    ) -> tuple[ScriptedLLM, Candidate]:
        llm = ScriptedLLM(script, log=self.log)
        cand = run_implementer(
            llm,
            self.base,
            self.plan,
            self.log,
            dispatcher=self.dispatcher,
            max_tool_turns=max_tool_turns,
            **kw,
        )
        return llm, cand


def read(path: str = SRC, **kw: Any) -> ChatResult:
    return turn(tool_call("read_file", path=path, **kw))


def _notes(log: EventLog, name: str) -> list[dict[str, Any]]:
    log.close()
    events = EventLog.read(log.dir.parent, log.run_id)
    return [e.data for e in events if e.kind == "note" and e.data.get("implementer") == name]


def _tool_msgs(llm: ScriptedLLM) -> list[str]:
    assert llm.last_messages is not None
    return [m["content"] for m in llm.last_messages if m["role"] == "tool"]


# ------------------------------------------------------------------ read cache


def test_rereading_an_unchanged_range_is_answered_without_the_content(log: EventLog) -> None:
    env = Env(log)
    script = [
        read(start_line=1, end_line=100),
        read(start_line=100, end_line=200),
        read(start_line=50, end_line=150),  # spans both earlier reads: covered by their union
        read(start_line=120, end_line="140"),  # string-typed end_line, as Super sends it
        read(start_line=190, end_line=260),  # 201-260 never shown: dispatched
        finish_turn(done=False, unmet=["stuck"]),
    ]
    llm, cand = env.run(script, idle_nudge_after=0)
    assert env.dispatcher.calls == 3
    assert cand.tool_turns == 5  # a cached answer still costs the model a turn
    msgs = _tool_msgs(llm)
    assert msgs[0].startswith("line 001\n") and msgs[1].startswith("line 100\n")
    assert msgs[2].startswith("unchanged since turn 2: src/recur.py lines 50-150")
    assert "You already have it" in msgs[2] and "line 0" not in msgs[2]
    assert msgs[3].startswith("unchanged since turn 2: src/recur.py lines 120-140")
    assert msgs[4].startswith("line 190\n")
    rereads = _notes(log, "reread")
    assert [(n["first"], n["last"], n["since_turn"]) for n in rereads] == [
        (50, 150, 2),
        (120, 140, 2),
    ]


def test_whole_file_reread_after_a_page_is_cached_only_when_fully_seen(log: EventLog) -> None:
    env = Env(log)
    script = [read(), read(), read(start_line=7), finish_turn()]
    llm, _ = env.run(script, idle_nudge_after=0)
    assert env.dispatcher.calls == 1
    msgs = _tool_msgs(llm)
    assert msgs[1].startswith("unchanged since turn 1: src/recur.py lines 1-300")
    assert msgs[2].startswith("unchanged since turn 1: src/recur.py lines 7-300")


def test_a_write_to_the_file_invalidates_its_reads(log: EventLog) -> None:
    env = Env(log)
    new = "".join(LINES[:10])
    script = [
        read(),
        read("src/other.py"),
        turn(tool_call("write_file", path=SRC, content=new)),
        read(),  # changed: must be served, and it is the new content
        read("src/other.py"),  # untouched by that write: still cached
        finish_turn(),
    ]
    llm, _ = env.run(script, idle_nudge_after=0)
    assert env.dispatcher.calls == 4
    msgs = _tool_msgs(llm)
    assert msgs[3] == new
    assert msgs[4].startswith("unchanged since turn 2: src/other.py")


def test_absolute_paths_share_the_cache_with_relative_ones(log: EventLog) -> None:
    env = Env(log)
    llm, _ = env.run([read(), read(f"/app/{SRC}"), finish_turn()], idle_nudge_after=0)
    assert env.dispatcher.calls == 1
    assert _tool_msgs(llm)[1].startswith("unchanged since turn 1")


def test_run_shell_invalidates_every_read(log: EventLog) -> None:
    env = Env(log)
    script = [
        read("src/other.py"),
        turn(tool_call("run_shell", command="echo y = 2 > src/other.py")),
        read("src/other.py"),
        finish_turn(),
    ]
    llm, _ = env.run(script, idle_nudge_after=0)
    assert env.dispatcher.calls == 3
    assert _tool_msgs(llm)[2] == "y = 2\n"


def test_an_elided_read_is_resent_with_a_note(log: EventLog) -> None:
    # compact_history stubs older large outputs; the model then genuinely lacks the text, so a
    # re-read is served again (marked) rather than refused.
    env = Env(log)
    others = [read("src/other.py", start_line=1)] + [
        turn(tool_call("list_files", path=".")) for _ in range(KEEP_RECENT - 1)
    ]
    llm, _ = env.run([read(), *others, read(), finish_turn()], idle_nudge_after=0)
    assert env.dispatcher.calls == KEEP_RECENT + 2
    last = _tool_msgs(llm)[-1]
    assert last.startswith("[unchanged since turn 1; re-sent because that output was elided]\n")
    assert last.endswith("line 300\n")
    assert len("".join(LINES)) > ELIDE_MIN


def test_read_cache_off_dispatches_every_read(log: EventLog) -> None:
    env = Env(log)
    llm, _ = env.run([read(), read(), finish_turn()], read_cache=False, idle_nudge_after=0)
    assert env.dispatcher.calls == 2
    assert "returns a short note" not in llm.calls[0].messages[0]["content"]


def test_the_system_prompt_announces_the_read_cache(log: EventLog) -> None:
    env = Env(log)
    llm, _ = env.run([finish_turn()])
    assert "returns a short note instead of the content" in llm.calls[0].messages[0]["content"]


# ------------------------------------------------------------------ idle nudge


def test_nudge_after_k_turns_without_an_edit(log: EventLog) -> None:
    env = Env(log)
    script = [
        turn(tool_call("list_files", path=".")),
        turn(tool_call("grep", pattern="line", path="src")),
        read(),
        finish_turn(),
    ]
    llm, _ = env.run(script, idle_nudge_after=3, max_tool_turns=10)
    msgs = llm.calls[-1].messages
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"].startswith("You have spent 3 tool turns in a row")
    assert "7 of 10 turns are left" in msgs[-1]["content"]
    (nudge,) = _notes(log, "nudge")
    assert nudge["idle"] == 3 and nudge["tool_turns"] == 3


def test_the_nudge_never_splits_a_batch_of_tool_calls(log: EventLog) -> None:
    env = Env(log)
    batch = turn(
        tool_call("list_files", path="."),
        tool_call("grep", pattern="x", path="src"),
        tool_call("read_file", path="src/other.py"),
    )
    llm, _ = env.run([batch, finish_turn()], idle_nudge_after=2)
    roles = [m["role"] for m in llm.calls[-1].messages]
    assert roles == ["system", "user", "assistant", "tool", "tool", "tool", "user"]


def test_writes_and_shell_runs_reset_the_idle_count(log: EventLog) -> None:
    env = Env(log)
    script = [
        read("src/other.py"),
        turn(tool_call("write_file", path=T, content="def test_a():\n    assert 0\n")),
        turn(tool_call("list_files", path=".")),
        turn(tool_call("run_shell", command=f"pytest -q {T}")),
        turn(tool_call("list_files", path="src")),
        finish_turn(),
    ]
    env.run(script, idle_nudge_after=2)
    assert _notes(log, "nudge") == []


def test_nudge_off_with_zero(log: EventLog) -> None:
    env = Env(log)
    script = [turn(tool_call("list_files", path=".")) for _ in range(12)] + [finish_turn()]
    env.run(script, idle_nudge_after=0)
    assert _notes(log, "nudge") == []


# ------------------------------------------------------------------ live-run replays


def test_replay_run_2_reread_loop(log: EventLog) -> None:
    """Shape of 20261014-123340-85cd: overlapping re-reads of one file, never an edit. With the
    guards the re-reads cost no sandbox calls and the model is nudged to edit; the scripted
    model then edits, runs the test and finishes."""
    env = Env(log)
    loop = [
        read(start_line=1, end_line=100),
        read(start_line=100, end_line=200),
        read(start_line=200, end_line=300),
        read(start_line=1, end_line=50),
        read(start_line=50, end_line=150),
        read(start_line=237, end_line="260"),
        read(start_line=120, end_line=140),
        read(start_line=237, end_line=260),
    ]
    fix = [
        turn(tool_call("write_file", path=T, content="def test_freq_first():\n    assert 0\n")),
        turn(tool_call("write_file", path=SRC, content="FREQ_FIRST = True\n")),
        turn(tool_call("run_shell", command=f"pytest -q {T}")),
        finish_turn(done=True, tests_added=[T]),
    ]
    llm, cand = env.run([*loop, *fix], max_tool_turns=40)
    assert cand.done is True and cand.tool_turns == 11 and cand.tests_added == [T]
    assert env.dispatcher.calls == 3 + 3  # three first reads, then the three fix calls
    assert len(_notes(log, "reread")) == 5
    nudges = [m for m in llm.calls[8].messages if m["role"] == "user"][1:]
    assert len(nudges) == 1 and "stop re-reading" in nudges[0]["content"]


def test_replay_run_1_end_line_strings_are_not_rejected(log: EventLog) -> None:
    """Shape of 20261014-122844-b9ee: `end_line` as a string plus invented keys. Every call is
    executed instead of rejected, and the repairs are on the tool events."""
    env = Env(log)
    script = [
        read(start_line=240, end_line="270"),
        read(start_line=120, end_line="150", context_lines=5),
        read(start_line="125", end_line="140"),  # inside 120-150: cached, not rejected
        finish_turn(done=False, unmet=["x"]),
    ]
    llm, cand = env.run(script, idle_nudge_after=0)
    assert cand.tool_turns == 3 and env.dispatcher.calls == 2
    msgs = _tool_msgs(llm)
    assert msgs[0].startswith("line 240\n") and msgs[1].startswith("line 120\n")
    assert msgs[2].startswith("unchanged since turn 2")
    assert not any(m.startswith("invalid arguments") for m in msgs)
    log.close()
    events = EventLog.read(log.dir.parent, log.run_id)
    tool = [e.data for e in events if e.kind == "tool"]
    assert tool[1]["ignored_args"] == {"context_lines": 5}
    assert tool[1]["repaired_args"] == ["end_line: str→int"]


def test_aliased_test_write_is_tracked_as_a_test(log: EventLog) -> None:
    env = Env(log)
    script = [
        turn(tool_call("write_file", file_path=T, content="def test_a():\n    assert 1\n")),
        finish_turn(done=True, tests_added=[T]),
    ]
    _, cand = env.run(script)
    assert cand.tests_added == [T] and cand.image_uuid != env.base.uuid


# ------------------------------------------------------------------ units + config


def test_read_memory_union_and_forget() -> None:
    mem = ReadMemory("/src")
    mem.record("a.py", 1, 10, 30, 1, 0)
    mem.record("/src/a.py", 11, 20, 30, 2, 1)
    yes = lambda _: True  # noqa: E731
    assert mem.covering("a.py", 5, 15, yes) == (2, 5, 15)
    assert mem.covering("a.py", 5, 25, yes) is None
    assert mem.covering("a.py", 1, 10, lambda i: i != 0) is None  # its serving was elided
    assert mem.covering("a.py", 31, None, yes) is None  # past the end: read_file reports it
    mem.forget("/src/a.py")
    assert mem.covering("a.py", 1, 5, yes) is None


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("WARDEN_MAX_TOOL_TURNS", "WARDEN_IDLE_NUDGE_AFTER", "WARDEN_READ_CACHE"):
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.max_tool_turns == 60 and s.idle_nudge_after == 8 and s.read_cache is True
    monkeypatch.setenv("WARDEN_IDLE_NUDGE_AFTER", "0")
    monkeypatch.setenv("WARDEN_READ_CACHE", "0")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.idle_nudge_after == 0 and s.read_cache is False


@pytest.mark.parametrize(("turns", "warned"), [("25", True), ("40", False), ("60", False)])
def test_doctor_warns_on_a_low_turn_cap(turns: str, warned: bool) -> None:
    from typer.testing import CliRunner

    from warden.cli import app

    res = CliRunner().invoke(app, ["doctor"], env={"WARDEN_MAX_TOOL_TURNS": turns, "NO_COLOR": "1"})
    assert ("WARDEN_MAX_TOOL_TURNS=25 is below 40" in res.stderr) is warned
    assert "WARDEN_IDLE_NUDGE_AFTER" in res.stderr


def test_env_example_documents_the_guards() -> None:
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    assert "WARDEN_MAX_TOOL_TURNS=60" in text
    assert "WARDEN_IDLE_NUDGE_AFTER=8" in text and "WARDEN_READ_CACHE=1" in text
