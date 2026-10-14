"""Loop over Nebius preloaded SWE-rebench images: plan → fix → verify → hidden tests → results.csv.

Each task's image (`swerebench/sweb.eval.x86_64.<owner>_1776_<repo>-<n>:latest`, preloaded in
Sandboxes) holds the repo at `/testbed` on the task's base commit, with the task's Python in the
conda env `/opt/miniconda3/envs/testbed` (not on PATH by default). Warden sees only the issue text
(`problem_statement`); the hidden tests (`test_patch`, FAIL_TO_PASS, PASS_TO_PASS) enter a box
only after the verdict, for grading — so `false_pass` (Warden said PASS, the hidden tests say
no) measures the verifier, not a leak.

Grading follows the official harness: reset the files `test_patch` touches to the base commit,
apply it, run FAIL_TO_PASS + PASS_TO_PASS with the task's `test_cmd`; resolved = all passed.
"""

from __future__ import annotations

import csv
import json
import random
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from warden.config import Settings
from warden.hunt.issue import IssueText
from warden.runlog.events import EventLog
from warden.sandbox.box import Box, SandboxBackend

DATASET = "nebius/SWE-rebench"
SPLIT = "filtered"
ROWS_URL = "https://datasets-server.huggingface.co/rows"
PAGE = 100  # datasets-server maximum

WORKDIR = "/testbed"
ENV_BINS = ("/opt/miniconda3/envs/testbed/bin", "/opt/conda/envs/testbed/bin")  # images vary
WRAPPER = "/usr/local/bin/testbed-env"
WRAP = "testbed-env sh -c"
SHIM_DIR = "/usr/local/warden-bin"
WRAPPER_SCRIPT = (
    "#!/bin/sh\n"
    "# Warden: run a command with the task's conda env first on PATH (when the image has one).\n"
    + "".join(
        f'if [ -d {d} ]; then PATH="{d}:$PATH"; export PATH; fi\n' for d in reversed(ENV_BINS)
    )
    + f'PATH="{SHIM_DIR}:$PATH"; export PATH\n'
    + 'exec "$@"\n'
)
# Some images ship a `pytest` whose shebang names a different env than the one the package is
# installed in (PennyLaneAI__pennylane-5831: plain `pytest` cannot import pennylane, while
# `python -m pytest` can). Every pytest call goes through the env's python instead.
PYTEST_SHIM = '#!/bin/sh\nexec python -m pytest "$@"\n'
TEST_PATCH = "/tmp/warden-test.patch"
GRADE_TIMEOUT = 1800
INSTALL_TIMEOUT = 900

CSV_COLUMNS = (
    "task_id",
    "resolved",
    "verdict",
    "false_pass",
    "theater_catch",
    "tool_turns",
    "usd",
    "seconds",
    "error",
)

_DIFF_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)
_STATUSES = ("PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL", "XPASS")


class Task(BaseModel):
    instance_id: str
    repo: str
    base_commit: str
    image: str
    problem_statement: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    test_patch: str
    test_cmd: str
    install: str = ""  # re-run in the base: some images' editable installs point elsewhere

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Task:
        def ids(value: Any) -> list[str]:
            return [str(v) for v in (json.loads(value) if isinstance(value, str) else value or [])]

        config = row.get("install_config") or {}
        if isinstance(config, str):
            config = json.loads(config)
        return cls(
            instance_id=row["instance_id"],
            repo=row["repo"],
            base_commit=row["base_commit"],
            image=row.get("image_name") or row["docker_image"],
            problem_statement=row["problem_statement"],
            fail_to_pass=ids(row["FAIL_TO_PASS"]),
            pass_to_pass=ids(row["PASS_TO_PASS"]),
            test_patch=row["test_patch"],
            test_cmd=config.get("test_cmd") or "pytest -rA",
            # --no-deps: the env is already pinned; only re-point the package at /testbed
            install=(config.get("install") or "").replace("pip install ", "pip install --no-deps "),
        )

    @property
    def tag(self) -> str:
        return self.image if ":" in self.image.rsplit("/", 1)[-1] else f"{self.image}:latest"

    def issue_text(self) -> IssueText:
        """The problem statement as an issue: first non-blank line is the title."""
        lines = self.problem_statement.strip().splitlines() or [self.instance_id]
        title = lines[0].lstrip("#").strip() or self.instance_id
        return IssueText(
            number=0, title=title[:200], body="\n".join(lines[1:]).strip(), comments=[]
        )


# --------------------------------------------------------------------------- dataset


def fetch_rows(
    *, offset: int, length: int = PAGE, split: str = SPLIT, get: Callable[..., Any] | None = None
) -> list[dict[str, Any]]:
    """One page of dataset rows from the HF datasets-server (public, no token)."""
    if get is None:
        import httpx

        get = httpx.get
    params = {"dataset": DATASET, "config": "default", "split": split}
    resp = get(ROWS_URL, params={**params, "offset": offset, "length": length}, timeout=60)
    resp.raise_for_status()
    return [r["row"] for r in resp.json()["rows"]]


KEEP = (
    "instance_id",
    "repo",
    "base_commit",
    "image_name",
    "docker_image",
    "problem_statement",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "test_patch",
    "install_config",
)  # the gold `patch` is never cached: nothing downstream may see it


def num_rows(*, split: str = SPLIT, get: Callable[..., Any] | None = None) -> int:
    if get is None:
        import httpx

        get = httpx.get
    resp = get(
        "https://datasets-server.huggingface.co/size", params={"dataset": DATASET}, timeout=60
    )
    resp.raise_for_status()
    splits = resp.json()["size"]["splits"]
    return int(next(x["num_rows"] for x in splits if x["split"] == split))


def pick_offsets(total: int, n: int, *, seed: int) -> list[int]:
    """`n` row offsets out of `total`, fixed by `seed`, ascending."""
    return sorted(random.Random(seed).sample(range(total), min(n, total)))


def fetch_sample(
    path: Path, n: int, *, seed: int, split: str = SPLIT, get: Callable[..., Any] | None = None
) -> list[Task]:
    """Sample `n` tasks by seed and cache them (without the gold patch) as JSONL at `path`."""
    offsets = pick_offsets(num_rows(split=split, get=get), n, seed=seed)
    rows = [fetch_rows(offset=o, length=1, split=split, get=get)[0] for o in offsets]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps({k: row[k] for k in KEEP if k in row}) + "\n")
    return load_tasks(path)


def load_tasks(path: Path) -> list[Task]:
    with path.open(encoding="utf-8") as fh:
        return [Task.from_row(json.loads(line)) for line in fh if line.strip()]


# --------------------------------------------------------------------------- box


def prepare_base(backend: SandboxBackend, task: Task, log: EventLog) -> Box:
    """The task image plus the `testbed-env` wrapper, as a kept checkpoint (the run's base)."""
    image = Box.from_ref(backend, task.tag, log, phase="hunt")
    res = image.run(
        f"chmod +x {WRAPPER} {SHIM_DIR}/pytest",
        cwd="/",
        keep=True,
        files={WRAPPER: WRAPPER_SCRIPT.encode(), f"{SHIM_DIR}/pytest": PYTEST_SHIM.encode()},
    )
    if not res.ok or res.box is None:
        raise RuntimeError(f"could not prepare {task.tag}: {res.stderr.strip()[:200]}")
    if not task.install:
        return res.box
    # Some images were built with the repo elsewhere (SpikeInterface: the editable .pth names
    # /spikeinterface/src), so the package does not import from /testbed until reinstalled.
    inst = res.box.run(
        f"{WRAP} {shlex.quote(task.install)}", cwd=WORKDIR, keep=True, timeout=INSTALL_TIMEOUT
    )
    if not inst.ok or inst.box is None:
        tail = (inst.stdout + inst.stderr).strip()[-300:]
        raise RuntimeError(f"install failed in {task.tag} ({task.install}): {tail}")
    return inst.box


def task_settings(settings: Settings, task: Task, base_uuid: str) -> Settings:
    return settings.model_copy(
        update={
            "base_image": base_uuid,
            "workdir": WORKDIR,
            "shell_wrap": WRAP,
            "gate_profile": "swe",
            "target_repo": task.repo,
        }
    )


def suite_command(task: Task, tests_added: list[str]) -> str:
    """The repo's own test command over the test files the candidate added or extended."""
    files = sorted({t.split("::", 1)[0] for t in tests_added})
    return f"{task.test_cmd} {' '.join(shlex.quote(f) for f in files)}" if files else ""


# --------------------------------------------------------------------------- grading


def test_files(patch: str) -> list[str]:
    """Paths a unified diff touches (the `b/` side), in order, deduplicated."""
    seen: list[str] = []
    for _, b in _DIFF_HEADER.findall(patch):
        if b not in seen:
            seen.append(b)
    return seen


def grade_command(task: Task) -> str:
    """Reset the hidden-test files to the base commit (a file new in the patch is removed), apply
    the test patch, run the task's test command on those files — as the official harness does.
    Not on the FAIL_TO_PASS / PASS_TO_PASS ids: the dataset cuts them at the first space
    (`test_label[op0-None-Exp((2+3j)`), and pytest refuses the whole run on one bad id."""
    files = test_files(task.test_patch)
    parts = []
    for f in files:
        q = shlex.quote(f)
        parts.append(f"{{ git checkout -q {task.base_commit} -- {q} 2>/dev/null || rm -f {q}; }}")
    parts.append(f"git apply {TEST_PATCH}")
    parts.append(f"{task.test_cmd} {' '.join(shlex.quote(f) for f in files)}")
    return " && ".join(parts)


def parse_test_lines(output: str) -> dict[str, str]:
    """`-rA` summary lines → {test id: outcome}, the id cut at the first space like the
    dataset's own ids (SWE-bench's parse_log_pytest)."""
    outcomes: dict[str, str] = {}
    for line in output.splitlines():
        words = line.split()
        if len(words) >= 2 and words[0] in _STATUSES:
            outcomes[words[1]] = words[0].lower()
    return outcomes


@dataclass(frozen=True)
class Grade:
    resolved: bool
    f2p_passed: int
    p2p_passed: int
    exit_code: int
    applied: bool
    output: str = ""  # tail of the grading run, for results that need explaining


def grade(candidate: Box, task: Task) -> Grade:
    res = candidate.run(
        f"{WRAP} {shlex.quote(grade_command(task))}",
        cwd=WORKDIR,
        keep=False,
        timeout=GRADE_TIMEOUT,
        files={TEST_PATCH: task.test_patch.encode()},
    )
    outcomes = parse_test_lines(res.stdout)
    f2p = sum(outcomes.get(t) == "passed" for t in task.fail_to_pass)
    p2p = sum(outcomes.get(t) == "passed" for t in task.pass_to_pass)
    return Grade(
        resolved=f2p == len(task.fail_to_pass) and p2p == len(task.pass_to_pass),
        f2p_passed=f2p,
        p2p_passed=p2p,
        exit_code=res.exit_code,
        applied=bool(outcomes) or res.exit_code in (0, 1),
        output=(res.stdout + res.stderr)[-4000:],
    )


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class Row:
    task_id: str
    resolved: bool
    verdict: str
    false_pass: bool
    theater_catch: bool
    tool_turns: int
    usd: float
    seconds: int
    error: str = ""

    def csv(self) -> dict[str, str]:
        d = asdict(self)
        return {
            k: (str(int(v)) if isinstance(v, bool) else f"{v:.4f}" if k == "usd" else str(v))
            for k, v in d.items()
        }


def make_row(
    task: Task,
    *,
    verdict: str,
    tests_added: int,
    theater_failed_on_base: bool,
    theater_inconclusive: bool,
    tool_turns: int,
    resolved: bool,
    usd: float,
    seconds: float,
) -> Row:
    return Row(
        task_id=task.instance_id,
        resolved=resolved,
        verdict=verdict,
        false_pass=verdict in ("PASS", "PASS_WITH_CONCERNS") and not resolved,
        # Warden's own tests passed on the unfixed base: the theater check rejected them.
        theater_catch=tests_added > 0 and not theater_failed_on_base and not theater_inconclusive,
        tool_turns=tool_turns,
        usd=round(usd, 4),
        seconds=int(seconds),
    )


def error_row(task: Task, err: BaseException, *, usd: float, seconds: float) -> Row:
    return Row(
        task_id=task.instance_id,
        resolved=False,
        verdict="ERROR",
        false_pass=False,
        theater_catch=False,
        tool_turns=0,
        usd=round(usd, 4),
        seconds=int(seconds),
        error=f"{type(err).__name__}: {str(err)[:200]}".replace("\n", " "),
    )


def done_ids(csv_path: Path) -> set[str]:
    if not csv_path.exists():
        return set()
    with csv_path.open(encoding="utf-8", newline="") as fh:
        return {r["task_id"] for r in csv.DictReader(fh)}


def append_row(csv_path: Path, row: Row) -> None:
    new = not csv_path.exists()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        if new:
            w.writeheader()
        w.writerow(row.csv())


# --------------------------------------------------------------------------- loop


def run_task(task: Task, settings: Settings, *, progress: Any = None) -> Row:
    """plan → fix → verify on the task image, then grade the candidate with the hidden tests.
    Raises ProviderBudgetError (the account is out of money: stop the eval); every other error
    becomes an ERROR row."""
    from warden.llm.client import ProviderBudgetError
    from warden.pipeline import IssueRef, live_steps, run_pipeline, verify_candidate
    from warden.sandbox.box import ContreeBackend
    from warden.schemas import Candidate, Verdict
    from warden.verify.gate import SWE_COMMANDS

    t0 = time.perf_counter()
    log = EventLog(settings.runs_dir)
    try:
        backend = ContreeBackend(
            settings.nebius_api_key, settings.contree_base_url, settings.nebius_project_id
        )
        base = prepare_base(backend, task, log)
        s = task_settings(settings, task, base.uuid)
        issue = task.issue_text()
        steps = replace(
            live_steps(),
            hunt=lambda ctx: IssueRef(issue=0, title=issue.title),
            verify=lambda ctx, plan, cand: verify_candidate(
                ctx, cand, replace(SWE_COMMANDS, suite=suite_command(task, cand.tests_added))
            ),
        )
        run_pipeline(
            s,
            log,
            steps,
            start_after=None,
            json_out=False,
            yes=True,
            progress=progress,
            stop_after="verify",
            issue=issue,
        )
        artifacts = log.dir / "artifacts"
        cand = Candidate.model_validate_json((artifacts / "candidate.json").read_text("utf-8"))
        verdict = Verdict.model_validate_json((artifacts / "verdict.json").read_text("utf-8"))
        g = grade(base.sibling(cand.image_uuid), task)
        log.artifact("grade.txt", f"$ {grade_command(task)}\n\n{g.output}")
        summary = {k: v for k, v in asdict(g).items() if k != "output"}
        log.emit("verify", "note", {"grade": summary, "task": task.instance_id})
        return make_row(
            task,
            verdict=verdict.decision,
            tests_added=len(cand.tests_added),
            theater_failed_on_base=verdict.theater_failed_on_base,
            theater_inconclusive=verdict.theater_inconclusive,
            tool_turns=cand.tool_turns,
            resolved=g.resolved,
            usd=_spent(log),
            seconds=time.perf_counter() - t0,
        )
    except ProviderBudgetError:
        raise
    except Exception as err:  # noqa: BLE001 — one task's failure is a row, not the end of the eval
        log.emit("verify", "error", {"task": task.instance_id, "error": str(err)[:500]})
        return error_row(task, err, usd=_spent(log), seconds=time.perf_counter() - t0)


def _spent(log: EventLog) -> float:
    from warden.pipeline import _spent as spent

    return spent(log.dir)


def run_eval(
    tasks: list[Task],
    settings: Settings,
    *,
    csv_path: Path,
    budget_usd: float,
    on_row: Callable[[Row], None] | None = None,
    task_runner: Callable[[Task, Settings], Row] | None = None,
) -> list[Row]:
    """Run `tasks` in order, appending each row to `csv_path`; tasks already there are skipped.
    Stops before a task when the eval's spend plus one task's cap would exceed `budget_usd`."""
    runner = task_runner or (lambda t, s: run_task(t, s))
    done = done_ids(csv_path)
    spent = 0.0
    rows: list[Row] = []
    for task in tasks:
        if task.instance_id in done:
            continue
        if spent + settings.max_usd_per_issue > budget_usd:
            break
        row = runner(task, settings)
        append_row(csv_path, row)
        rows.append(row)
        spent += row.usd
        if on_row is not None:
            on_row(row)
    return rows
