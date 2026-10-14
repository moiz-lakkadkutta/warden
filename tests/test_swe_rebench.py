from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from warden.config import Settings
from warden.eval import swe_rebench as swe
from warden.runlog.events import EventLog
from warden.sandbox.box import Box, RunResult
from warden.sandbox.fake import FakeBackend
from warden.schemas import Candidate
from warden.verify.gate import SWE_COMMANDS, run_gate
from warden.verify.tail import keep_tail
from warden.verify.theater import TheaterResult
from warden.verify.verifier import decide

TEST_PATCH = """diff --git a/tests/test_utils.py b/tests/test_utils.py
index 0f2d696..83b4140 100644
--- a/tests/test_utils.py
+++ b/tests/test_utils.py
@@ -1 +1 @@
-a
+b
diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1 @@
+def test_x(): pass
"""

ROW: dict[str, Any] = {
    "instance_id": "0b01001001__spectree-64",
    "repo": "0b01001001/spectree",
    "base_commit": "a091fab",
    "image_name": "swerebench/sweb.eval.x86_64.0b01001001_1776_spectree-64",
    "docker_image": "swerebench/sweb.eval.x86_64.0b01001001_1776_spectree-64",
    "problem_statement": "### [BUG] description for query params\nHi, it does not show.\n",
    "FAIL_TO_PASS": ["tests/test_utils.py::test_parse_params"],
    # the dataset cuts ids at the first space: pytest's `test_p[a b]` is listed as `test_p[a`
    "PASS_TO_PASS": ["tests/test_utils.py::test_comments", "tests/test_new.py::test_p[a"],
    "test_patch": TEST_PATCH,
    "install_config": {
        "test_cmd": "pytest --no-header -rA --tb=line -p no:cacheprovider",
        "install": "pip install -e .[dev]",
    },
    "patch": "diff --git a/gold b/gold",  # the gold patch: must never be cached
}


@pytest.fixture
def log(tmp_path: Path) -> Iterator[EventLog]:
    log = EventLog(tmp_path / "runs", "t")
    yield log
    log.close()


def test_task_from_row_accepts_lists_or_json_strings() -> None:
    t = swe.Task.from_row(ROW)
    s = swe.Task.from_row({**ROW, "FAIL_TO_PASS": json.dumps(ROW["FAIL_TO_PASS"])})
    assert t.fail_to_pass == s.fail_to_pass == ["tests/test_utils.py::test_parse_params"]
    assert t.tag == ROW["image_name"] + ":latest"
    assert t.test_cmd.startswith("pytest --no-header -rA")
    issue = t.issue_text()
    assert issue.title == "[BUG] description for query params"
    assert issue.body == "Hi, it does not show."
    assert issue.comments == []


def test_issue_text_has_no_hidden_test_information() -> None:
    issue = swe.Task.from_row(ROW).issue_text()
    text = issue.title + issue.body
    assert "test_parse_params" not in text and "diff --git" not in text


def test_pick_offsets_is_fixed_by_seed_and_sorted() -> None:
    a = swe.pick_offsets(6542, 10, seed=7)
    assert a == swe.pick_offsets(6542, 10, seed=7) == sorted(a)
    assert a != swe.pick_offsets(6542, 10, seed=8)
    assert len(set(a)) == 10
    assert swe.pick_offsets(3, 10, seed=7) == [0, 1, 2]


class _Resp:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._body


def test_fetch_sample_caches_tasks_without_the_gold_patch(tmp_path: Path) -> None:
    def get(url: str, params: dict[str, Any], timeout: int) -> _Resp:
        if url.endswith("/size"):
            return _Resp({"size": {"splits": [{"split": "filtered", "num_rows": 3}]}})
        row = {**ROW, "instance_id": f"o__r-{params['offset']}"}
        return _Resp({"rows": [{"row": row}]})

    path = tmp_path / "tasks.jsonl"
    tasks = swe.fetch_sample(path, 2, seed=1, get=get)
    assert len(tasks) == 2
    cached = [json.loads(line) for line in path.read_text().splitlines()]
    assert all("patch" not in row for row in cached)
    assert [t.instance_id for t in swe.load_tasks(path)] == [t.instance_id for t in tasks]


def test_test_files_reads_the_b_side_once() -> None:
    assert swe.test_files(TEST_PATCH) == ["tests/test_utils.py", "tests/test_new.py"]
    assert swe.test_files(TEST_PATCH + TEST_PATCH) == ["tests/test_utils.py", "tests/test_new.py"]


def test_grade_command_resets_hidden_test_files_then_applies_and_runs() -> None:
    cmd = swe.grade_command(swe.Task.from_row(ROW))
    reset = (
        "git checkout -q a091fab -- tests/test_utils.py 2>/dev/null || rm -f tests/test_utils.py"
    )
    assert reset in cmd
    assert cmd.index(reset) < cmd.index("git apply /tmp/warden-test.patch") < cmd.index("pytest")
    # the test command runs on the patch's files, never on the (cut-off) ids
    assert cmd.endswith("-p no:cacheprovider tests/test_utils.py tests/test_new.py")
    assert "test_parse_params" not in cmd and "test_p[a" not in cmd


def test_grade_command_resets_data_files_but_does_not_run_them() -> None:
    # IAMconsortium__nomenclature-431: the test patch adds a YAML fixture; passing it to pytest
    # makes the whole run "ERROR: not found", so nothing passes even with the gold patch
    data = "diff --git a/tests/data/v.yaml b/tests/data/v.yaml\nnew file mode 100644\n"
    cmd = swe.grade_command(swe.Task.from_row({**ROW, "test_patch": TEST_PATCH + data}))
    assert "rm -f tests/data/v.yaml" in cmd
    assert cmd.endswith("-p no:cacheprovider tests/test_utils.py tests/test_new.py")


def test_parse_test_lines_cuts_ids_at_the_first_space_like_the_dataset() -> None:
    out = (
        "PASSED tests/test_x.py::test_p[a b]\n"
        "FAILED tests/test_utils.py::test_parse_params - AssertionError: assert {'in': 1}\n"
        "ERROR tests/test_y.py::test_e\n"
        "1 failed, 1 passed in 0.5s\n"
    )
    assert swe.parse_test_lines(out) == {
        "tests/test_x.py::test_p[a": "passed",
        "tests/test_utils.py::test_parse_params": "failed",
        "tests/test_y.py::test_e": "error",
    }


class _StubBox:
    def __init__(self, stdout: str, code: int) -> None:
        self.stdout, self.code = stdout, code
        self.calls: list[dict[str, Any]] = []

    def run(self, shell: str, **kw: Any) -> RunResult:
        self.calls.append({"shell": shell, **kw})
        return RunResult(self.stdout, "", self.code, None, 1)


def _passed(*ids: str) -> str:  # what pytest prints: the full id, `[a b]` included
    return "".join(f"PASSED {i.replace('[a', '[a b]')}\n" for i in ids)


def test_grade_resolved_only_when_every_listed_test_passes() -> None:
    task = swe.Task.from_row(ROW)
    everything = task.fail_to_pass + task.pass_to_pass
    box = _StubBox(_passed(*everything), 0)
    g = swe.grade(box, task)  # type: ignore[arg-type]
    assert g.resolved and g.f2p_passed == 1 and g.p2p_passed == 2
    call = box.calls[0]
    assert call["shell"].startswith("testbed-env sh -c ")
    assert call["shell"].endswith(f"> {swe.GRADE_LOG} 2>&1")
    assert call["files"] == {"/tmp/warden-test.patch": TEST_PATCH.encode()}
    assert call["cwd"] == "/testbed" and call["keep"] is True

    only_f2p = _StubBox(_passed(*task.fail_to_pass), 1)
    missing_p2p = swe.grade(only_f2p, task)  # type: ignore[arg-type]
    assert not missing_p2p.resolved and missing_p2p.f2p_passed == 1
    nothing = swe.grade(_StubBox("error: patch failed\n", 1), task)  # type: ignore[arg-type]
    assert not nothing.resolved


class _LogBox:
    """A graded checkpoint whose log is read back in chunks (sandbox stdout stops at 64 KiB)."""

    def __init__(self, log: str, code: int) -> None:
        self.log, self.code = log, code
        self.calls: list[str] = []

    def run(self, shell: str, **kw: Any) -> RunResult:
        self.calls.append(shell)
        if "tail -c +" in shell:  # `… | tail -c +OFF | head -c N`
            off = int(shell.split("tail -c +")[1].split()[0])
            n = int(shell.split("head -c ")[1].split()[0])
            status = "".join(
                " ".join(line.split()[:2]) + "\n"
                for line in self.log.splitlines()
                if line.split()[:1] and line.split()[0] in ("PASSED", "FAILED")
            )
            return RunResult(status[off - 1 : off - 1 + n], "", 0, None, 1)
        if shell.startswith("tail -c"):
            return RunResult(self.log[-50:], "", 0, None, 1)
        return RunResult("", "", self.code, self, 1)  # type: ignore[arg-type]


def test_grade_reads_a_log_larger_than_one_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(swe, "GRADE_CHUNK", 40)
    task = swe.Task.from_row(ROW)
    log = "".join(f"PASSED {t} - ok\n" for t in task.pass_to_pass) + "noise\n"
    log += "".join(f"PASSED {t}\n" for t in task.fail_to_pass) + "=== 3 passed ===\n"
    box = _LogBox(log, 0)
    g = swe.grade(box, task)  # type: ignore[arg-type]
    assert g.resolved and g.f2p_passed == 1 and g.p2p_passed == 2
    assert sum("tail -c +" in c for c in box.calls) >= 3  # several chunks
    assert g.output.endswith("=== 3 passed ===\n")


def _row(verdict: str, resolved: bool, **kw: Any) -> swe.Row:
    args: dict[str, Any] = dict(
        verdict=verdict,
        tests_added=2,
        theater_failed_on_base=True,
        theater_inconclusive=False,
        tool_turns=12,
        resolved=resolved,
        usd=0.1234567,
        seconds=61.9,
    )
    args.update(kw)
    return swe.make_row(swe.Task.from_row(ROW), **args)


def test_false_pass_is_a_pass_verdict_the_hidden_tests_reject() -> None:
    assert _row("PASS", False).false_pass
    assert _row("PASS_WITH_CONCERNS", False).false_pass
    assert not _row("PASS", True).false_pass
    assert not _row("FAIL", False).false_pass  # a FAIL on a bad fix is the verifier working


def test_false_fail_excludes_an_inconclusive_fail() -> None:
    assert _row("FAIL", True).false_fail
    timed_out = _row("FAIL", True, inconclusive=True)
    assert timed_out.unverified and not timed_out.false_fail
    assert not _row("FAIL", False).false_fail and not _row("FAIL", False).unverified
    assert not _row("PASS", True).false_fail


def test_append_refuses_a_csv_with_other_columns(tmp_path: Path) -> None:
    path = tmp_path / "results.csv"
    path.write_text("task_id,resolved\nx,1\n")
    with pytest.raises(ValueError, match="other columns"):
        swe.append_row(path, _row("PASS", True))


def test_theater_catch_only_when_new_tests_passed_on_base() -> None:
    assert _row("FAIL", False, theater_failed_on_base=False).theater_catch
    assert not _row("FAIL", False).theater_catch
    passed_on_base = {"theater_failed_on_base": False}
    assert not _row("FAIL", False, **passed_on_base, theater_inconclusive=True).theater_catch
    assert not _row("FAIL", False, **passed_on_base, tests_added=0).theater_catch


def test_rows_go_to_csv_with_the_planned_columns(tmp_path: Path) -> None:
    path = tmp_path / "results.csv"
    swe.append_row(path, _row("PASS", True))
    err = swe.error_row(swe.Task.from_row(ROW), ValueError("x\ny"), usd=0, seconds=1)
    swe.append_row(path, err)
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert tuple(rows[0]) == swe.CSV_COLUMNS
    assert rows[0]["resolved"] == "1" and rows[0]["usd"] == "0.1235" and rows[0]["seconds"] == "61"
    assert rows[1]["verdict"] == "ERROR" and rows[1]["error"] == "ValueError: x y"
    assert swe.done_ids(path) == {ROW["instance_id"]}


def test_run_eval_resumes_and_stops_before_the_budget(tmp_path: Path) -> None:
    tasks = [swe.Task.from_row({**ROW, "instance_id": f"o__r-{i}"}) for i in range(5)]
    path = tmp_path / "results.csv"
    swe.append_row(path, replace(_row("PASS", True), task_id="o__r-0"))
    settings = Settings(_env_file=None, WARDEN_MAX_USD_PER_ISSUE=1.0)  # type: ignore[call-arg]
    ran: list[str] = []

    def runner(task: swe.Task, s: Settings) -> swe.Row:
        ran.append(task.instance_id)
        return replace(_row("FAIL", False, usd=0.6), task_id=task.instance_id)

    rows = swe.run_eval(tasks, settings, csv_path=path, budget_usd=2.3, task_runner=runner)
    # o__r-0 is done; 1 → 0.6, 2 → 1.2; 3 would risk 1.2 + 1.0 cap ≤ 2.3 → runs (1.8); 4 would not
    assert ran == ["o__r-1", "o__r-2", "o__r-3"]
    assert len(rows) == 3 and len(swe.done_ids(path)) == 4


def test_suite_command_runs_the_repo_test_cmd_on_touched_test_files() -> None:
    task = swe.Task.from_row(ROW)
    cmd = swe.suite_command(task, ["tests/a.py::test_1", "tests/a.py::test_2", "tests/b.py"])
    assert cmd == f"{task.test_cmd} tests/a.py tests/b.py"
    assert swe.suite_command(task, []) == ""


def test_swe_gate_runs_only_the_suite_and_reports_the_rest_not_verified(log: EventLog) -> None:
    backend = FakeBackend()
    cand = Box.from_ref(backend, "task", log, phase="verify")
    backend.images[cand.uuid].suite_counts = {"passed": 7}
    gate = run_gate(cand, log, commands=replace(SWE_COMMANDS, suite="pytest tests/a.py"))
    assert backend.calls == [keep_tail("pytest tests/a.py")]  # only the suite, its tail kept
    assert set(gate.skipped) == {"mypy", "ruff", "alembic", "openapi"}
    assert {f"{c} (not configured)" for c in gate.skipped} <= set(gate.not_verified)
    assert gate.ok

    fixed = Candidate(
        issue=0, image_uuid=cand.uuid, tool_turns=3, tests_added=["a.py::t"], cost_usd=0, done=True
    )
    v = decide(fixed, TheaterResult(True, 1, "", "b", "p"), gate)
    assert v.decision == "PASS"
    unchecked = ("mypy", "ruff", "alembic", "OpenAPI")
    assert not any(name in r for r in v.reasons for name in unchecked)


def test_prepare_base_installs_the_env_wrapper_and_a_python_m_pytest_shim(log: EventLog) -> None:
    backend = FakeBackend()
    base = swe.prepare_base(backend, swe.Task.from_row(ROW), log)
    files = backend.images[base.uuid].files
    wrapper = files[swe.WRAPPER].decode()
    shim = files[f"{swe.SHIM_DIR}/pytest"].decode()
    assert 'exec python -m pytest "$@"' in shim
    # shim first, then miniconda's env, then /opt/conda's (last prepend wins)
    assert (
        wrapper.index("/opt/conda/envs")
        < wrapper.index("/opt/miniconda3")
        < wrapper.index(swe.SHIM_DIR)
    )
    assert f"chmod +x {swe.WRAPPER} {swe.SHIM_DIR}/pytest" in backend.calls


def test_prepare_base_reinstalls_the_package_from_testbed(log: EventLog) -> None:
    # SpikeInterface__spikeinterface-2775: the image's editable install points at
    # /spikeinterface/src, not /testbed, so nothing imports until the task's install re-runs
    task = swe.Task.from_row(ROW)
    assert task.install == "pip install --no-deps -e .[dev]"
    backend = FakeBackend()
    swe.prepare_base(backend, task, log)
    assert backend.calls[-1] == f"{swe.WRAP} 'pip install --no-deps -e .[dev]'"
    multi = swe.Task.from_row(
        {**ROW, "install_config": {"install": "for d in a b; do (cd $d; pip install -e .); done"}}
    )
    assert multi.install == "for d in a b; do (cd $d; pip install --no-deps -e .); done"
    bare = swe.Task.from_row({**ROW, "install_config": {}})
    assert bare.install == ""


def test_best_of_n_columns_default_to_one_candidate(tmp_path: Path) -> None:
    path = tmp_path / "results.csv"
    swe.append_row(path, _row("PASS", True, candidates=3, chosen=2, resolved_candidates="2;3"))
    swe.append_row(path, _row("FAIL", False))
    err = swe.error_row(swe.Task.from_row(ROW), ValueError("x"), usd=0, seconds=1)
    swe.append_row(path, err)
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [(r["candidates"], r["chosen"], r["resolved_candidates"]) for r in rows] == [
        ("3", "2", "2;3"),
        ("1", "1", ""),
        ("1", "0", ""),
    ]


def test_a_pre_d4_csv_is_upgraded_in_place_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "results.csv"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=swe.LEGACY_COLUMNS)
        w.writeheader()
        old = {k: v for k, v in _row("PASS", True).csv().items() if k in swe.LEGACY_COLUMNS}
        w.writerow({**old, "task_id": "old__one"})
        w.writerow({**old, "task_id": "old__err", "verdict": "ERROR"})
    assert swe.done_ids(path) == {"old__one", "old__err"}  # old csvs still load
    swe.append_row(path, _row("PASS", True, candidates=3, chosen=1))
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert tuple(rows[0]) == swe.CSV_COLUMNS
    assert [(r["task_id"], r["candidates"], r["chosen"]) for r in rows] == [
        ("old__one", "1", "1"),
        ("old__err", "1", "0"),
        (ROW["instance_id"], "3", "1"),
    ]
    assert rows[0]["usd"] == "0.1235"


def test_the_committed_results_csv_still_loads() -> None:
    path = Path(__file__).resolve().parents[1] / "evals" / "swe_rebench" / "results.csv"
    with path.open(newline="") as fh:
        header = tuple(next(csv.reader(fh)))
    assert header in (swe.LEGACY_COLUMNS, swe.CSV_COLUMNS)
    assert swe.done_ids(path)
