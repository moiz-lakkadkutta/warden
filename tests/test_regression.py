"""Regression check: the base's own tests, unchanged, on the candidate — relative to the base."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.verify.regression import content_pattern, regression_check, related_tests, targets
from warden.verify.tail import TAIL_BYTES

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
        self.runs.append(
            {"image": image, "shell": shell, "cwd": cwd, "files": files, "keep": keep, **kw}
        )
        if files is not None or (keep and "git checkout" in shell):
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
    assert related_tests(changed, [], hits, cap=8) == [
        "tests/plugins_t/test_telegrafmonitoring.py",
        "tests/test_utils.py",
    ]
    assert related_tests(changed, [], hits, cap=1) == ["tests/plugins_t/test_telegrafmonitoring.py"]


# SWE-rebench SpikeInterface__spikeinterface-2775 (pilot 3): the test file only says
# `CurationSorting`, so a case-sensitive content grep for `curationsorting` never selected it.
SI_SRC = "src/spikeinterface/curation/curationsorting.py"
SI_TEST = "src/spikeinterface/curation/tests/test_curationsorting.py"
SI_TESTS = [
    "src/spikeinterface/core/tests/test_basesorting.py",
    "src/spikeinterface/curation/tests/test_auto_merge.py",
    SI_TEST,
    "src/spikeinterface/curation/tests/test_mergeunitssorting.py",
]


def test_a_test_file_named_after_the_module_is_selected_without_any_content_hit() -> None:
    assert related_tests([SI_SRC], SI_TESTS, [], cap=8) == [SI_TEST]


def test_filename_matches_rank_before_content_matches() -> None:
    hits = ["src/spikeinterface/curation/tests/test_auto_merge.py", SI_TEST]
    assert related_tests([SI_SRC], SI_TESTS, hits, cap=8) == [
        SI_TEST,
        "src/spikeinterface/curation/tests/test_auto_merge.py",
    ]
    assert related_tests([SI_SRC], SI_TESTS, hits, cap=1) == [SI_TEST]


def test_other_filename_shapes_match() -> None:
    tests = ["tests/curationsorting_test.py", "tests/curationsorting/test_split.py", "tests/a.py"]
    assert related_tests([SI_SRC], tests, [], cap=8) == tests[:2]


def test_content_is_searched_case_insensitively_and_by_dotted_module_path() -> None:
    pattern = content_pattern(targets([SI_SRC]))
    assert "curationsorting" in pattern
    assert r"spikeinterface\.curation\.curationsorting" in pattern
    assert re.search(pattern, "from spikeinterface.curation import CurationSorting", re.I)


def test_init_uses_the_package_name() -> None:
    (t,) = targets(["pkg/curation/__init__.py"])
    assert t.stem == "curation" and t.module == "pkg.curation" and not t.generic
    tests = ["pkg/tests/test_curation.py", "pkg/tests/test_other.py"]
    assert related_tests(["pkg/curation/__init__.py"], tests, [], cap=8) == tests[:1]
    # a top-level package's __init__ is imported by everything: name match only, no content term
    (top,) = targets(["pkg/__init__.py"])
    assert top.generic and content_pattern([top]) == ""


def test_generic_stems_match_by_name_or_dotted_path_never_by_bare_word() -> None:
    changed = ["src/pkg/utils.py"]
    (t,) = targets(changed)
    assert t.generic and t.module == "pkg.utils"
    pattern = content_pattern([t])
    assert re.search(pattern, "from pkg.utils import x", re.I)
    assert not re.search(pattern, "from other import utils", re.I)
    tests = [f"tests/utils/test_{i}.py" for i in range(50)] + ["tests/test_utils.py"]
    # a `tests/utils/` directory is not evidence for a generic stem; the exact name is
    assert related_tests(changed, tests, [], cap=8) == ["tests/test_utils.py"]
    (short,) = targets(["pkg/io.py"])
    assert short.generic


def test_selection_is_capped() -> None:
    hits = [f"tests/test_{i}.py" for i in range(30)]
    assert len(related_tests([SI_SRC], SI_TESTS, hits, cap=8)) == 8


def test_spikeinterface_layout_end_to_end_with_dash_safe_commands(tmp_path: Path) -> None:
    out = f"PASSED {SI_TEST}::test_curation\n"
    rules = [
        ("cand", "git diff --name-only", f"{SI_SRC}\n", 0),
        ("base", "git ls-files", "\n".join(SI_TESTS) + "\n", 0),
        ("base", "git grep", "", 1),  # nothing mentions the lower-case stem
        ("base", "cat ", "def test_curation(): ...\n", 0),
        ("base", "pytest", out, 0),
        ("cand+t", "pytest", out, 0),
    ]
    stub = ShellStub(rules)
    base, cand = _boxes(tmp_path, stub)
    res = regression_check(base, cand, test_cmd=CMD, cwd="/testbed")
    assert res.ran and res.files == (SI_TEST,) and res.broken == []
    grep = next(r["shell"] for r in stub.runs if "git grep" in r["shell"])
    assert " -i " in grep and " -l " in grep
    for r in stub.runs:  # POSIX sh (dash) in the sandbox: no bash-isms
        assert not any(b in r["shell"] for b in ("[[", "<(", "{a,", "shopt", "**"))


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
    # the base's own files are restored on the candidate: an edited test cannot hide a regression
    restore = next(r for r in stub.runs if "git checkout" in r["shell"])
    assert restore["image"] == "cand" and restore["keep"] is True
    assert restore["shell"] == f"git checkout -q HEAD -- {T1} {T2}"
    pytest_runs = [r for r in stub.runs if "pytest" in r["shell"]]
    assert all(r["shell"].startswith("testbed-env sh -c '") for r in pytest_runs)
    assert all("; exit $rc'" in r["shell"] for r in pytest_runs)  # their tails are kept
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


def test_a_node_scrolled_out_of_a_cut_tail_is_not_a_regression(tmp_path: Path) -> None:
    """Run 20261014-123938-6f4c: long -rA output; a node absent from one side's (full) tail is
    unknown, not red. Without the cut, a vanished node still counts as red."""
    pad = "x" * TAIL_BYTES + "\n"
    out = f"{pad}PASSED {T1}::T::test_init\nFAILED {T2}::test_old\n"  # test_notify cut off
    base, cand = _boxes(tmp_path, ShellStub(_rules(out)))
    res = regression_check(base, cand, test_cmd=CMD, cwd="/testbed")
    assert res is not None and res.broken == [] and res.changed_expectations == []
    short = f"PASSED {T1}::T::test_init\nFAILED {T2}::test_old\n"
    base, cand = _boxes(tmp_path, ShellStub(_rules(short)))
    res = regression_check(base, cand, test_cmd=CMD, cwd="/testbed")
    assert res is not None and res.changed_expectations == [f"{T1}::T::test_notify"]


def res_runs(box: Box) -> list[dict[str, Any]]:
    return box._b.runs  # type: ignore[attr-defined,no-any-return]


def test_no_changed_module_is_an_explicit_not_run(tmp_path: Path) -> None:
    rules = [("cand", "git diff --name-only", "README.md\n", 0)]
    base, cand = _boxes(tmp_path, ShellStub(rules))
    res = regression_check(base, cand, test_cmd=CMD, cwd="/testbed")
    assert not res.ran and res.files == () and res.broken == []
    assert res.label.startswith("not run: ")


def test_no_related_test_file_is_an_explicit_not_run(tmp_path: Path) -> None:
    rules = [
        ("cand", "git diff --name-only", "pkg/frobnicate.py\n", 0),
        ("base", "git ls-files", "tests/test_other.py\n", 0),
        ("base", "git grep", "", 1),
    ]
    stub = ShellStub(rules)
    base, cand = _boxes(tmp_path, stub)
    res = regression_check(base, cand, test_cmd=CMD, cwd="/testbed")
    assert not res.ran and "frobnicate" in res.skipped
    assert not any("pytest" in r["shell"] for r in stub.runs)


def test_base_test_files_are_restored_from_git_never_read_through_stdout(tmp_path: Path) -> None:
    """E1 gmr__pamqp-52: `cat` of a 1700-line test file came back cut at the sandbox's 64 KiB
    output cap; copied onto the candidate it was a SyntaxError that read as 237 regressions."""
    out = f"PASSED {T1}::T::test_notify\nPASSED {T1}::T::test_init\nFAILED {T2}::test_old\n"
    stub = ShellStub(_rules(out))
    base, cand = _boxes(tmp_path, stub)
    res = regression_check(base, cand, test_cmd=CMD, cwd="/testbed")
    assert res.broken == [] and not res.inconclusive
    assert not any(r["shell"].startswith("cat ") for r in stub.runs)
    assert not any(r["files"] for r in stub.runs)
    probe_runs = [r for r in stub.runs if r["image"] == "cand+t"]
    assert probe_runs and all("pytest" in r["shell"] for r in probe_runs)


class _NoRestore(ShellStub):
    def run(self, image: Any, *, shell: str, cwd: str, keep: bool, **kw: Any) -> Any:
        if "git checkout" in shell:
            self.runs.append({"image": image, "shell": shell, "cwd": cwd, "files": None})
            return "", "error: pathspec", 1, None, ""
        return super().run(image, shell=shell, cwd=cwd, keep=keep, **kw)


def test_a_failed_restore_is_inconclusive_not_a_regression(tmp_path: Path) -> None:
    base, cand = _boxes(tmp_path, _NoRestore(_rules("")))
    res = regression_check(base, cand, test_cmd=CMD, cwd="/testbed")
    assert res.ran and res.inconclusive and res.broken == []
