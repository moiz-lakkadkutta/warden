"""Fix branches: patch out of the sandbox, applied + pushed from the host (no token in a box)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from warden.integrate.branch import NoChangesError, export_patch, push_fix_branch
from warden.runlog.events import EventLog
from warden.sandbox.box import Box


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> tuple[Path, str, str]:
    """A bare 'GitHub' with two commits on main; returns (bare, sha_c1, sha_c2)."""
    work = tmp_path / "seed"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "t@t")
    git(work, "config", "user.name", "t")
    (work / "svc.py").write_text("STATUSES = ('DRAFT', 'FINALIZED')\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "c1")
    c1 = git(work, "rev-parse", "HEAD")
    (work / "other.py").write_text("x = 1\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "c2")
    c2 = git(work, "rev-parse", "HEAD")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return bare, c1, c2


def _patch_against(bare: Path, rev: str, tmp_path: Path) -> str:
    """What export_patch would return from a sandbox checked out at `rev`."""
    w = tmp_path / "box"
    subprocess.run(["git", "clone", "-q", str(bare), str(w)], check=True)
    git(w, "checkout", "-q", "--detach", rev)
    (w / "svc.py").write_text("STATUSES = ('FINALIZED',)\n")
    (w / "test_svc.py").write_text("def test_draft_excluded():\n    assert True\n")
    git(w, "add", "-A")
    git(
        w,
        "-c",
        "user.name=warden",
        "-c",
        "user.email=warden@localhost",
        "commit",
        "-q",
        "-m",
        "fix #196",
    )
    return git(w, "format-patch", "-1", "--stdout") + "\n"


def test_replay_pushes_base_branch_at_rev_and_fix_branch_on_top(
    origin: tuple[Path, str, str], tmp_path: Path
) -> None:
    bare, c1, _ = origin
    patch = _patch_against(bare, c1, tmp_path)
    pushed = push_fix_branch(
        "o/fabric",
        patch,
        issue=196,
        run_id="r1",
        base_rev=c1,
        cache_dir=tmp_path / "cache",
        remote_url=str(bare),
    )
    assert pushed.base == f"warden/base-{c1[:7]}"
    assert pushed.head == "warden/fix-196-r1"
    assert git(bare, "rev-parse", pushed.base) == c1
    assert git(bare, "rev-parse", f"{pushed.head}^") == c1
    assert git(bare, "rev-parse", pushed.head) == pushed.sha
    assert "FINALIZED',)" in git(bare, "show", f"{pushed.head}:svc.py")
    assert git(bare, "log", "-1", "--format=%an", pushed.head) == "warden"


def test_live_issue_targets_main(origin: tuple[Path, str, str], tmp_path: Path) -> None:
    bare, _, c2 = origin
    patch = _patch_against(bare, c2, tmp_path)
    pushed = push_fix_branch(
        "o/fabric",
        patch,
        issue=7,
        run_id="r2",
        base_rev=None,
        cache_dir=tmp_path / "cache",
        remote_url=str(bare),
    )
    assert pushed.base == "main"
    assert git(bare, "rev-parse", f"{pushed.head}^") == c2


def test_second_push_reuses_the_cache_and_base_branch(
    origin: tuple[Path, str, str], tmp_path: Path
) -> None:
    bare, c1, _ = origin
    patch = _patch_against(bare, c1, tmp_path)
    kw: dict[str, Any] = dict(base_rev=c1, cache_dir=tmp_path / "cache", remote_url=str(bare))
    push_fix_branch("o/fabric", patch, issue=196, run_id="a", **kw)
    again = push_fix_branch("o/fabric", patch, issue=196, run_id="b", **kw)
    assert again.head == "warden/fix-196-b"
    assert git(bare, "rev-parse", again.base) == c1
    assert not list(tmp_path.glob("wt-*"))  # worktrees (next to the cache) cleaned up


def test_patch_that_does_not_apply_raises_and_pushes_nothing(
    origin: tuple[Path, str, str], tmp_path: Path
) -> None:
    bare, c1, _ = origin
    bad = _patch_against(bare, c1, tmp_path).replace("STATUSES = ('DRAFT', 'FINALIZED')", "NOPE")
    with pytest.raises(RuntimeError, match="git am"):
        push_fix_branch(
            "o/fabric",
            bad,
            issue=1,
            run_id="x",
            base_rev=c1,
            cache_dir=tmp_path / "cache",
            remote_url=str(bare),
        )
    assert "warden/fix-1-x" not in git(bare, "branch", "--list")


class _GitBox:
    def __init__(self, stdout: str, code: int = 0) -> None:
        self.stdout, self.code = stdout, code
        self.shell = ""
        self.cwd = ""

    def use(self, ref: str) -> str:
        return ref

    def run(self, image: Any, *, shell: str, cwd: str, keep: bool, **_: Any) -> Any:
        self.shell, self.cwd = shell, cwd
        return self.stdout, "nothing to commit" if self.code else "", self.code, None, ""

    def tag(self, image: Any, name: str) -> None:
        return None

    def uuid_of(self, image: Any) -> str:
        return str(image)


def test_export_patch_commits_in_the_box_and_returns_format_patch(tmp_path: Path) -> None:
    backend = _GitBox("From abc Mon Sep 17 00:00:00 2001\nSubject: [PATCH] x\n")
    box = Box(backend, "cand", EventLog(tmp_path / "runs", "t"))
    out = export_patch(box, cwd="/src", issue=196, title="Dashboard KPIs count DRAFT")
    assert out.startswith("From abc")
    assert backend.cwd == "/src"
    assert "git add -A" in backend.shell and "format-patch -1 --stdout" in backend.shell
    assert "user.name=warden" in backend.shell
    assert "'warden: fix #196 — Dashboard KPIs count DRAFT'" in backend.shell


def test_export_patch_without_changes_raises(tmp_path: Path) -> None:
    box = Box(_GitBox("", code=1), "cand", EventLog(tmp_path / "runs", "t"))
    with pytest.raises(NoChangesError):
        export_patch(box, cwd="/src", issue=1, title="t")


def test_push_url_sends_the_fix_branch_to_the_fork_only(
    origin: tuple[Path, str, str], tmp_path: Path
) -> None:
    bare, _, c2 = origin
    fork = tmp_path / "fork.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(bare), str(fork)], check=True)
    git(bare, "branch", "-m", "main", "trunk")  # a non-main default branch
    patch = _patch_against(bare, c2, tmp_path)
    pushed = push_fix_branch(
        "up/stream",
        patch,
        issue=1928,
        run_id="r9",
        base_rev=None,
        cache_dir=tmp_path / "cache",
        remote_url=str(bare),
        push_url=str(fork),
        base_branch="trunk",
    )
    assert pushed.base == "trunk" and pushed.head == "warden/fix-1928-r9"
    assert git(fork, "rev-parse", f"{pushed.head}^") == c2
    assert "warden/fix-1928-r9" not in git(bare, "branch", "--list")  # upstream untouched


def test_replay_through_a_fork_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="push access"):
        push_fix_branch(
            "up/stream",
            "From x\n",
            issue=1,
            run_id="r",
            base_rev="abc",
            cache_dir=tmp_path / "cache",
            push_url="https://github.com/me/stream.git",
        )
