from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_swe_rebench import ROW
from warden.eval import bases
from warden.eval import swe_rebench as swe
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.sandbox.fake import FakeBackend, FakeImage

GOLD = "diff --git a/src/x.py b/src/x.py\n"


class _Backend(FakeBackend):
    """FakeBackend where `git apply` succeeds (the fake has no git)."""

    def _interpret_gate(self, img: FakeImage, parts: list[str]) -> tuple[str, str, int]:
        if parts[:2] == ["git", "apply"]:
            return "", "", 0
        return super()._interpret_gate(img, parts)


@pytest.fixture
def log(tmp_path: Path) -> Iterator[EventLog]:
    log = EventLog(tmp_path / "runs", "t")
    yield log
    log.close()


def _grader(*, base_f2p: int = 0, base_p2p: int = 2, gold_ok: bool = True) -> Any:
    """Grade by whether the box carries the gold patch file: base vs gold."""

    def grade(box: Box, task: swe.Task) -> swe.Grade:
        backend: Any = box._b  # noqa: SLF001 — test peeks at the fake image
        gold = bases.GOLD_PATCH in backend.images[box.uuid].files
        f2p = len(task.fail_to_pass) if (gold and gold_ok) else base_f2p
        p2p = len(task.pass_to_pass) if (gold and gold_ok) else base_p2p
        resolved = f2p == len(task.fail_to_pass) and p2p == len(task.pass_to_pass)
        return swe.Grade(resolved, f2p, p2p, 0 if resolved else 1, True)

    return grade


def test_a_good_base_fails_unfixed_and_passes_with_gold(log: EventLog) -> None:
    task = swe.Task.from_row(ROW)
    c = bases.check_base(_Backend(), task, log, gold_patch=GOLD, grader=_grader())
    assert c.image_ok and c.base_unresolved and c.gold_resolved
    assert c.excluded_reason == ""
    assert "F2P 0/1" in c.note and "P2P 2/2" in c.note


def test_without_gold_the_gold_column_is_unknown(log: EventLog) -> None:
    c = bases.check_base(_Backend(), swe.Task.from_row(ROW), log, grader=_grader())
    assert c.gold_resolved is None and c.excluded_reason == ""


def test_a_base_that_already_passes_is_excluded(log: EventLog) -> None:
    task = swe.Task.from_row(ROW)
    c = bases.check_base(_Backend(), task, log, gold_patch=GOLD, grader=_grader(base_f2p=1))
    assert not c.base_unresolved
    assert "already" in c.excluded_reason


def test_gold_that_does_not_resolve_is_excluded(log: EventLog) -> None:
    task = swe.Task.from_row(ROW)
    c = bases.check_base(_Backend(), task, log, gold_patch=GOLD, grader=_grader(gold_ok=False))
    assert c.base_unresolved and c.gold_resolved is False
    assert "gold" in c.excluded_reason


def test_gold_that_does_not_apply_is_excluded(log: EventLog) -> None:
    task = swe.Task.from_row(ROW)
    c = bases.check_base(FakeBackend(), task, log, gold_patch=GOLD, grader=_grader())
    assert c.gold_resolved is False and "apply" in c.note


def test_p2p_red_on_base_without_a_gold_check_is_excluded(log: EventLog) -> None:
    c = bases.check_base(_Backend(), swe.Task.from_row(ROW), log, grader=_grader(base_p2p=1))
    assert "P2P" in c.excluded_reason


def test_a_missing_image_is_a_row_not_a_crash(log: EventLog) -> None:
    class Missing(FakeBackend):
        def use(self, ref: str) -> Any:
            raise LookupError(f"no image {ref}")

    c = bases.check_base(Missing(), swe.Task.from_row(ROW), log, grader=_grader())
    assert not c.image_ok and c.base_unresolved is None
    assert "image" in c.excluded_reason and "LookupError" in c.note


def test_a_failed_install_keeps_image_ok(log: EventLog) -> None:
    def bad_prepare(backend: Any, task: swe.Task, log: EventLog) -> Box:
        raise RuntimeError("install failed in x (pip install): boom")

    c = bases.check_base(
        _Backend(), swe.Task.from_row(ROW), log, grader=_grader(), prepare=bad_prepare
    )
    assert c.image_ok and c.base_unresolved is None
    assert "install" in c.excluded_reason

    def lazy_missing(backend: Any, task: swe.Task, log: EventLog) -> Box:
        raise RuntimeError("could not prepare x:latest: image not found")

    c = bases.check_base(
        _Backend(), swe.Task.from_row(ROW), log, grader=_grader(), prepare=lazy_missing
    )
    assert not c.image_ok and "image" in c.excluded_reason


def test_write_outputs_csv_and_excluded(tmp_path: Path) -> None:
    good = bases.BaseCheck("a__b-1", True, True, True, 3, "F2P 0/1 P2P 2/2")
    bad = bases.BaseCheck("c__d-2", True, False, None, 4, "F2P 1/1 P2P 2/2")
    bases.write_outputs(tmp_path, [good, bad])
    with (tmp_path / "bases.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert tuple(rows[0]) == bases.CSV_COLUMNS
    assert rows[0]["gold_resolved"] == "1" and rows[1]["gold_resolved"] == ""
    assert rows[1]["base_unresolved"] == "0"
    excluded = (tmp_path / "excluded.txt").read_text()
    assert excluded.startswith("c__d-2\t") and "a__b-1" not in excluded
    assert bases.read_excluded(tmp_path / "excluded.txt") == {"c__d-2": bad.excluded_reason}


def test_run_checks_keeps_task_order_with_parallel_workers() -> None:
    tasks = [swe.Task.from_row({**ROW, "instance_id": f"o__r-{i}"}) for i in range(6)]

    def check(task: swe.Task) -> bases.BaseCheck:
        return bases.BaseCheck(task.instance_id, True, True, None, 0, "")

    out = bases.run_checks(tasks, check, workers=3)
    assert [c.task_id for c in out] == [t.instance_id for t in tasks]


def test_read_checks_round_trips_and_merge_replaces_by_id(tmp_path: Path) -> None:
    a = bases.BaseCheck("a__b-1", True, True, None, 3, "base F2P 0/1 P2P 1/2", True)
    b = bases.BaseCheck("c__d-2", False, None, None, 1, "LookupError: x")
    bases.write_outputs(tmp_path, [a, b])
    back = bases.read_checks(tmp_path / "bases.csv")
    assert back == [a, b] and back[0].excluded_reason.startswith("P2P")
    fixed = bases.BaseCheck("a__b-1", True, True, True, 4, "base F2P 0/1 P2P 2/2; gold ...")
    new = bases.BaseCheck("e__f-3", True, True, True, 4, "")
    assert bases.merge_checks(back, [new, fixed]) == [fixed, b, new]
