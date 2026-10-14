"""Fix branches: the candidate's change leaves the sandbox as a patch, is applied on the host and
pushed to the target repo with the host's git credentials.

No GitHub token ever enters a sandbox: checkpoints persist (180-day retention), so a secret
written into one would outlive the run. The sandbox only commits and prints `git format-patch`.

Branches on the target repo:
  warden/fix-<issue>-<run_id>   the fix, one commit on top of the base
  warden/base-<sha7>            for replays of closed issues: the pre-fix commit, so the draft
                                PR shows only Warden's change (a PR against main would not)
Live issues use `base_branch` (default `main`) as the base. When the user cannot push to the
target repo the fix branch goes to `push_url` (their fork, see fork.py) instead of origin; a
replay's base branch cannot live in a fork, so replays need push access to the target.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from warden.integrate.pr import PrOpenError
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


# Paths a fix branch never touches. Workflows run with the repo's secrets, so a model persuaded by
# an issue body to edit them could exfiltrate those secrets on the next CI run.
PROTECTED_PREFIXES = (".github/",)
_DIFF_PATHS = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)


def touched_paths(patch: str) -> list[str]:
    """Every path a format-patch touches (both sides of a rename), sorted, unique."""
    return sorted({p for pair in _DIFF_PATHS.findall(patch) for p in pair})


def refuse_protected(patch: str) -> None:
    """Raise PrOpenError (nothing is pushed) when the patch touches a protected path."""
    hit = [p for p in touched_paths(patch) if p.startswith(PROTECTED_PREFIXES)]
    if hit:
        raise PrOpenError(
            f"refusing to push: the candidate changes {', '.join(hit)} "
            "(Warden never edits CI workflows or repo automation)",
            fix="make that change by hand; the patch is in the run's artifacts/candidate.patch",
        )


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
    push_url: str | None = None,
    base_branch: str = "main",
    runner: Runner = subprocess.run,
) -> PushedBranch:
    """Apply `patch` on `base_rev` (or origin/<base_branch>) in a throwaway worktree of a cached
    clone and push `warden/fix-<issue>-<run_id>` to origin, or to `push_url` (a fork) when given;
    for a replay also push `warden/base-<sha7>` to origin."""
    if push_url is not None and base_rev is not None:
        raise ValueError("a replay pushes warden/base-<sha> to the target: needs push access")
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
        base = base_branch
        start = _git(runner, cache_dir, "rev-parse", "--verify", f"origin/{base}^{{commit}}")
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
        _git(runner, worktree, "push", "-q", push_url or "origin", f"HEAD:refs/heads/{head}")
    finally:
        runner(
            ["git", "-C", str(cache_dir), "worktree", "remove", "--force", str(worktree)],
            capture_output=True,
            check=False,
        )
    return PushedBranch(head=head, base=base, sha=sha)
