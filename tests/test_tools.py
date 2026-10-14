import inspect
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import warden.fix.tools as tools_module
from warden.fix.tools import TOOL_NAMES, TOOL_SCHEMAS, ToolDispatcher, ToolResult, as_message
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.sandbox.fake import FakeBackend, FakeImage


@pytest.fixture
def log(tmp_path: Path) -> Iterator[EventLog]:
    log = EventLog(tmp_path / "runs", "t")
    yield log
    log.close()


def _setup(log: EventLog) -> tuple[FakeBackend, Box, FakeImage]:
    backend = FakeBackend()
    box = Box.from_ref(backend, "warden/fabric-base:latest", log)
    return backend, box, backend.images[box.uuid]


def _events(log: EventLog) -> list[Any]:
    log.close()
    return EventLog.read(log.dir.parent, log.run_id)


def test_tool_schemas_shape() -> None:
    assert TOOL_NAMES == ("list_files", "read_file", "grep", "write_file", "run_shell")
    assert len(TOOL_SCHEMAS) == 5
    assert [s["function"]["name"] for s in TOOL_SCHEMAS] == list(TOOL_NAMES)
    for schema in TOOL_SCHEMAS:
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["description"]
        assert "strict" not in fn
        params = fn["parameters"]
        assert params["type"] == "object"
        assert params["additionalProperties"] is False
        assert set(params["required"]) == set(params["properties"])
        assert "title" not in params
        for prop in params["properties"].values():
            assert prop["description"]
            assert "title" not in prop
            assert "default" not in prop


def test_list_files_happy(log: EventLog) -> None:
    _, box, img = _setup(log)
    img.files["/app/src/b.py"] = b"b"
    img.files["/app/src/a.py"] = b"a"
    result = ToolDispatcher(log).dispatch(box, "list_files", {"path": "src"})
    assert result.ok and result.exit_code == 0
    assert result.output == "src/a.py\nsrc/b.py\n"
    assert result.box is box
    assert result.truncated is False


def test_read_file_happy_and_missing(log: EventLog) -> None:
    _, box, img = _setup(log)
    img.files["/app/src/a.py"] = b"def foo():\n    return 1\n"
    d = ToolDispatcher(log)
    good = d.dispatch(box, "read_file", {"path": "src/a.py"})
    assert good.ok and good.exit_code == 0
    assert good.output == "def foo():\n    return 1\n"
    assert good.box is box
    bad = d.dispatch(box, "read_file", {"path": "src/missing.py"})
    assert bad.ok is False and bad.exit_code == 1
    assert "No such file" in bad.output
    assert bad.box is box


def test_grep_match_and_no_match(log: EventLog) -> None:
    _, box, img = _setup(log)
    img.files["/app/src/a.py"] = b"import os\ndef foo():\n    pass\n"
    d = ToolDispatcher(log)
    hit = d.dispatch(box, "grep", {"pattern": "def foo", "path": "src"})
    assert hit.ok and hit.exit_code == 0
    assert hit.output == "src/a.py:2:def foo():\n"
    miss = d.dispatch(box, "grep", {"pattern": "nope", "path": "src"})
    assert miss.ok is True and miss.exit_code == 1
    assert miss.output == "(no matches)"
    assert miss.box is box


def test_write_file_forks(log: EventLog) -> None:
    backend, parent, _ = _setup(log)
    d = ToolDispatcher(log)
    res = d.dispatch(parent, "write_file", {"path": "src/new.py", "content": "x = 1\n"})
    assert res.ok and res.exit_code == 0
    assert res.output == "wrote 6 bytes to src/new.py"
    child = res.box
    assert child.uuid != parent.uuid
    assert d.dispatch(child, "read_file", {"path": "src/new.py"}).output == "x = 1\n"
    assert d.dispatch(parent, "read_file", {"path": "src/new.py"}).ok is False
    assert "/app/src/new.py" not in backend.images[parent.uuid].files
    assert backend.images[child.uuid].files["/app/src/new.py"] == b"x = 1\n"
    absolute = d.dispatch(parent, "write_file", {"path": "/etc/warden.cfg", "content": "k=v"})
    assert absolute.ok and absolute.box.uuid != parent.uuid
    assert backend.images[absolute.box.uuid].files["/etc/warden.cfg"] == b"k=v\n"


def test_run_shell_happy(log: EventLog) -> None:
    backend, box, _ = _setup(log)
    res = ToolDispatcher(log).dispatch(box, "run_shell", {"command": "echo hi"})
    assert res.ok and res.exit_code == 0
    assert res.output.startswith("hi")
    assert res.output.endswith("[exit 0]")
    assert "--- stderr ---" not in res.output
    assert res.box.uuid != box.uuid
    assert backend.timeouts[-1] == 600


def test_run_shell_nonzero(log: EventLog) -> None:
    _, box, img = _setup(log)
    img.test_results["tests/t.py"] = 1
    res = ToolDispatcher(log).dispatch(box, "run_shell", {"command": "pytest tests/t.py"})
    assert res.ok is False and res.exit_code == 1
    assert "1 failed" in res.output
    assert res.output.endswith("[exit 1]")
    assert res.box.uuid != box.uuid


def test_truncation(log: EventLog) -> None:
    _, box, img = _setup(log)
    big = "".join(f"{i % 10}" for i in range(20_000))
    img.files["/app/big.txt"] = big.encode()
    img.files["/app/small.txt"] = b"tiny"
    d = ToolDispatcher(log, max_output=1000)
    res = d.dispatch(box, "run_shell", {"command": "cat big.txt"})
    assert res.truncated is True
    total = len(big) + len("\n[exit 0]")
    marker = f"\n…[truncated {total - 1000} of {total} chars]…\n"
    assert marker in res.output
    assert len(res.output) <= 1000 + len(marker)  # shell output keeps head + tail
    assert res.output.startswith(big[:500])
    small = d.dispatch(box, "read_file", {"path": "small.txt"})
    assert small.truncated is False and small.output == "tiny"


def test_truncate_function() -> None:
    assert tools_module.truncate("abc", 10) == ("abc", False)
    text, cut = tools_module.truncate("a" * 50 + "b" * 50, 20)
    assert cut is True
    assert text.startswith("a" * 10) and text.endswith("b" * 10)
    assert "[truncated 80 of 100 chars]" in text


def test_unknown_tool(log: EventLog) -> None:
    _, box, _ = _setup(log)
    res = ToolDispatcher(log).dispatch(box, "delete_everything", {})
    assert res.ok is False and res.exit_code is None
    assert res.name == "delete_everything"
    for name in TOOL_NAMES:
        assert name in res.output
    assert res.output.startswith("unknown tool delete_everything")
    assert res.box is box


@pytest.mark.parametrize(
    ("name", "args", "needle"),
    [
        ("read_file", {}, "path"),
        ("write_file", {"path": "a", "extra": 1}, "content"),  # an extra never fills a gap
        ("run_shell", {"command": "ls", "timeout": "soon"}, "timeout"),
        ("read_file", {"path": None, "start_line": 3}, "path"),  # null for a required key
        ("read_file", "src/a.py", "invalid"),
        ("list_files", None, "invalid"),
    ],
)
def test_bad_args(log: EventLog, name: str, args: Any, needle: str) -> None:
    _, box, _ = _setup(log)
    res = ToolDispatcher(log).dispatch(box, name, args)
    assert res.ok is False and res.exit_code is None
    assert res.output.startswith(f"invalid arguments for {name}")
    assert needle in res.output
    assert "https://" not in res.output
    assert res.box is box


def test_every_call_emits_tool_event(log: EventLog) -> None:
    _, box, img = _setup(log)
    img.files["/app/src/a.py"] = b"x\n"
    d = ToolDispatcher(log)
    long_cmd = "echo " + "y" * 300
    d.dispatch(box, "list_files", {"path": "src"})
    d.dispatch(box, "read_file", {"path": "src/a.py"})
    d.dispatch(box, "grep", {"pattern": "x", "path": "src"})
    d.dispatch(box, "write_file", {"path": "src/b.py", "content": "secret-content"})
    d.dispatch(box, "run_shell", {"command": long_cmd})
    d.dispatch(box, "nope", {})
    d.dispatch(box, "read_file", {})
    events = _events(log)
    tool = [e for e in events if e.kind == "tool"]
    assert len(tool) == 7
    assert [e.data["name"] for e in tool] == [
        "list_files",
        "read_file",
        "grep",
        "write_file",
        "run_shell",
        "nope",
        "read_file",
    ]
    for e in tool:
        assert e.phase == "fix"
        assert {"name", "args", "ok", "exit_code", "from", "to", "truncated"} <= set(e.data)
        assert e.ms is not None and e.ms >= 0
    assert [e.data["ok"] for e in tool] == [True, True, True, True, True, False, False]
    assert [e.data["exit_code"] for e in tool] == [0, 0, 0, 0, 0, None, None]
    write = tool[3].data
    assert write["args"] == {"path": "src/b.py", "bytes": 14}
    assert "secret-content" not in str(write)
    assert write["from"] == box.uuid and write["to"] != box.uuid
    shell = tool[4].data
    assert len(shell["args"]["command"]) <= 200
    assert shell["args"]["timeout"] == 600
    assert tool[5].data["error"].startswith("unknown tool")
    assert tool[6].data["error"].startswith("invalid arguments")
    assert tool[5].data["from"] == tool[5].data["to"] == box.uuid
    shell_idx = events.index(tool[4])
    assert events[shell_idx - 1].kind == "sandbox"
    assert events[shell_idx - 1].data["shell"].startswith("echo ")
    assert tool[0].data["args"] == {"path": "src"}


def test_calls_counter(log: EventLog) -> None:
    _, box, _ = _setup(log)
    d = ToolDispatcher(log)
    assert d.calls == 0
    d.dispatch(box, "list_files", {"path": "."})
    d.dispatch(box, "nope", {})
    d.dispatch(box, "read_file", {})
    d.dispatch(box, "run_shell", {"command": "false"})
    assert d.calls == 4


def test_as_message(log: EventLog) -> None:
    _, box, _ = _setup(log)
    res = ToolDispatcher(log).dispatch(box, "run_shell", {"command": "echo hi"})
    msg = as_message(res, "call_123")
    assert msg == {"role": "tool", "tool_call_id": "call_123", "content": res.output}


def test_tool_result_is_frozen(log: EventLog) -> None:
    _, box, _ = _setup(log)
    res = ToolDispatcher(log).dispatch(box, "list_files", {})
    assert isinstance(res, ToolResult)
    with pytest.raises(AttributeError):
        res.ok = False  # type: ignore[misc]


def test_shell_timeout_caps_and_phase(log: EventLog) -> None:
    backend, box, _ = _setup(log)
    d = ToolDispatcher(log, phase="verify", shell_timeout=30)
    d.dispatch(box, "run_shell", {"command": "echo hi", "timeout": 120})
    assert backend.timeouts[-1] == 30
    d.dispatch(box, "run_shell", {"command": "echo hi", "timeout": 5})
    assert backend.timeouts[-1] == 30  # the floor never exceeds the configured cap
    events = _events(log)
    assert all(e.phase == "verify" for e in events if e.kind == "tool")


def test_shell_args_are_quoted(log: EventLog) -> None:
    backend, box, img = _setup(log)
    img.files["/app/src/my file.py"] = b"hello world\n"
    d = ToolDispatcher(log)
    res = d.dispatch(box, "read_file", {"path": "src/my file.py"})
    assert res.ok and res.output == "hello world\n"
    assert "'src/my file.py'" in backend.calls[-1]
    hit = d.dispatch(box, "grep", {"pattern": "hello world", "path": "src"})
    assert hit.ok and hit.output == "src/my file.py:1:hello world\n"


def test_invalid_write_file_never_logs_content(log: EventLog) -> None:
    _, box, _ = _setup(log)
    secret = "SECRETSECRETSECRET" * 600  # ~10 KB
    res = ToolDispatcher(log).dispatch(
        box, "write_file", {"path": "", "content": secret, "extra": 1}
    )
    assert res.ok is False and res.exit_code is None
    log.close()
    lines = (log.dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    tool_lines = [ln for ln in lines if json.loads(ln)["kind"] == "tool"]
    assert len(tool_lines) == 1
    assert "SECRET" not in tool_lines[0]
    assert len(tool_lines[0]) < 1024
    args = json.loads(tool_lines[0])["data"]["args"]
    assert "content" not in args
    assert args["bytes"] == len(secret) and args["path"] == "" and args["extra"] == 1


def test_scrub_args_shapes() -> None:
    long = "x" * 500
    scrubbed = tools_module.scrub_args({"command": long, "n": 3, "obj": [long], "content": 42})
    assert len(scrubbed["command"]) == 200
    assert scrubbed["n"] == 3
    assert len(scrubbed["obj"]) <= 200
    assert scrubbed["bytes"] == 2 and "content" not in scrubbed
    assert tools_module.scrub_args(long) == {"raw": repr(long)[:200]}
    assert tools_module.scrub_args(None) == {"raw": "None"}


def test_unknown_tool_event_scrubs_raw_args(log: EventLog) -> None:
    _, box, _ = _setup(log)
    ToolDispatcher(log).dispatch(box, "nope", {"content": "hidden", "path": "p"})
    ToolDispatcher(log).dispatch(box, "nope", "not a dict")
    tool = [e for e in _events(log) if e.kind == "tool"]
    assert tool[0].data["args"] == {"bytes": 6, "path": "p"}
    assert tool[1].data["args"] == {"raw": "'not a dict'"}


def test_run_shell_pipe_inside_quotes(log: EventLog) -> None:
    _, box, _ = _setup(log)
    res = ToolDispatcher(log).dispatch(box, "run_shell", {"command": 'echo "a|b"'})
    assert res.ok and res.exit_code == 0
    assert res.output.startswith("a|b")


def test_run_shell_unparseable_does_not_raise(log: EventLog) -> None:
    _, box, _ = _setup(log)
    res = ToolDispatcher(log).dispatch(box, "run_shell", {"command": 'echo "unterminated'})
    assert res.ok is False and res.exit_code == 127
    assert "cannot parse shell" in res.output
    assert res.output.endswith("[exit 127]")


def test_run_shell_or_is_not_a_pipe(log: EventLog) -> None:
    _, box, _ = _setup(log)
    res = ToolDispatcher(log).dispatch(box, "run_shell", {"command": "false || true"})
    assert res.ok is False and res.exit_code == 127


def test_write_file_empty_path(log: EventLog) -> None:
    _, box, _ = _setup(log)
    res = ToolDispatcher(log).dispatch(box, "write_file", {"path": "", "content": "x"})
    assert res.ok is False and res.exit_code is None
    assert res.output.startswith("invalid arguments for write_file")
    assert "path" in res.output
    assert res.box is box


@pytest.mark.parametrize(
    ("name", "args"),
    [("read_file", {"path": ""}), ("grep", {"pattern": "", "path": "src"})],
)
def test_empty_required_strings_rejected(log: EventLog, name: str, args: Any) -> None:
    _, box, _ = _setup(log)
    res = ToolDispatcher(log).dispatch(box, name, args)
    assert res.ok is False and res.exit_code is None


def test_no_contree_import() -> None:
    assert "contree" not in inspect.getsource(tools_module)
    assert not [m for m in sys.modules if m.startswith("contree")]


class _RecordingBackend:
    """Records (shell, cwd) of every run; always succeeds with a child checkpoint."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, str]] = []

    def use(self, ref: str) -> str:
        return ref

    def run(self, image: Any, *, shell: str, cwd: str, keep: bool, **_: Any) -> Any:
        self.runs.append((shell, cwd))
        return "ok", "", 0, (f"{image}+" if keep else None), (f"{image}+" if keep else "")

    def tag(self, image: Any, name: str) -> None:
        return None

    def uuid_of(self, image: Any) -> str:
        return str(image)


def test_run_shell_wraps_command_and_uses_workdir(log: EventLog) -> None:
    backend = _RecordingBackend()
    box = Box(backend, "base", log)
    d = ToolDispatcher(log, cwd="/src", shell_wrap="with-services sh -c")
    res = d.dispatch(box, "run_shell", {"command": "pytest -q 'a b.py'", "timeout": 60})
    assert res.ok
    assert backend.runs[-1] == ("with-services sh -c 'pytest -q '\"'\"'a b.py'\"'\"''", "/src")
    # read-only tools are not wrapped (no services needed), but run in the workdir
    d.dispatch(box, "read_file", {"path": "backend/app/x.py"})
    assert backend.runs[-1] == ("cat backend/app/x.py", "/src")
    # event shows the command the model asked for, not the wrapper
    ev = [e for e in _events(log) if e.data.get("name") == "run_shell"][-1]
    assert ev.data["args"]["command"] == "pytest -q 'a b.py'"


def test_run_shell_without_wrap_is_unchanged(log: EventLog) -> None:
    backend = _RecordingBackend()
    ToolDispatcher(log).dispatch(Box(backend, "b", log), "run_shell", {"command": "ls"})
    assert backend.runs[-1] == ("ls", "/app")


def test_read_file_returns_a_16k_file_whole(log: EventLog) -> None:
    _, box, img = _setup(log)
    text = "".join(f"line {i} " + "x" * 60 + "\n" for i in range(240))  # ~16.5k chars
    img.files["/app/svc.py"] = text.encode()
    res = ToolDispatcher(log).dispatch(box, "read_file", {"path": "svc.py"})
    assert res.ok and res.truncated is False and res.output == text


def test_read_file_pages_by_whole_lines_with_a_continue_hint(log: EventLog) -> None:
    _, box, img = _setup(log)
    lines = [f"row {i:03d}\n" for i in range(1, 101)]  # 8 chars each
    img.files["/app/big.py"] = "".join(lines).encode()
    d = ToolDispatcher(log, read_limit=200)
    first = d.dispatch(box, "read_file", {"path": "big.py"})
    assert first.ok and first.truncated
    shown = first.output.split("\n…[")[0] + "\n"
    assert shown == "".join(lines[:25])  # 25 whole lines = 200 chars, never a partial line
    assert "lines 1-25 of 100" in first.output
    assert "start_line=26" in first.output
    nxt = d.dispatch(box, "read_file", {"path": "big.py", "start_line": 26})
    assert nxt.output.startswith("row 026\n")
    assert "lines 26-50 of 100" in nxt.output
    last = d.dispatch(box, "read_file", {"path": "big.py", "start_line": 90})
    assert last.truncated is False and last.output == "".join(lines[89:])
    past = d.dispatch(box, "read_file", {"path": "big.py", "start_line": 500})
    assert past.ok is False and "100 lines" in past.output


def test_read_file_accepts_an_end_line_range(log: EventLog) -> None:
    # Nemotron Super asks for ranges with end_line, often as a string ("970"): on icalendar#1928
    # 9 of 25 tool turns were rejected as invalid arguments before this existed.
    _, box, img = _setup(log)
    lines = [f"row {i:03d}\n" for i in range(1, 101)]
    img.files["/app/big.py"] = "".join(lines).encode()
    d = ToolDispatcher(log)
    res = d.dispatch(box, "read_file", {"path": "big.py", "start_line": 40, "end_line": "42"})
    assert res.ok and res.output == "".join(lines[39:42])
    null = d.dispatch(box, "read_file", {"path": "big.py", "start_line": 99, "end_line": None})
    assert null.output == "".join(lines[98:])
    past = d.dispatch(box, "read_file", {"path": "big.py", "start_line": 95, "end_line": 500})
    assert past.ok and past.output == "".join(lines[94:])


# ------------------------------------------------------------------ tolerant arguments


def test_unknown_and_mistyped_args_are_tolerated_and_recorded(log: EventLog) -> None:
    # icalendar#1928 run 20261014-122844-b9ee: 9 of 25 turns burned on rejected arguments.
    _, box, img = _setup(log)
    lines = [f"row {i:03d}\n" for i in range(1, 101)]
    img.files["/app/big.py"] = "".join(lines).encode()
    d = ToolDispatcher(log)
    res = d.dispatch(
        box,
        "read_file",
        {"path": "big.py", "start_line": "40", "end_line": "42.0", "context": 3, "mode": "x"},
    )
    assert res.ok and res.output == "".join(lines[39:42])
    assert res.lines == (40, 42) and res.total_lines == 100
    shell = d.dispatch(box, "run_shell", {"command": "true", "timeout": 9999, "cwd": "/app"})
    assert shell.ok
    tool = [e for e in _events(log) if e.kind == "tool"]
    first = tool[0].data
    assert first["ok"] is True and "error" not in first
    assert first["args"] == {"path": "big.py", "start_line": 40, "end_line": 42}
    assert first["ignored_args"] == {"context": 3, "mode": "x"}
    assert first["repaired_args"] == ["start_line: str→int", "end_line: str→int"]
    second = tool[1].data
    assert second["args"]["timeout"] == 600
    assert second["ignored_args"] == {"cwd": "/app"}
    assert second["repaired_args"] == ["timeout: 9999→600"]


def test_aliases_and_nulls_for_defaulted_keys(log: EventLog) -> None:
    backend, box, img = _setup(log)
    img.files["/app/a.py"] = b"x = 1\n"
    d = ToolDispatcher(log)
    read = d.dispatch(box, "read_file", {"file_path": "a.py", "start_line": None})
    assert read.ok and read.output == "x = 1\n"
    wrote = d.dispatch(box, "write_file", {"file": "b.py", "contents": "y = 2\n"})
    assert wrote.ok and backend.images[wrote.box.uuid].files["/app/b.py"] == b"y = 2\n"
    both = d.dispatch(box, "read_file", {"path": "a.py", "file_path": "nope.py"})
    assert both.ok and both.output == "x = 1\n"  # the declared key wins; the alias is ignored
    events = [e.data for e in _events(log) if e.kind == "tool"]
    assert events[0]["repaired_args"] == ["file_path→path", "start_line: null→default"]
    assert events[1]["repaired_args"] == ["file→path", "contents→content"]
    assert events[2]["ignored_args"] == {"file_path": "nope.py"}


def test_clean_calls_carry_no_repair_fields(log: EventLog) -> None:
    _, box, img = _setup(log)
    img.files["/app/a.py"] = b"x\n"
    ToolDispatcher(log).dispatch(box, "read_file", {"path": "a.py", "start_line": 1})
    (event,) = [e.data for e in _events(log) if e.kind == "tool"]
    assert "ignored_args" not in event and "repaired_args" not in event


def test_grep_supports_alternation(log: EventLog) -> None:
    _, box, img = _setup(log)
    img.files["/app/src/a.py"] = b"FOO = 1\nBAR = 2\nBAZ = 3\n"
    hit = ToolDispatcher(log).dispatch(box, "grep", {"pattern": "FOO|BAR", "path": "src"})
    assert hit.output == "src/a.py:1:FOO = 1\nsrc/a.py:2:BAR = 2\n"


def test_write_file_adds_the_missing_final_newline(log: EventLog) -> None:
    """Regression (Fabric#220): Super's whole-file rewrites dropped the trailing newline and the
    target repo's `ruff check` (W292) failed on both files."""
    backend, box, _ = _setup(log)
    d = ToolDispatcher(log)
    res = d.dispatch(box, "write_file", {"path": "svc.py", "content": "__all__ = ['x']"})
    assert backend.images[res.box.uuid].files["/app/svc.py"] == b"__all__ = ['x']\n"
    kept = d.dispatch(box, "write_file", {"path": "a.py", "content": "x = 1\n"})
    assert backend.images[kept.box.uuid].files["/app/a.py"] == b"x = 1\n"
    empty = d.dispatch(box, "write_file", {"path": "__init__.py", "content": ""})
    assert backend.images[empty.box.uuid].files["/app/__init__.py"] == b""


def test_model_chosen_shell_timeouts_have_a_floor(log: EventLog) -> None:
    # SWE-rebench spikeinterface-2775: Super passed timeout=10 / 30 to `pip install -e .` and a
    # first pytest import; the sandbox op timed out and the task was lost
    from warden.fix.tools import MIN_SHELL_TIMEOUT

    backend, box, _ = _setup(log)
    d = ToolDispatcher(log)
    d.dispatch(box, "run_shell", {"command": "echo hi", "timeout": 10})
    assert backend.timeouts[-1] == MIN_SHELL_TIMEOUT == 120
    d.dispatch(box, "run_shell", {"command": "echo hi", "timeout": 300})
    assert backend.timeouts[-1] == 300
