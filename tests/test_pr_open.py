"""open_draft_pr / confirm_pr — offline. A real `gh` can never run here."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from warden.integrate.pr import PrOpenError, confirm_pr, labels_for, open_draft_pr
from warden.runlog.events import EventLog
from warden.schemas import Plan, RiskClass

MakePlan = Callable[[list[RiskClass]], Plan]

GH_OK = "Creating pull request…\nhttps://github.com/o/r/pull/12\n"


@pytest.fixture(autouse=True)
def _no_real_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: Any, **k: Any) -> None:
        raise AssertionError("real subprocess.run called in tests")

    monkeypatch.setattr(subprocess, "run", _boom)


class FakeRunner:
    def __init__(self, stdout: str = GH_OK, stderr: str = "", rc: int = 0) -> None:
        self.stdout, self.stderr, self.rc = stdout, stderr, rc
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.body_seen: str | None = None

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(kwargs)))
        self.body_seen = Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(argv, self.rc, stdout=self.stdout, stderr=self.stderr)


def _open(runner: FakeRunner, **kw: Any) -> Any:
    return open_draft_pr(
        "o/r",
        "warden/1",
        "main",
        "title",
        "body text",
        labels=kw.pop("labels", []),
        runner=runner,
        **kw,
    )


# 1 -----------------------------------------------------------------------------------------------
def test_argv_body_and_url(capsys: pytest.CaptureFixture[str]) -> None:
    r = FakeRunner()
    res = _open(r)
    argv, kwargs = r.calls[0]
    body_path = argv[argv.index("--body-file") + 1]
    assert argv == [
        "gh",
        "pr",
        "create",
        "--draft",
        "--repo",
        "o/r",
        "--head",
        "warden/1",
        "--base",
        "main",
        "--title",
        "title",
        "--body-file",
        body_path,
    ]
    assert kwargs == {"capture_output": True, "text": True, "check": False}
    assert r.body_seen == "body text"
    assert res.url == "https://github.com/o/r/pull/12"
    assert res.argv == argv and res.exit_code == 0
    assert capsys.readouterr().out == ""


def test_last_url_wins() -> None:
    r = FakeRunner(stdout="see https://github.com/o/r/pull/1\nhttps://github.com/o/r/pull/99\n")
    assert _open(r).url == "https://github.com/o/r/pull/99"


# 2 -----------------------------------------------------------------------------------------------
def test_labels_for(make_plan: MakePlan) -> None:
    assert labels_for(make_plan(["money"])) == ["PENDING-HUMAN"]
    assert labels_for(make_plan(["concurrency"])) == []


def test_labels_in_argv(make_plan: MakePlan) -> None:
    r = FakeRunner()
    _open(r, labels=labels_for(make_plan(["money"])))
    argv = r.calls[0][0]
    assert argv[-2:] == ["--label", "PENDING-HUMAN"]
    r2 = FakeRunner()
    _open(r2, labels=labels_for(make_plan(["concurrency"])))
    assert "--label" not in r2.calls[0][0]


# 3 -----------------------------------------------------------------------------------------------
def test_nonzero_exit_raises(capsys: pytest.CaptureFixture[str]) -> None:
    r = FakeRunner(stdout="", stderr="GraphQL: Could not resolve to a Repository (not found)", rc=1)
    with pytest.raises(PrOpenError) as ei:
        _open(r)
    e = ei.value
    assert not str(e).startswith("ERROR")
    assert "warden/1" in str(e) and "o/r" in str(e)
    assert "gh auth status" in e.fix
    assert "not found" in e.stderr_tail
    assert capsys.readouterr().out == ""


# 4 -----------------------------------------------------------------------------------------------
def test_zero_exit_without_url_raises() -> None:
    with pytest.raises(PrOpenError) as ei:
        _open(FakeRunner(stdout="Creating pull request…\n"))
    assert "no PR URL" in str(ei.value)


# 5 -----------------------------------------------------------------------------------------------
def test_event_and_artifact_with_log(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = EventLog(tmp_path)
    r = FakeRunner()
    res = _open(r, log=log)
    log.close()
    events = EventLog.read(tmp_path, log.run_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.phase == "pr" and ev.kind == "tool"
    assert ev.data["name"] == "gh"
    assert ev.data["exit_code"] == 0
    assert ev.data["url"] == res.url
    assert ev.data["args"] == res.argv[1:]
    assert ev.ms is not None
    body_path = Path(r.calls[0][0][-1])
    assert body_path == tmp_path / log.run_id / "artifacts" / "pr_body.md"
    assert body_path.read_text(encoding="utf-8") == "body text"
    assert capsys.readouterr().out == ""


def test_tempfile_removed_without_log() -> None:
    r = FakeRunner()
    _open(r)
    assert not Path(r.calls[0][0][-1]).exists()


def test_tempfile_removed_on_failure() -> None:
    r = FakeRunner(rc=1, stderr="boom")
    with pytest.raises(PrOpenError):
        _open(r)
    assert not Path(r.calls[0][0][-1]).exists()


def test_event_emitted_on_failure(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    with pytest.raises(PrOpenError):
        _open(FakeRunner(stdout="", rc=1, stderr="boom"), log=log)
    log.close()
    events = EventLog.read(tmp_path, log.run_id)
    assert len(events) == 1
    assert events[0].data["exit_code"] == 1 and events[0].data["url"] is None


class RaisingRunner(FakeRunner):
    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(kwargs)))
        raise FileNotFoundError(2, "No such file or directory", "gh")


def test_runner_oserror_becomes_pr_open_error(capsys: pytest.CaptureFixture[str]) -> None:
    r = RaisingRunner()
    with pytest.raises(PrOpenError) as ei:
        _open(r)
    e = ei.value
    assert str(e).startswith("could not run gh: ")
    assert "gh" in str(e) and "ERROR" not in str(e)
    assert e.fix == "install gh (see `warden doctor`)"
    assert e.stderr_tail == ""
    assert isinstance(e.__cause__, FileNotFoundError)
    assert not Path(r.calls[0][0][-1]).exists()  # temp body file still cleaned up
    assert capsys.readouterr().out == ""


def test_runner_oserror_still_emits_event(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    with pytest.raises(PrOpenError):
        _open(RaisingRunner(), log=log)
    log.close()
    events = EventLog.read(tmp_path, log.run_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.phase == "pr" and ev.kind == "tool"
    assert ev.data["name"] == "gh"
    assert ev.data["exit_code"] is None and ev.data["url"] is None
    assert "gh" in ev.data["error"]


# 6 -----------------------------------------------------------------------------------------------
def test_confirm_yes_bypasses(capsys: pytest.CaptureFixture[str]) -> None:
    buf = StringIO()
    inp = StringIO("n\n")
    assert confirm_pr(True, what="w", input=inp, err=buf) is True
    assert inp.tell() == 0
    assert "--yes" in buf.getvalue()
    assert capsys.readouterr().out == ""


# 7 -----------------------------------------------------------------------------------------------
def test_confirm_non_tty_refuses(capsys: pytest.CaptureFixture[str]) -> None:
    buf = StringIO()
    assert confirm_pr(False, what="w", run_id="r1", input=StringIO(), err=buf) is False
    out = buf.getvalue()
    assert "re-run with --yes" in out and "r1" in out
    assert "ERROR:" not in out
    assert capsys.readouterr().out == ""


# 8 -----------------------------------------------------------------------------------------------
class Tty(StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("y\n", True), ("Y\n", True), ("yes\n", True), ("n\n", False), ("", False)],
)
def test_confirm_tty(typed: str, expected: bool, capsys: pytest.CaptureFixture[str]) -> None:
    buf = StringIO()
    assert confirm_pr(False, what="o/r warden/1 → main", input=Tty(typed), err=buf) is expected
    out = buf.getvalue()
    assert out.startswith("Open draft PR? o/r warden/1 → main [y/N] ")
    if expected:
        assert out.endswith("[y/N] ")
    else:
        assert "not opening the PR" in out
    assert capsys.readouterr().out == ""


def test_confirm_defaults_write_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    assert confirm_pr(False, what="w", run_id="r9", input=StringIO()) is False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Not opening the draft PR" in captured.err
    assert "re-run with --yes" in captured.err and "r9" in captured.err
    assert confirm_pr(True, what="w", input=StringIO()) is True
    captured = capsys.readouterr()
    assert captured.out == "" and "--yes" in captured.err
