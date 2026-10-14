"""Fork support: push only where the user may push; otherwise fork, push there, PR upstream with
`--head user:branch`. Every gh call is a fake; a real subprocess can never run here."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import warden.integrate.branch as branch_mod
import warden.integrate.fork as fork_mod
import warden.integrate.pr as pr_mod
import warden.sandbox.box as box_mod
from warden.config import Settings
from warden.integrate.fork import PushTarget, resolve_push_target
from warden.integrate.pr import PrOpenError
from warden.pipeline import IssueRef, NullProgress, StepContext
from warden.pipeline import _live_pr as live_pr  # the conftest swaps the module attribute
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.sandbox.fake import FakeBackend
from warden.schemas import Candidate, Plan, RiskClass, Verdict

UPSTREAM = "collective/icalendar"


@pytest.fixture(autouse=True)
def _no_real_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: Any, **k: Any) -> None:
        raise AssertionError("real subprocess.run called in tests")

    monkeypatch.setattr(subprocess, "run", _boom)


class FakeGh:
    """Answers `gh` argv by prefix; records every call. `fork_after` = verify attempts that 404."""

    def __init__(
        self,
        *,
        push: str = "false",
        login: str = "moiz",
        fork_tsv: str = f"true\t{UPSTREAM}\tmoiz",
        fork_after: int = 0,
        fork_rc: int = 0,
    ) -> None:
        self.push, self.login, self.fork_tsv = push, login, fork_tsv
        self.fork_after, self.fork_rc = fork_after, fork_rc
        self.calls: list[list[str]] = []
        self.body: str | None = None

    def __call__(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        args = argv[1:]

        def done(out: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

        if args[:2] == ["api", f"repos/{UPSTREAM}"]:
            return done(self.push + "\n")
        if args[:2] == ["api", "user"]:
            return done(self.login + "\n")
        if args[:2] == ["repo", "fork"]:
            return done("", self.fork_rc)
        if args[0] == "api" and args[1].startswith("repos/"):
            if self.fork_after > 0:
                self.fork_after -= 1
                return done("", 1)
            return done(self.fork_tsv + "\n")
        if args[:2] == ["pr", "create"]:
            self.body = Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8")
            return done("https://github.com/collective/icalendar/pull/1999\n")
        raise AssertionError(f"unexpected gh call {argv}")


def test_push_access_targets_the_repo_itself() -> None:
    gh = FakeGh(push="true")
    target = resolve_push_target(UPSTREAM, runner=gh)
    assert target == PushTarget(repo=UPSTREAM, push_url=None, head_owner=None)
    assert not target.forked and target.head("warden/fix-1-r") == "warden/fix-1-r"
    assert len(gh.calls) == 1  # never forks when it may push


def test_no_push_access_forks_and_targets_the_fork() -> None:
    gh = FakeGh()
    target = resolve_push_target(UPSTREAM, runner=gh, sleep=lambda _: None)
    assert target.repo == "moiz/icalendar"
    assert target.push_url == "https://github.com/moiz/icalendar.git"
    assert target.head("warden/fix-1928-r") == "moiz:warden/fix-1928-r"
    assert gh.calls[2] == ["gh", "repo", "fork", UPSTREAM, "--clone=false", "--remote=false"]
    assert gh.calls[3][:3] == ["gh", "api", "repos/moiz/icalendar"]


def test_fork_that_appears_late_is_retried() -> None:
    slept: list[float] = []
    gh = FakeGh(fork_after=2)
    target = resolve_push_target(UPSTREAM, runner=gh, sleep=slept.append)
    assert target.forked and len(slept) == 2


@pytest.mark.parametrize(
    "tsv",
    ["false\t\tmoiz", "true\tsomeone/else\tmoiz", f"true\t{UPSTREAM}\tother"],
    ids=["not-a-fork", "fork-of-another-repo", "not-owned"],
)
def test_never_pushes_to_a_repo_that_is_not_the_users_fork(tsv: str) -> None:
    with pytest.raises(PrOpenError, match="not moiz's fork"):
        resolve_push_target(UPSTREAM, runner=FakeGh(fork_tsv=tsv), sleep=lambda _: None)


def test_fork_failure_and_missing_fork_raise() -> None:
    with pytest.raises(PrOpenError, match="could not fork"):
        resolve_push_target(UPSTREAM, runner=FakeGh(fork_rc=1))
    with pytest.raises(PrOpenError, match="did not appear"):
        resolve_push_target(
            UPSTREAM, runner=FakeGh(fork_after=99), sleep=lambda _: None, attempts=3
        )


# ------------------------------------------------------------------------- _live_pr end to end
def _plan(risk: list[RiskClass]) -> Plan:
    return Plan(
        issue=1928,
        summary="s",
        files_to_touch=[],
        tests_to_add=[],
        landing_order=[],
        risk_class=risk,
        citations=[],
        acceptance=[],
    )


def _verdict() -> Verdict:
    return Verdict(
        decision="PASS",
        theater_failed_on_base=True,
        suite={"passed": 3},
        mypy_ok=True,
        ruff_ok=True,
        alembic_single_head=True,
        openapi_drift=False,
        race=None,
        concerns=[],
        not_verified=[],
        reasons=[],
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gh: FakeGh, **settings: Any
) -> tuple[StepContext, dict[str, Any]]:
    pushed: dict[str, Any] = {}
    backend = FakeBackend()
    monkeypatch.setattr(
        box_mod, "open_base", lambda s, log, phase="fix": Box.from_ref(backend, "base", log)
    )
    monkeypatch.setattr(branch_mod, "export_patch", lambda *a, **k: "From abc\n")

    def push(repo: str, patch: str, **kw: Any) -> branch_mod.PushedBranch:
        pushed.update(kw, repo=repo)
        return branch_mod.PushedBranch(
            head=f"warden/fix-{kw['issue']}-{kw['run_id']}", base=kw["base_branch"], sha="1" * 40
        )

    monkeypatch.setattr(branch_mod, "push_fix_branch", push)
    real_resolve, real_open = fork_mod.resolve_push_target, pr_mod.open_draft_pr
    monkeypatch.setattr(
        fork_mod,
        "resolve_push_target",
        lambda repo, **kw: real_resolve(repo, runner=gh, sleep=lambda _: None, **kw),
    )
    monkeypatch.setattr(pr_mod, "open_draft_pr", lambda *a, **kw: real_open(*a, runner=gh, **kw))
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        WARDEN_RUNS_DIR=tmp_path / "runs",
        WARDEN_TARGET_REPO=UPSTREAM,
        WARDEN_BASE_BRANCH="main",
        **settings,
    )
    log = EventLog(s.runs_dir, "r1")
    ctx = StepContext(
        settings=s,
        log=log,
        run_dir=log.dir,
        artifacts=log.dir / "artifacts",
        progress=NullProgress(),
        yes=True,
        json_out=False,
        requested_issue=1928,
    )
    return ctx, pushed


def _cand() -> Candidate:
    return Candidate(
        issue=1928, image_uuid="img-cand", tests_added=[], tool_turns=3, cost_usd=0.1, done=True
    )


def test_live_pr_without_push_access_goes_through_the_fork(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = FakeGh()
    ctx, pushed = _wire(monkeypatch, tmp_path, gh)
    out = live_pr(ctx, IssueRef(1928, "RRULE UNTIL"), _plan(["none"]), _cand(), _verdict())
    ctx.log.close()
    assert pushed["push_url"] == "https://github.com/moiz/icalendar.git"
    assert pushed["repo"] == UPSTREAM and pushed["base_branch"] == "main"
    create = next(c for c in gh.calls if c[1:3] == ["pr", "create"])
    assert create[create.index("--repo") + 1] == UPSTREAM
    assert create[create.index("--head") + 1] == "moiz:warden/fix-1928-r1"
    assert "--draft" in create and "merge" not in create
    assert out.url == "https://github.com/collective/icalendar/pull/1999"
    assert gh.body is not None and "## AI disclosure" in gh.body
    assert "## Warden evidence — #1928 RRULE UNTIL" in gh.body


def test_live_pr_fork_puts_pending_human_in_the_title_not_a_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = FakeGh()
    ctx, _ = _wire(monkeypatch, tmp_path, gh)
    out = live_pr(ctx, IssueRef(1928, "t"), _plan(["security"]), _cand(), _verdict())
    ctx.log.close()
    create = next(c for c in gh.calls if c[1:3] == ["pr", "create"])
    assert "--label" not in create  # a fork's author cannot label upstream PRs
    assert create[create.index("--title") + 1].startswith("[PENDING-HUMAN] [Warden] #1928")
    assert out.label == "PENDING-HUMAN"


def test_live_pr_with_push_access_pushes_to_origin_and_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = FakeGh(push="true")
    ctx, pushed = _wire(monkeypatch, tmp_path, gh)
    live_pr(ctx, IssueRef(1928, "t"), _plan(["money"]), _cand(), _verdict())
    ctx.log.close()
    assert pushed["push_url"] is None
    assert not any(c[1:3] == ["repo", "fork"] for c in gh.calls)
    create = next(c for c in gh.calls if c[1:3] == ["pr", "create"])
    assert create[create.index("--head") + 1] == "warden/fix-1928-r1"
    assert create[create.index("--label") + 1] == "PENDING-HUMAN"


def test_live_pr_uses_the_configured_template(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tpl = tmp_path / "pr.md"
    tpl.write_text("## Linked issue\n\n- Closes #$issue\n\n## Description\n\n$evidence\n")
    gh = FakeGh()
    ctx, _ = _wire(monkeypatch, tmp_path, gh, WARDEN_PR_TEMPLATE=str(tpl))
    out = live_pr(ctx, IssueRef(1928, "t"), _plan(["none"]), _cand(), _verdict())
    ctx.log.close()
    assert gh.body == out.body
    assert out.body.startswith("## Linked issue\n\n- Closes #1928\n")
    assert out.body.index("## Warden evidence") < out.body.index("## AI disclosure")


def test_live_pr_fail_verdict_touches_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = FakeGh()
    ctx, pushed = _wire(monkeypatch, tmp_path, gh)
    v = _verdict().model_copy(update={"decision": "FAIL"})
    out = live_pr(ctx, IssueRef(1928, "t"), _plan(["none"]), _cand(), v)
    ctx.log.close()
    assert gh.calls == [] and pushed == {} and out.url is None
    assert "## AI disclosure" in out.body
