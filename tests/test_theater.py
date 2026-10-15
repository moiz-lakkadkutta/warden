"""Theater check on a real repo: node ids, per-test outcomes, errors are not failures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.verify.theater import (
    DEFAULT_CMD,
    exception_kind,
    failure_messages,
    parse_outcomes,
    theater_check,
)

REPRO = "backend/tests/test_d.py::test_draft_excluded"
GUARD = "backend/tests/test_d.py::test_ar_unchanged"


class PytestStub:
    """Scripted backend: copy_in forks; any pytest run returns (stdout, exit)."""

    def __init__(self, stdout: str, exit_code: int) -> None:
        self.stdout, self.exit_code = stdout, exit_code
        self.runs: list[dict[str, Any]] = []

    def use(self, ref: str) -> str:
        return ref

    def run(
        self, image: Any, *, shell: str, cwd: str, keep: bool, files: Any = None, **_: Any
    ) -> Any:
        self.runs.append({"shell": shell, "cwd": cwd, "files": files, "keep": keep})
        if files is not None:
            return "", "", 0, f"{image}+t", f"{image}+t"
        return self.stdout, "", self.exit_code, None, ""

    def tag(self, image: Any, name: str) -> None:
        return None

    def uuid_of(self, image: Any) -> str:
        return str(image)


RA_OUT = f"""
FAILED {REPRO} - assert Decimal('10498.95') == Decimal('0')
PASSED {GUARD}
=========================== short test summary info ============================
PASSED {GUARD}
FAILED {REPRO} - assert Decimal('10498.95') == Decimal('0')
1 failed, 1 passed in 0.61s
"""


def _box(tmp_path: Path, stub: PytestStub) -> Box:
    return Box(stub, "base", EventLog(tmp_path / "runs", "t"))


def test_parse_outcomes_reads_ra_summary() -> None:
    assert parse_outcomes(RA_OUT) == {REPRO: "failed", GUARD: "passed"}
    assert parse_outcomes(f"ERROR {REPRO} - ImportError\n") == {REPRO: "error"}


def test_repro_fails_and_guard_passes_on_base_is_a_real_test(tmp_path: Path) -> None:
    stub = PytestStub(RA_OUT, 1)
    res = theater_check(
        _box(tmp_path, stub),
        {"backend/tests/test_d.py": b"def test_draft_excluded(): ..."},
        node_ids=[REPRO, GUARD],
        cwd="/src",
        wrap="with-services sh -c",
    )
    assert res.failed_on_base is True and res.inconclusive is False
    assert res.outcomes == {REPRO: "failed", GUARD: "passed"}
    copy, run = stub.runs
    assert copy["files"] and copy["cwd"] == "/src"
    assert run["cwd"] == "/src"
    assert run["shell"].startswith("with-services sh -c '")
    assert REPRO in run["shell"] and GUARD in run["shell"] and "-rA" in run["shell"]
    assert "FAIL on unfixed base" in res.label


def test_all_new_tests_pass_on_base_is_theater(tmp_path: Path) -> None:
    out = f"PASSED {REPRO}\nPASSED {GUARD}\n2 passed in 0.5s\n"
    res = theater_check(_box(tmp_path, PytestStub(out, 0)), {"f.py": b""}, node_ids=[REPRO, GUARD])
    assert res.failed_on_base is False and res.inconclusive is False
    assert "theater" in res.label


def test_no_tests_or_usage_error_is_inconclusive_not_a_failure(tmp_path: Path) -> None:
    for code, out in [(4, "no tests ran\n"), (5, ""), (124, "")]:
        res = theater_check(_box(tmp_path, PytestStub(out, code)), {"f.py": b""}, node_ids=[REPRO])
        assert res.failed_on_base is False, code
        assert res.inconclusive is True and res.invalid is False, code
        assert "inconclusive" in res.label


@pytest.mark.parametrize(
    ("code", "out", "why"),
    [
        (2, f"ERROR {REPRO} - ImportError: cannot import name 'f'\n", "fixture/setup error"),
        (1, f"ERROR {REPRO} - fixture 'x' not found\n1 error in 0.1s\n", "fixture not found"),
        (1, f"ERROR {REPRO} - RuntimeError: db down\n1 error in 0.1s\n", "setup error on base"),
        (
            2,
            "____ ERROR collecting backend/tests/test_d.py ____\nE   ImportError: no name\n"
            "ERROR backend/tests/test_d.py\n!!! Interrupted: 1 error during collection !!!\n",
            "collection error on base (ImportError)",
        ),
    ],
)
def test_errors_on_base_are_invalid_repros(tmp_path: Path, code: int, out: str, why: str) -> None:
    """A collection error, a fixture error, an import of a name the fix adds: the test never ran
    the behaviour. Not proof, and not 'inconclusive' either: the repro is invalid."""
    res = theater_check(_box(tmp_path, PytestStub(out, code)), {"f.py": b""}, node_ids=[REPRO])
    assert res.failed_on_base is False and res.inconclusive is False
    assert res.invalid is True
    assert why in res.reasons[REPRO] and res.reasons[REPRO].startswith("invalid")
    assert res.label.startswith("invalid repro on unfixed base")


class TwoBoxStub(PytestStub):
    """Base and candidate answer differently: runs on an image whose name contains "cand" get
    `cand_stdout`."""

    def __init__(self, stdout: str, exit_code: int, cand_stdout: str, cand_exit: int) -> None:
        super().__init__(stdout, exit_code)
        self.cand = (cand_stdout, cand_exit)

    def run(
        self, image: Any, *, shell: str, cwd: str, keep: bool, files: Any = None, **_: Any
    ) -> Any:
        if files is None and "cand" in str(image):
            self.runs.append({"shell": shell, "cwd": cwd, "files": None, "keep": keep})
            return self.cand[0], "", self.cand[1], None, ""
        return super().run(image, shell=shell, cwd=cwd, keep=keep, files=files)


def _check(
    tmp_path: Path, base_out: str, base_exit: int, cand_out: str, cand_exit: int, **kw: Any
) -> Any:
    stub = TwoBoxStub(base_out, base_exit, cand_out, cand_exit)
    log = EventLog(tmp_path / "runs", "t")
    res = theater_check(
        Box(stub, "base", log),
        {"backend/tests/test_d.py": b""},
        node_ids=[REPRO, GUARD],
        candidate=Box(stub, "cand", log),
        **kw,
    )
    return res, stub


FIXED = f"PASSED {REPRO}\nPASSED {GUARD}\n2 passed in 0.3s\n"


def test_repro_must_pass_with_the_fix(tmp_path: Path) -> None:
    res, stub = _check(tmp_path, RA_OUT, 1, FIXED, 0)
    assert res.failed_on_base is True and res.invalid is False
    assert res.reasons == {
        REPRO: "repro: fails on base (AssertionError), passes with the fix",
        GUARD: "guard: passes on base",
    }
    assert res.candidate_outcomes == {REPRO: "passed", GUARD: "passed"}
    base_run, cand_run = stub.runs[1], stub.runs[2]
    assert base_run["shell"] == cand_run["shell"]  # the same tests, the same command
    assert "(1 of 2 reproduce)" in res.label


def test_wrong_expectation_still_failing_with_the_fix_is_invalid(tmp_path: Path) -> None:
    """Run 20261014-123340-85cd: the xcal test failed on base only because its own expectation
    was wrong — it failed on the candidate too. Failing on base was not the issue's reason."""
    still = (
        f"PASSED {GUARD}\nFAILED {REPRO} - assert ['TEST', ['byday']] == ['TEST', ['recur']]\n"
        "1 failed, 1 passed in 0.4s\n"
    )
    res, _ = _check(tmp_path, RA_OUT, 1, still, 1)
    assert res.failed_on_base is False and res.invalid is True and res.inconclusive is False
    assert res.reasons[REPRO] == (
        "invalid: fails on base (AssertionError), and still fails with the fix"
    )
    assert "test_d.py::test_draft_excluded: invalid: fails on base" in res.label


def test_a_repro_missing_from_the_candidate_run_is_not_confirmed(tmp_path: Path) -> None:
    res, _ = _check(tmp_path, RA_OUT, 1, f"PASSED {GUARD}\n1 passed\n", 0)
    assert res.failed_on_base is True
    assert res.reasons[REPRO] == (
        "repro: fails on base (AssertionError); not confirmed with the fix"
    )


def test_any_new_test_red_with_the_fix_invalidates_the_repro(tmp_path: Path) -> None:
    guard_breaks = f"PASSED {REPRO}\nFAILED {GUARD} - assert 1 == 0\n1 failed, 1 passed\n"
    res, _ = _check(tmp_path, RA_OUT, 1, guard_breaks, 1)
    assert res.failed_on_base is False and res.invalid is True
    assert res.reasons[REPRO].startswith("repro:")
    assert res.reasons[GUARD] == "invalid: guard: passes on base, but failed with the fix applied"


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("NameError: name 'to_jcal_ordered' is not defined", "NameError"),
        ("ModuleNotFoundError: No module named 'icalendar.jcal2'", "ModuleNotFoundError"),
        ("TypeError: to_jcal() got an unexpected keyword argument 'order'", "TypeError"),
        ("AttributeError: 'vRecur' object has no attribute 'ordered'", "AttributeError"),
    ],
)
def test_red_on_base_for_a_missing_api_is_invalid(tmp_path: Path, message: str, kind: str) -> None:
    base = f"FAILED {REPRO} - {message}\nPASSED {GUARD}\n1 failed, 1 passed in 0.2s\n"
    res, _ = _check(tmp_path, base, 1, FIXED, 0)
    assert res.failed_on_base is False and res.invalid is True
    assert res.reasons[REPRO] == f"invalid: fails on base with {kind}, not on the behaviour"


def test_an_exception_the_issue_names_is_the_behaviour(tmp_path: Path) -> None:
    base = f"FAILED {REPRO} - TypeError: can't compare offset-naive\nPASSED {GUARD}\n"
    res, _ = _check(tmp_path, base, 1, FIXED, 0, issue_text="to_ical() raises TypeError when …")
    assert res.failed_on_base is True
    assert res.reasons[REPRO] == "repro: fails on base (TypeError), passes with the fix"
    other = f"FAILED {REPRO} - KeyError: 'FREQ'\nPASSED {GUARD}\n"
    assert _check(tmp_path, other, 1, FIXED, 0)[0].failed_on_base is True  # the bug raising


def test_failure_kind_is_read_from_tb_sections_when_the_summary_has_no_message(
    tmp_path: Path,
) -> None:
    """Long node ids drop the `-rA` message (terminal width); the --tb section still has it."""
    base = (
        "___________ test_draft_excluded ___________\n"
        "backend/tests/test_d.py:9: in test_draft_excluded\n"
        "    f()\n"
        "E   NameError: name 'f' is not defined\n"
        f"FAILED {REPRO}\nPASSED {GUARD}\n1 failed, 1 passed in 0.2s\n"
    )
    res, _ = _check(tmp_path, base, 1, FIXED, 0)
    assert res.invalid is True and "NameError" in res.reasons[REPRO]


def test_failure_messages_and_exception_kind() -> None:
    out = (
        "____ ERROR at setup of TestC.test_y ____\nE       fixture 'db' not found\n"
        "____ TestC.test_z[a-1] ____\nE   assert 1 == 2\nE     more\n"
        "_ _ _ _ _ _ _ _ _ _\nE   ValueError: deeper frame\n"
        "FAILED t.py::test_w - pkg.errors.ParseError: bad\n"
    )
    msgs = failure_messages(out)
    assert msgs == {
        "setup:TestC.test_y": "fixture 'db' not found",
        "TestC.test_z[a-1]": "assert 1 == 2",
        "t.py::test_w": "pkg.errors.ParseError: bad",
    }
    assert [exception_kind(m) for m in msgs.values()] == ["fixture", "AssertionError", "ParseError"]
    assert exception_kind("Failed: DID NOT RAISE <class 'ValueError'>") == "Failed"
    assert exception_kind("something odd") == ""


def test_theater_runs_with_the_tail_kept(tmp_path: Path) -> None:
    stub = PytestStub(RA_OUT, 1)
    theater_check(_box(tmp_path, stub), {"f.py": b""}, node_ids=[REPRO])
    assert stub.runs[1]["shell"].endswith("; exit $rc") and "tail -c " in stub.runs[1]["shell"]


REAL_TESTS = """
def test_behaviour_with_a_long_name_that_pushes_the_message_past_the_terminal_width_x():
    assert ["BYDAY", "FREQ"] == ["FREQ", "BYDAY"]


def test_missing_api():
    to_jcal_ordered()  # noqa: F821


def test_missing_fixture(nope):
    pass


def test_guard():
    pass
"""


def test_classification_on_real_pytest_output(tmp_path: Path) -> None:
    """Real `pytest -rA --tb=short` output at 80 columns, where long ids lose their message."""
    import os
    import subprocess
    import sys

    (tmp_path / "test_real.py").write_text(REAL_TESTS)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *DEFAULT_CMD.split()[1:], "test_real.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "COLUMNS": "80"},
        check=False,
    )
    nodes = {
        "repro": "test_real.py::test_behaviour_with_a_long_name_that_pushes_the_message_past_the"
        "_terminal_width_x",
        "api": "test_real.py::test_missing_api",
        "fixture": "test_real.py::test_missing_fixture",
        "guard": "test_real.py::test_guard",
    }
    fixed = "".join(f"PASSED {n}\n" for n in nodes.values())
    stub = TwoBoxStub(proc.stdout, proc.returncode, fixed, 0)
    log = EventLog(tmp_path / "runs", "t")
    res = theater_check(
        Box(stub, "base", log),
        {"test_real.py": b""},
        node_ids=list(nodes.values()),
        candidate=Box(stub, "cand", log),
    )
    assert res.reasons == {
        nodes["repro"]: "repro: fails on base (AssertionError), passes with the fix",
        nodes["api"]: "invalid: fails on base with NameError, not on the behaviour",
        nodes["fixture"]: "invalid: fixture not found on base",
        nodes["guard"]: "guard: passes on base",
    }, proc.stdout
    assert res.failed_on_base is True  # one real repro; the invalid ones just do not count


# Run 20261014-131452-cd9b (icalendar#1928): the implementer added a whole new test file, so
# `tests_added` (the theater targets) was the *file*, while the -rA lines name its tests. The
# verdict keyed theater_tests by the file with "no result on base" although all four passed.
CD9B_FILE = "src/icalendar/tests/prop/recur/test_recur.py"
CD9B_TESTS = [
    f"{CD9B_FILE}::test_vrecur_to_jcal_freq_first",
    f"{CD9B_FILE}::test_vrecur_to_xcal_freq_first",
    f"{CD9B_FILE}::test_vrecur_to_ical_freq_first_regression",
    f"{CD9B_FILE}::test_vrecur_other_keys_after_freq",
]
# verbatim from runs/20261014-131452-cd9b/artifacts/theater.txt
CD9B_BASE_OUT = (
    "....                                                                     [100%]\n"
    "==================================== PASSES ====================================\n"
    "=========================== short test summary info ============================\n"
    + "".join(f"PASSED {t}\n" for t in CD9B_TESTS)
    + "4 passed in 0.61s\n"
)


def test_file_target_maps_to_its_per_test_ids_cd9b(tmp_path: Path) -> None:
    res = theater_check(
        _box(tmp_path, PytestStub(CD9B_BASE_OUT, 0)),
        {CD9B_FILE: b""},
        node_ids=[CD9B_FILE],
        cwd="/src",
    )
    assert res.reasons == {t: "guard: passes on base" for t in CD9B_TESTS}
    assert res.failed_on_base is False and res.invalid is False and res.inconclusive is False
    assert res.label == "PASSED on unfixed base — tests are theater"


def test_file_target_with_a_test_red_on_base_is_a_repro(tmp_path: Path) -> None:
    """The same file-target shape plus a FREQ-last test that fails on base, passes when fixed:
    with the file as the target it used to be "no result" — a missed repro."""
    last = f"{CD9B_FILE}::test_to_jcal_freq_last_in_source"
    base_out = CD9B_BASE_OUT.replace(
        "4 passed in 0.61s\n",
        f"FAILED {last} - AssertionError: assert 'COUNT' == 'FREQ'\n1 failed, 4 passed in 0.7s\n",
    )
    fixed = "".join(f"PASSED {t}\n" for t in [*CD9B_TESTS, last]) + "5 passed in 0.6s\n"
    stub = TwoBoxStub(base_out, 1, fixed, 0)
    log = EventLog(tmp_path / "runs", "t")
    res = theater_check(
        Box(stub, "base", log),
        {CD9B_FILE: b""},
        node_ids=[CD9B_FILE],
        candidate=Box(stub, "cand", log),
    )
    assert res.failed_on_base is True and res.invalid is False
    assert res.reasons[last] == "repro: fails on base (AssertionError), passes with the fix"
    assert CD9B_FILE not in res.reasons
    assert all(res.reasons[t] == "guard: passes on base" for t in CD9B_TESTS)


def test_a_file_target_and_its_own_node_are_not_counted_twice(tmp_path: Path) -> None:
    node = CD9B_TESTS[0]
    res = theater_check(
        _box(tmp_path, PytestStub(CD9B_BASE_OUT, 0)),
        {CD9B_FILE: b""},
        node_ids=[CD9B_FILE, node],
    )
    assert list(res.reasons) == CD9B_TESTS


def test_theater_db_prefix_is_prepended_to_the_run(tmp_path: Path) -> None:
    stub = PytestStub(RA_OUT, 1)
    theater_check(_box(tmp_path, stub), {"f.py": b""}, node_ids=[REPRO], db_prefix="PFX_X")
    pytest_runs = [r for r in stub.runs if "pytest" in r["shell"]]
    assert pytest_runs and all("PFX_X &&" in r["shell"] for r in pytest_runs)
