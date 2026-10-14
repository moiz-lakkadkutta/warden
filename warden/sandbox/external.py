"""Base checkpoint for an external public repo (`warden run --repo owner/name`).

python:3.12 → clone the repo at its default-branch head → install it with its test dependencies.
The resulting checkpoint is cached by (repo, sha, install command) in runs/.bases.json, so a second
run on the same head starts in seconds. The head comes from `git ls-remote` on the host; the clone
itself runs inside the sandbox (a public repo needs no credentials, and none enter a box).

Install detection reads only pyproject.toml and the root listing (no recursive globs):
  uv.lock              → `uv export` the locked deps (+ test group/extra), pip install them
  [dependency-groups]  → pip ≥ 25.1 `--group <test|tests|testing|dev>`
  optional extras      → `pip install -e '.[test]'`
  requirements-*.txt   → `-r` the test/dev one
pytest is always added; `--install-cmd` overrides all of it.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from warden.runlog.events import EventLog
from warden.sandbox.box import Box, SandboxBackend

Runner = Callable[..., subprocess.CompletedProcess[str]]

IMAGE = "python:3.12"
WORKDIR = "/src"
WRAP = "sh -c"
DEFAULT_TEST_CMD = "python -m pytest -q -p no:cacheprovider"
TEST_GROUPS = ("test", "tests", "testing", "dev")
REQ_FILES = (
    "requirements-test.txt",
    "requirements-tests.txt",
    "test-requirements.txt",
    "requirements_test.txt",
    "requirements-dev.txt",
    "dev-requirements.txt",
    "requirements_dev.txt",
)
_PIP = "python -m pip install -q --root-user-action=ignore --disable-pip-version-check"
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class BaseBuildError(RuntimeError):
    """`str(err)` is the human sentence; `fix` the next step."""

    def __init__(self, message: str, *, fix: str) -> None:
        super().__init__(message)
        self.fix = fix


@dataclass(frozen=True)
class RepoHead:
    sha: str
    branch: str


@dataclass(frozen=True)
class ExternalBase:
    repo: str
    sha: str
    branch: str
    image: str  # checkpoint uuid
    install_cmd: str
    test_cmd: str
    cached: bool = False

    def overrides(self) -> dict[str, Any]:
        """Settings fields for a run on this base (model_copy(update=...))."""
        return {
            "base_image": self.image,
            "workdir": WORKDIR,
            "shell_wrap": WRAP,
            "gate_profile": "swe",
            "target_repo": self.repo,
            "base_branch": self.branch,
            "test_cmd": self.test_cmd,
        }

    def to_json(self) -> str:
        data = asdict(self)
        data.pop("cached")
        return json.dumps(data, indent=2)

    @classmethod
    def from_json(cls, text: str) -> ExternalBase:
        data = json.loads(text)
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


def valid_repo(repo: str) -> bool:
    return bool(_REPO.match(repo)) and ".." not in repo


def remote_head(repo: str, *, runner: Runner = subprocess.run) -> RepoHead:
    """Default branch and its head sha, from `git ls-remote --symref` (no auth, no clone)."""
    url = f"https://github.com/{repo}.git"
    res = runner(
        ["git", "ls-remote", "--symref", url, "HEAD"], capture_output=True, text=True, check=False
    )
    if res.returncode != 0:
        raise BaseBuildError(
            f"cannot read {repo}'s default branch: {(res.stderr or '').strip()[-200:]}",
            fix=f"check that https://github.com/{repo} exists and is public",
        )
    branch = sha = ""
    for line in (res.stdout or "").splitlines():
        ref, _, name = line.partition("\t")
        if ref.startswith("ref: refs/heads/") and name == "HEAD":
            branch = ref.removeprefix("ref: refs/heads/")
        elif name == "HEAD" and re.fullmatch(r"[0-9a-f]{40}", ref):
            sha = ref
    if not sha or not branch:
        raise BaseBuildError(
            f"git ls-remote printed no HEAD for {repo}", fix="re-run; or pass a different --repo"
        )
    return RepoHead(sha=sha, branch=branch)


def _pyproject(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text) if text else {}
    except tomllib.TOMLDecodeError:
        return {}


def _first(names: Iterable[str], present: Iterable[str]) -> str | None:
    have = set(present)
    return next((n for n in names if n in have), None)


def detect_install(pyproject: str, files: Iterable[str]) -> str:
    """The install command for a repo with this pyproject.toml text and root file listing."""
    root = set(files)
    data = _pyproject(pyproject)
    groups = data.get("dependency-groups") or {}
    extras = (data.get("project") or {}).get("optional-dependencies") or {}
    group = _first(TEST_GROUPS, groups)
    extra = _first(TEST_GROUPS, extras)
    installable = "pyproject.toml" in root or "setup.py" in root or "setup.cfg" in root

    if group:
        pip_cmd = f"{_PIP} -U 'pip>=25.1' && {_PIP} -e . --group {group} pytest"
    elif extra:
        pip_cmd = f"{_PIP} -e {shlex.quote(f'.[{extra}]')} pytest"
    else:
        req = _first(REQ_FILES, root)
        parts = (["-e ."] if installable else []) + ([f"-r {req}"] if req else [])
        if not parts and "requirements.txt" in root:
            parts = ["-r requirements.txt"]
        pip_cmd = f"{_PIP} {' '.join(parts + ['pytest'])}"

    if "uv.lock" in root and "pyproject.toml" in root:
        sel = f" --group {group}" if group else (f" --extra {extra}" if extra else "")
        uv_cmd = (
            f"{_PIP} uv && uv export --frozen --no-hashes --no-emit-project{sel} "
            f"-o /tmp/warden-req.txt && {_PIP} -r /tmp/warden-req.txt -e . pytest"
        )
        return f"{{ {uv_cmd}; }} || {{ {pip_cmd}; }}"
    return pip_cmd


def detect_test_cmd(pyproject: str, files: Iterable[str]) -> str:
    """pytest, which also collects unittest suites; the repo's own pytest config applies."""
    return DEFAULT_TEST_CMD


def _clone_cmd(repo: str, sha: str) -> str:
    url = shlex.quote(f"https://github.com/{repo}.git")
    return (
        f"git clone -q {url} {WORKDIR} && cd {WORKDIR} && git checkout -q {sha} && "
        "git submodule update -q --init --recursive && "
        "git config user.email warden@localhost && git config user.name warden"
    )


def _load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def prepare_base(
    backend: SandboxBackend,
    log: EventLog,
    repo: str,
    head: RepoHead,
    *,
    cache_path: Path,
    install_cmd: str | None = None,
    test_cmd: str | None = None,
    install_timeout: int = 900,
) -> ExternalBase:
    """The cached checkpoint for (repo, head.sha, install) or a fresh build of it.

    `install_cmd=None` detects the install from the clone (cached under repo@sha); an explicit one
    gets its own cache entry. `test_cmd=None` uses the detected test command."""
    cache = _load_cache(cache_path)
    key = f"{repo}@{head.sha}" + ("" if install_cmd is None else f"|{install_cmd}")
    if key in cache:
        hit = ExternalBase.from_json(json.dumps(cache[key]))
        return replace(hit, test_cmd=test_cmd or hit.test_cmd, cached=True)

    box = Box.from_ref(backend, IMAGE, log, phase="hunt")
    cloned = box.run(_clone_cmd(repo, head.sha), cwd="/", keep=True, timeout=600)
    if not cloned.ok or cloned.box is None:
        raise BaseBuildError(
            f"could not clone {repo} at {head.sha[:7]} in the sandbox: "
            f"{(cloned.stdout + cloned.stderr).strip()[-300:]}",
            fix="check the repo is public; re-run",
        )
    src = cloned.box
    listing = src.run("ls -1a", cwd=WORKDIR, keep=False).stdout.split()
    pyproject = (
        src.run("cat pyproject.toml", cwd=WORKDIR, keep=False).stdout
        if "pyproject.toml" in listing
        else ""
    )
    detected_test = detect_test_cmd(pyproject, listing)
    install = install_cmd if install_cmd is not None else detect_install(pyproject, listing)
    log.note("hunt", f"base: install {install}")
    inst = src.run(install, cwd=WORKDIR, keep=True, timeout=install_timeout)
    if not inst.ok or inst.box is None:
        raise BaseBuildError(
            f"install failed for {repo} (exit {inst.exit_code}): "
            f"{(inst.stdout + inst.stderr).strip()[-400:]}",
            fix="pass --install-cmd '<the repo's dev install>' (see its CONTRIBUTING)",
        )
    built = ExternalBase(
        repo=repo,
        sha=head.sha,
        branch=head.branch,
        image=inst.box.uuid,
        install_cmd=install,
        test_cmd=detected_test,
    )
    cache[key] = json.loads(built.to_json())
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_name(cache_path.name + ".tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(cache_path)
    return replace(built, test_cmd=test_cmd or built.test_cmd)
