"""Regression check: the base's own tests, unchanged, on the candidate — relative to the base."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.verify.regression import regression_check, related_tests

CMD = "pytest -rA -p no:cacheprovider"
T1 = "tests/plugins_t/test_telegrafmonitoring.py"
T2 = "tests/test_utils.py"


class ShellStub:
    """Scripted backend: the first matching (image prefix, substring) rule answers a run.

    copy_in forks to `<image>+t`; every run records (image, shell, cwd, files)."""

    def __init__(self, rules: list[tuple[str, str, str, int]]) -> None:
        self.rules = rules
        self.runs: list[dict[str, Any]] = []

    def use(self, ref: str) -> str:
        return ref

    def run(
        self, image: Any, *, shell: str, cwd: str, keep: bool, files: Any = None, **kw: Any
    ) -> Any:
        self.runs.append({"image": image, "shell": shell, "cwd": cwd, "files": files, **kw})
        if files is not None:
            return "", "", 0, f"{image}+t", f"{image}+t"
        for img, needle, out, code in self.rules:
            if str(image).startswith(img) and needle in shell:
                return out, "", code, None, ""
        return "", "no rule", 2, None, ""

    def tag(self, image: Any, name: str) -> None:
        return None

    def uuid_of(self, image: Any) -> str:
        return str(image)


def _boxes(tmp_path: Path, stub: ShellStub) -> tuple[Box, Box]:
    log = EventLog(tmp_path / "runs", "t")
    return Box(stub, "base", log), Box(stub, "cand", log)


DIFF = "tardis/plugins/telegrafmonitoring.py\ntests/plugins_t/test_telegrafmonitoring.py\n"
GREP = f"{T2}\n{T1}\n"
BASE_OUT = f"PASSED {T1}::T::test_notify\nPASSED {T1}::T::test_init\nFAILED {T2}::test_old\n"


def _rules(cand_out: str, cand_exit: int = 1) -> list[tuple[str, str, str, int]]:
    return [
        ("cand", "git diff --name-only", DIFF, 0),
        ("base", "git grep", GREP, 0),
        ("base", "cat ", "def test_notify(): ...\n", 0),
        ("base", "pytest", BASE_OUT, 1),
        ("cand+t", "pytest", cand_out, cand_exit),
    ]


def test_related_tests_ranks_name_matches_first_and_skips_non_python() -> None:
    changed = ["tardis/plugins/telegrafmonitoring.py", "README.md", "tests/test_x.py"]
    hits = ["tests/test_utils.py", "tests/plugins_t/test_telegrafmonitoring.py", "docs/x.py"]
    assert related_tests(changed, hits, cap=8) == [
        "tests/plugins_t/test_telegrafmonitoring.py",
        "tests/test_utils.py",
    ]
    assert related_tests(changed, hits, cap=1) == ["tests/plugins_t/test_telegrafmonitoring.py"]


def test_a_test_green_on_base_and_red_on_candidate_is_a_regression(tmp_path: Path) -> None:
    out = f"FAILED {T1}::T::test_notify\nPASSED {T1}::T::test_init\nFAILED {T2}::test_old\n"
    stub = ShellStub(_rules(out))
    base, cand = _boxes(tmp_path, stub)
    res = regression_check(base, cand, test_cmd=CMD, cwd="/testbed", wrap="testbed-env sh -c")
    assert res is not None
    # T1 was edited by the candidate, so its red test is a changed expectation, not a regression;
    # T2's test_old already failed on base and is not the candidate's fault.
    assert res.broken == []
    assert res.changed_expectations == [f"{T1}::T::test_notify"]
    assert res.inconclusive is False
    # the base's own files are copied onto the candidate: an edited test cannot hide a regression
    copy = next(r for r in stub.runs if r["files"] is not None)
    assert copy["image"] == "cand" and set(copy["files"]) == {T1, T2}
    pytest_runs = [r for r in stub.runs if "pytest" in r["shell"]]
    assert all(r["shell"].startswith("testbed-env sh -c '") for r in pytest_runs)
    assert all(r["cwd"] == "/testbed" for r in pytest_runs)


def test_red_test_in_a_file_the_candidate_did_not_edit_is_broken(tmp_path: Path) -> None:
    diff_src_only = "tardis/plugins/telegrafmonitoring.py\n"
    out = f"PASSED {T1}::T::test_notify\nERROR {T1}::T::test_init\nFAILED {T2}::test_old\n"
    rules = [("cand", "git diff --name-only", diff_src_only, 0), *_rules(out)[1:]]
    base, cand = _boxes(tmp_path, ShellStub(rules))
    res = regression_check(base, cand, test_cmd=CMD, cwd="/testbed")
    assert res is not None
    assert res.broken == [f"{T1}::T::test_init"]
    assert "1 test green on base is red on the candidate" in res.label


def test_all_green_is_clean(tmp_path: Path) -> None:
    out = f"PASSED {T1}::T::test_notify\nPASSED {T1}::T::test_init\nFAILED {T2}::test_old\n"
    base, cand = _boxes(tmp_path, ShellStub(_rules(out)))
    res = regression_check(base, cand, test_cmd=CMD, cwd="/testbed")
    assert res is not None and res.broken == [] and res.changed_expectations == []
    assert res.label == "2 base tests in 2 files stay green"


def test_no_outcomes_on_candidate_is_inconclusive_not_proof(tmp_path: Path) -> None:
    base, cand = _boxes(tmp_path, ShellStub(_rules("ImportError: boom", 2)))
    res = regression_check(base, cand, test_cmd=CMD, cwd="/testbed")
    assert res is not None and res.inconclusive is True and res.broken == []


def test_timeout_is_inconclusive(tmp_path: Path) -> None:
    base, cand = _boxes(tmp_path, ShellStub(_rules("", 124)))
    res = regression_check(base, cand, test_cmd=CMD, cwd="/testbed", timeout=60)
    assert res is not None and res.inconclusive is True
    assert all(r.get("timeout") == 60 for r in res_runs(base) if "pytest" in r["shell"])


def res_runs(box: Box) -> list[dict[str, Any]]:
    return box._b.runs  # type: ignore[attr-defined,no-any-return]


def test_no_related_tests_means_not_run(tmp_path: Path) -> None:
    rules = [("cand", "git diff --name-only", "README.md\n", 0)]
    base, cand = _boxes(tmp_path, ShellStub(rules))
    assert regression_check(base, cand, test_cmd=CMD, cwd="/testbed") is None
