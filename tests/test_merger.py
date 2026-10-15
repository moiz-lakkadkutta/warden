"""D2: a wave's candidates merge with --no-ff onto a fork chain of the integration image."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from warden.integrate.merger import (
    INTEGRATION_BRANCH,
    PATCH_DIR,
    merge_command,
    merge_wave,
)
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.sandbox.fake import FakeBackend, FakeImage
from warden.schemas import Wave
from warden.verify.gate import DEFAULT_COMMANDS

_ISSUE = re.compile(r"checkout -q -B fix/(\d+) (\S+)")
_INTEGRATION = re.compile(rf"checkout -q -B {re.escape(INTEGRATION_BRANCH)} (\S+)")


class GitFake(FakeBackend):
    """FakeBackend plus the two git commands the merger sends: `git rev-parse HEAD` and the
    merge line. HEAD lives in the image as /git/HEAD; `conflicts` / `bad_patch` pick failures."""

    def __init__(self) -> None:
        super().__init__()
        self.conflicts: dict[int, list[str]] = {}
        self.bad_patch: set[int] = set()
        self.merges: list[tuple[int, str, str]] = []  # (issue, wave base, integration parent)

    def _interpret(self, img: FakeImage, shell: str, cwd: str) -> tuple[str, str, int]:
        head = img.files.get("/git/HEAD", b"base0").decode()
        if shell == "git rev-parse HEAD":
            return head + "\n", "", 0
        m = _ISSUE.search(shell)
        if m and "merge --no-ff" in shell:
            issue, base = int(m[1]), m[2]
            parent = _INTEGRATION.search(shell)
            assert parent is not None
            self.merges.append((issue, base, parent[1]))
            if f"{PATCH_DIR}/fix-{issue}.patch" not in img.files:
                return "WARDEN_STAGE=am\n", "error: no patch", 1
            if issue in self.bad_patch:
                return "WARDEN_STAGE=am\n", "error: patch failed: app.py:10", 1
            if issue in self.conflicts:
                files = " ".join(self.conflicts[issue])
                return f"WARDEN_STAGE=am\nWARDEN_STAGE=merge\nWARDEN_CONFLICT={files} \n", "", 1
            sha = f"m{issue}"
            img.files["/git/HEAD"] = sha.encode()
            return f"WARDEN_STAGE=am\nWARDEN_STAGE=merge\nWARDEN_SHA={sha}\n", "", 0
        return super()._interpret(img, shell, cwd)


@pytest.fixture
def log(tmp_path: Path) -> Iterator[EventLog]:
    log = EventLog(tmp_path / "runs", "campaign")
    yield log
    log.close()


def _base(log: EventLog) -> tuple[GitFake, Box]:
    backend = GitFake()
    return backend, Box.from_ref(backend, "warden/fabric-base:latest", log, phase="pr")


def _patches(*issues: int) -> dict[int, str]:
    return {i: f"From abc\nSubject: fix #{i}\n" for i in issues}


def test_three_issue_wave_merges_cleanly_as_a_fork_chain(log: EventLog) -> None:
    backend, base = _base(log)
    wave = Wave(number=1, issues=[207, 196, 200])
    res = merge_wave(base, wave, _patches(196, 200, 207), log, cwd="/src", gate=DEFAULT_COMMANDS)

    assert [o.issue for o in res.outcomes] == [196, 200, 207]  # issue order
    assert all(o.merged for o in res.outcomes)
    assert res.wave.merged == [196, 200, 207]
    # every fix/N branches off the same wave base; each merge goes onto the previous merge
    assert backend.merges == [
        (196, "base0", "base0"),
        (200, "base0", "m196"),
        (207, "base0", "m200"),
    ]
    assert res.sha == "m207"
    # a fork chain: each merge's checkpoint is a child of the previous one, the base untouched
    uuids = [o.image_uuid for o in res.outcomes]
    assert len(set(uuids)) == 3 and base.uuid not in uuids
    assert res.wave.integration_image_uuid == res.box.uuid == uuids[-1]
    assert backend.images[base.uuid].files.get("/git/HEAD") is None
    # the gate ran on the integrated image
    assert res.gate is not None and res.gate.ok


def test_conflict_fails_that_issue_and_the_chain_continues(log: EventLog) -> None:
    backend, base = _base(log)
    backend.conflicts[200] = ["backend/app/service/sales_service.py"]
    res = merge_wave(
        base, Wave(number=1, issues=[196, 200, 207]), _patches(196, 200, 207), log, cwd="/src"
    )

    by = {o.issue: o for o in res.outcomes}
    assert by[196].merged and by[207].merged and not by[200].merged
    assert by[200].reason == "merge conflict in backend/app/service/sales_service.py"
    assert backend.merges[2] == (207, "base0", "m196")  # 207 merges onto 196, not the failed 200
    assert res.wave.merged == [196, 207]
    assert res.gate is None  # no gate commands given


def test_unappliable_patch_and_missing_candidate(log: EventLog) -> None:
    backend, base = _base(log)
    backend.bad_patch.add(196)
    res = merge_wave(
        base,
        Wave(number=2, issues=[196, 205]),
        _patches(196),
        log,
        cwd="/src",
        gate=DEFAULT_COMMANDS,
    )

    by = {o.issue: o for o in res.outcomes}
    assert by[196].reason.startswith("patch does not apply on the wave base")
    assert by[205].reason.startswith("no candidate")
    assert res.skipped == [205]
    assert res.wave.merged == [] and res.box is base and res.sha == "base0"
    assert res.gate is None  # nothing merged: the image did not change


def test_next_wave_forks_the_updated_image(log: EventLog) -> None:
    backend, base = _base(log)
    w1 = merge_wave(base, Wave(number=1, issues=[196, 197]), _patches(196, 197), log, cwd="/src")
    w2 = merge_wave(w1.box, Wave(number=2, issues=[200]), _patches(200), log, cwd="/src")

    assert backend.merges[-1] == (200, "m197", "m197")  # wave 2's base is wave 1's result
    assert w2.sha == "m200"
    events = [e.data for e in EventLog.read(log.dir.parent, log.run_id) if e.kind == "note"]
    assert [e["merge"]["issue"] for e in events if "merge" in e] == [196, 197, 200]


def test_merge_command_is_dash_safe_and_names_the_branch() -> None:
    cmd = merge_command(212, wave_base="aaa", integration="bbb")
    assert "git checkout -q -B fix/212 aaa" in cmd
    assert f"git checkout -q -B {INTEGRATION_BRANCH} bbb" in cmd
    assert "merge --no-ff" in cmd and "fix/212" in cmd
    assert "[[" not in cmd and "$RANDOM" not in cmd and "pipefail" not in cmd
