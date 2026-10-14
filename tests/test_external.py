"""External repo base (`warden run --repo`): head lookup, install detection, build + cache. Offline:
the sandbox is FakeBackend, `git ls-remote` a fake runner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from warden.pipeline import _live_verify as live_verify  # the conftest swaps the module attribute
from warden.runlog.events import EventLog
from warden.sandbox.external import (
    DEFAULT_TEST_CMD,
    BaseBuildError,
    ExternalBase,
    RepoHead,
    detect_install,
    prepare_base,
    remote_head,
    valid_repo,
)
from warden.sandbox.fake import FakeBackend

SHA = "4de293d27c7155b2828f38a7589055ed1ab1b29a"
ICAL_PYPROJECT = """
[project]
name = "icalendar"
dependencies = ["python-dateutil", "tzdata"]

[dependency-groups]
dev = [{include-group = "test"}, "tox"]
test = ["coverage", "hypothesis", "pytest"]
"""


class RepoBackend(FakeBackend):
    """FakeBackend whose `git clone` lays down `repo_files` under /src and whose install exits
    `install_rc`."""

    def __init__(self, repo_files: dict[str, str], install_rc: int = 0) -> None:
        super().__init__()
        self.repo_files = repo_files
        self.install_rc = install_rc

    def run(self, image: Any, **kw: Any) -> tuple[str, str, int, Any | None, str]:
        shell = kw["shell"]
        if shell.startswith("git clone"):
            self.calls.append(shell)
            self.timeouts.append(kw["timeout"])
            child = self._new(image)
            child.files.update({f"/src/{k}": v.encode() for k, v in self.repo_files.items()})
            return "", "", 0, child, child.uuid
        if "pip install" in shell or "uv export" in shell:
            self.calls.append(shell)
            self.timeouts.append(kw["timeout"])
            if self.install_rc:
                return "", "ERROR: no matching distribution", self.install_rc, None, ""
            child = self._new(image)
            return "", "", 0, child, child.uuid
        return super().run(image, **kw)


def _ls_remote(stdout: str, rc: int = 0) -> Any:
    calls: list[list[str]] = []

    def runner(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="fatal: not found")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


# ---------------------------------------------------------------------------------- remote head
def test_remote_head_reads_default_branch_and_sha() -> None:
    runner = _ls_remote(f"ref: refs/heads/main\tHEAD\n{SHA}\tHEAD\n")
    head = remote_head("collective/icalendar", runner=runner)
    assert head == RepoHead(sha=SHA, branch="main")
    assert runner.calls == [
        ["git", "ls-remote", "--symref", "https://github.com/collective/icalendar.git", "HEAD"]
    ]


def test_remote_head_failure_is_a_base_build_error() -> None:
    with pytest.raises(BaseBuildError, match="cannot read o/missing"):
        remote_head("o/missing", runner=_ls_remote("", rc=128))


@pytest.mark.parametrize(
    ("repo", "ok"),
    [("collective/icalendar", True), ("a/b.c-d_e", True), ("nope", False), ("a/b/c", False),
     ("../x", False), ("a/..", False)],
)  # fmt: skip
def test_valid_repo(repo: str, ok: bool) -> None:
    assert valid_repo(repo) is ok


# ---------------------------------------------------------------------------- install detection
def test_dependency_group_uses_pip_group_after_upgrading_pip() -> None:
    cmd = detect_install(ICAL_PYPROJECT, ["pyproject.toml", "src", "tox.ini"])
    assert "-U 'pip>=25.1'" in cmd
    assert cmd.endswith("-e . --group test pytest")


def test_optional_extra() -> None:
    py = '[project]\nname = "x"\n[project.optional-dependencies]\ntests = ["pytest-mock"]\n'
    assert detect_install(py, ["pyproject.toml"]).endswith("-e '.[tests]' pytest")


def test_requirements_file_and_plain_package() -> None:
    cmd = detect_install("", ["setup.py", "requirements-dev.txt"])
    assert cmd.endswith("-e . -r requirements-dev.txt pytest")
    assert detect_install("", ["requirements.txt"]).endswith("-r requirements.txt pytest")
    assert detect_install('[project]\nname="x"\n', ["pyproject.toml"]).endswith("-e . pytest")


def test_uv_lock_exports_the_lock_and_falls_back_to_pip() -> None:
    cmd = detect_install(ICAL_PYPROJECT, ["pyproject.toml", "uv.lock"])
    uv, _, pip = cmd.partition(" || ")
    assert "uv export --frozen --no-hashes --no-emit-project --group test" in uv
    assert "-r /tmp/warden-req.txt -e . pytest" in uv
    assert "--group test pytest" in pip


def test_malformed_pyproject_is_ignored() -> None:
    assert detect_install("[[[", ["pyproject.toml"]).endswith("-e . pytest")


# ------------------------------------------------------------------------------- build + cache
def test_build_clones_at_sha_installs_and_caches(tmp_path: Path) -> None:
    backend = RepoBackend({"pyproject.toml": ICAL_PYPROJECT, "src/icalendar/__init__.py": ""})
    log = EventLog(tmp_path / "runs", "r1")
    cache = tmp_path / "runs" / ".bases.json"
    head = RepoHead(sha=SHA, branch="main")

    base = prepare_base(backend, log, "collective/icalendar", head, cache_path=cache)
    assert not base.cached
    assert base.sha == SHA and base.branch == "main" and base.test_cmd == DEFAULT_TEST_CMD
    assert base.install_cmd.endswith("--group test pytest")
    clone = backend.calls[0]
    assert clone.startswith("git clone -q https://github.com/collective/icalendar.git /src")
    assert f"git checkout -q {SHA}" in clone
    assert backend.calls[-1] == base.install_cmd
    assert backend.images[base.image]  # the install checkpoint is the base
    assert base.overrides() == {
        "base_image": base.image,
        "workdir": "/src",
        "shell_wrap": "sh -c",
        "gate_profile": "swe",
        "target_repo": "collective/icalendar",
        "base_branch": "main",
        "test_cmd": DEFAULT_TEST_CMD,
    }
    assert json.loads(cache.read_text())[f"collective/icalendar@{SHA}"]["image"] == base.image

    n = len(backend.calls)
    again = prepare_base(
        backend, log, "collective/icalendar", head, cache_path=cache, test_cmd="pytest -x"
    )
    assert again.cached and again.image == base.image and again.test_cmd == "pytest -x"
    assert len(backend.calls) == n  # no sandbox work on a cache hit
    log.close()


def test_new_sha_or_explicit_install_rebuilds(tmp_path: Path) -> None:
    backend = RepoBackend({"pyproject.toml": ICAL_PYPROJECT})
    log = EventLog(tmp_path / "runs", "r1")
    cache = tmp_path / ".bases.json"
    a = prepare_base(backend, log, "o/r", RepoHead(SHA, "main"), cache_path=cache)
    b = prepare_base(backend, log, "o/r", RepoHead("f" * 40, "main"), cache_path=cache)
    c = prepare_base(
        backend, log, "o/r", RepoHead(SHA, "main"), cache_path=cache, install_cmd="pip install ."
    )
    assert len({a.image, b.image, c.image}) == 3
    assert c.install_cmd == "pip install ." and not c.cached
    assert backend.calls[-1] == "pip install ."
    log.close()


def test_install_failure_raises_with_the_override_hint(tmp_path: Path) -> None:
    backend = RepoBackend({"pyproject.toml": ICAL_PYPROJECT}, install_rc=1)
    log = EventLog(tmp_path / "runs", "r1")
    with pytest.raises(BaseBuildError, match="install failed") as err:
        prepare_base(backend, log, "o/r", RepoHead(SHA, "main"), cache_path=tmp_path / "c.json")
    assert "--install-cmd" in err.value.fix
    assert not (tmp_path / "c.json").exists()  # a failed build is never cached
    log.close()


def test_test_cmd_drives_the_gate_suite_and_the_regression_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import warden.pipeline as pipeline
    from warden.config import Settings
    from warden.schemas import Candidate, Plan

    seen: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(
        pipeline, "verify_candidate", lambda ctx, cand, cmds, **kw: seen.append((cmds, kw))
    )
    plan = Plan(
        issue=1928, summary="s", files_to_touch=[], tests_to_add=[], landing_order=[],
        risk_class=["none"], citations=[], acceptance=[],
    )  # fmt: skip
    cand = Candidate(issue=1928, image_uuid="c", tool_turns=1, tests_added=[], cost_usd=0)

    def ctx(**env: Any) -> pipeline.StepContext:
        s = Settings(_env_file=None, WARDEN_RUNS_DIR=tmp_path, **env)  # type: ignore[call-arg]
        log = EventLog(tmp_path, "v")
        return pipeline.StepContext(
            settings=s, log=log, run_dir=log.dir, artifacts=log.dir / "artifacts",
            progress=pipeline.NullProgress(), yes=False, json_out=False, requested_issue=1928,
        )  # fmt: skip

    live_verify(ctx(WARDEN_GATE_PROFILE="swe", WARDEN_TEST_CMD=DEFAULT_TEST_CMD), plan, cand)
    cmds, kw = seen[-1]
    assert cmds.suite == DEFAULT_TEST_CMD
    assert kw["regression_cmd"] == f"{DEFAULT_TEST_CMD} -rA"
    assert kw["suite_timeout"] == pipeline.EXTERNAL_SUITE_TIMEOUT

    live_verify(ctx(WARDEN_GATE_PROFILE="fabric"), plan, cand)  # unset: the profile, unchanged
    cmds, kw = seen[-1]
    assert cmds.suite.startswith("cd backend && pytest") and "regression_cmd" not in kw


def test_external_base_json_round_trip() -> None:
    base = ExternalBase("o/r", SHA, "dev", "img-1", "pip install .", "pytest", cached=True)
    back = ExternalBase.from_json(base.to_json())
    assert back.overrides() == base.overrides() and back.branch == "dev"
