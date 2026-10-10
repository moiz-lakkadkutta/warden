"""Fix branches: the candidate's change leaves the sandbox as a patch, is applied on the host and
pushed to the target repo with the host's git credentials.

No GitHub token ever enters a sandbox: checkpoints persist (180-day retention), so a secret
written into one would outlive the run. The sandbox only commits and prints `git format-patch`.

Branches on the target repo:
  warden/fix-<issue>-<run_id>   the fix, one commit on top of the base
  warden/base-<sha7>            for replays of closed issues: the pre-fix commit, so the draft
                                PR shows only Warden's change (a PR against main would not)
Live issues use `main` as the base.
"""

from __future__ import annotations

import shlex
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from warden.sandbox.box import Box

Runner = Callable[..., subprocess.CompletedProcess[str]]


class NoChangesError(RuntimeError):
    """The candidate has nothing to commit: there is no fix to push."""


@dataclass(frozen=True)
class PushedBranch:
    head: str
    base: str
    sha: str


def export_patch(box: Box, *, cwd: str, issue: int, title: str) -> str:
    """Commit everything the candidate changed (inside the box) and return the patch text."""
    message = f"warden: fix #{issue} — {title}"
    cmd = (
        "git add -A && "
        "git -c user.name=warden -c user.email=warden@localhost "
        f"commit -q -m {shlex.quote(message)} && "
        "git format-patch -1 --stdout"
    )
    res = box.run(cmd, cwd=cwd, keep=False)
    if not res.ok or not res.stdout.startswith("From "):
        raise NoChangesError(f"no change to export from {box.uuid}: {res.stderr.strip()[:200]}")
    return res.stdout


def _git(runner: Runner, cwd: Path, *args: str) -> str:
    res = runner(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {(res.stderr or '').strip()[-300:]}")
    return (res.stdout or "").strip()


def push_fix_branch(
    repo: str,
    patch: str,
    *,
    issue: int,
    run_id: str,
    base_rev: str | None,
    cache_dir: Path,
    remote_url: str | None = None,
    runner: Runner = subprocess.run,
) -> PushedBranch:
    """Apply `patch` on `base_rev` (or origin/main) in a throwaway worktree of a cached clone and
    push `warden/fix-<issue>-<run_id>`; for a replay also push `warden/base-<sha7>`."""
    url = remote_url or f"https://github.com/{repo}.git"
    if not (cache_dir / ".git").exists() and not (cache_dir / "HEAD").exists():
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        res = runner(
            ["git", "clone", "-q", "--no-checkout", url, str(cache_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(f"git clone {url} failed: {(res.stderr or '').strip()[-300:]}")
    _git(runner, cache_dir, "fetch", "-q", "origin")

    if base_rev is None:
        base = "main"
        start = _git(runner, cache_dir, "rev-parse", "--verify", "origin/main^{commit}")
    else:
        start = _git(runner, cache_dir, "rev-parse", "--verify", f"{base_rev}^{{commit}}")
        base = f"warden/base-{start[:7]}"
        _git(runner, cache_dir, "push", "-q", "origin", f"{start}:refs/heads/{base}")

    head = f"warden/fix-{issue}-{run_id}"
    worktree = Path(tempfile.mkdtemp(prefix="wt-", dir=cache_dir.parent))
    worktree.rmdir()  # git worktree add wants to create it
    _git(runner, cache_dir, "worktree", "add", "-q", "--detach", str(worktree), start)
    try:
        patch_file = worktree.parent / f"{worktree.name}.patch"
        patch_file.write_text(patch, encoding="utf-8")
        try:
            _git(
                runner,
                worktree,
                "-c",
                "user.name=warden",
                "-c",
                "user.email=warden@localhost",
                "am",
                "-q",
                "--3way",
                str(patch_file),
            )
        except RuntimeError as err:
            runner(["git", "-C", str(worktree), "am", "--abort"], capture_output=True, check=False)
            raise RuntimeError(f"git am could not apply the candidate patch: {err}") from err
        finally:
            patch_file.unlink(missing_ok=True)
        sha = _git(runner, worktree, "rev-parse", "HEAD")
        _git(runner, worktree, "push", "-q", "origin", f"HEAD:refs/heads/{head}")
    finally:
        runner(
            ["git", "-C", str(cache_dir), "worktree", "remove", "--force", str(worktree)],
            capture_output=True,
            check=False,
        )
    return PushedBranch(head=head, base=base, sha=sha)
