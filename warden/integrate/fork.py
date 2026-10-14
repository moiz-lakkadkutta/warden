"""Where the fix branch goes: the target repo when the user can push to it, else the user's fork.

Warden pushes to a repo only when GitHub says the authenticated user has push permission on it.
Otherwise it forks (`gh repo fork --clone=false`, idempotent), checks the fork really is the
user's own fork of the target, pushes there and opens the draft PR on the target with
`--head <user>:<branch>`. All `gh` calls go through an injectable runner; tests never run gh.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from warden.integrate.pr import PrOpenError

if TYPE_CHECKING:
    from warden.runlog.events import EventLog

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class PushTarget:
    repo: str  # where the branch is pushed
    push_url: str | None  # None = the target repo's own remote (origin)
    head_owner: str | None  # set for a fork: the PR head is "<owner>:<branch>"

    @property
    def forked(self) -> bool:
        return self.head_owner is not None

    def head(self, branch: str) -> str:
        return f"{self.head_owner}:{branch}" if self.head_owner else branch


def _gh(runner: Runner, log: EventLog | None, *args: str) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    try:
        res = runner(["gh", *args], capture_output=True, text=True, check=False)
    except OSError as e:
        raise PrOpenError(f"could not run gh: {e}", fix="install gh (see `warden doctor`)") from e
    if log is not None:
        log.emit(
            "pr",
            "tool",
            {"name": "gh", "args": list(args), "exit_code": res.returncode},
            ms=int((time.monotonic() - started) * 1000),
        )
    return res


def _out(res: subprocess.CompletedProcess[str]) -> str:
    return (res.stdout or "").strip()


def resolve_push_target(
    repo: str,
    *,
    runner: Runner = subprocess.run,
    log: EventLog | None = None,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 5,
) -> PushTarget:
    """The target repo itself when `permissions.push` is true; otherwise the user's fork of it."""
    perm = _gh(runner, log, "api", f"repos/{repo}", "--jq", ".permissions.push")
    if perm.returncode != 0:
        raise PrOpenError(
            f"cannot read {repo} on GitHub (exit {perm.returncode}).",
            fix="check `gh auth status` and the repo name",
            stderr_tail=(perm.stderr or "").strip()[-300:],
        )
    if _out(perm) == "true":
        return PushTarget(repo=repo, push_url=None, head_owner=None)

    who = _gh(runner, log, "api", "user", "--jq", ".login")
    login = _out(who)
    if who.returncode != 0 or not login:
        raise PrOpenError("cannot tell which GitHub user is logged in.", fix="gh auth login")
    name = repo.split("/", 1)[1]
    fork = f"{login}/{name}"
    made = _gh(runner, log, "repo", "fork", repo, "--clone=false", "--remote=false")
    if made.returncode != 0:
        raise PrOpenError(
            f"gh could not fork {repo} (exit {made.returncode}).",
            fix=f"fork {repo} on github.com as {login}, then re-run with `warden resume`",
            stderr_tail=(made.stderr or "").strip()[-300:],
        )
    # A new fork can take a few seconds to appear; it must be the user's own fork of `repo`.
    want = f"true\t{repo.casefold()}\t{login.casefold()}"
    for attempt in range(attempts):
        check = _gh(
            runner,
            log,
            "api",
            f"repos/{fork}",
            "--jq",
            "[.fork, (.parent.full_name|ascii_downcase), (.owner.login|ascii_downcase)] | @tsv",
        )
        if check.returncode == 0:
            if _out(check).casefold() == want:
                return PushTarget(
                    repo=fork, push_url=f"https://github.com/{fork}.git", head_owner=login
                )
            raise PrOpenError(
                f"{fork} exists but is not {login}'s fork of {repo}; not pushing to it.",
                fix=f"rename or delete {fork}, then re-run with `warden resume`",
            )
        if attempt + 1 < attempts:
            sleep(2.0)
    raise PrOpenError(
        f"the fork {fork} did not appear after `gh repo fork`.",
        fix="wait a minute, then re-run with `warden resume`",
    )
