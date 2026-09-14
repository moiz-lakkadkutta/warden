from pathlib import Path

import pytest

from warden.llm.cost import CostCapExceededError, CostMeter, usd_for
from warden.runlog.events import EventLog


def test_usd_for_known_model() -> None:
    assert usd_for("nvidia/Nemotron-3_5-Lightning", 1_000_000, 1_000_000) == pytest.approx(0.30)


def test_cap_message_is_actionable() -> None:
    m = CostMeter(cap_usd=0.01)
    with pytest.raises(CostCapExceededError) as e:
        m.charge("nvidia/Nemotron-3-Ultra-550b-a55b", 10_000, 10_000, "fix")
    assert "Raise with --max-usd" in str(e.value)
    assert "Fix" in str(e.value)


def test_cap_message_exact() -> None:
    e = CostCapExceededError(3.5, 3.0, "fix")
    assert str(e) == "cost cap $3.00 reached at Fix; stopping. Raise with --max-usd"
    assert (e.spent, e.cap, e.phase) == (3.5, 3.0, "fix")


def test_cap_emits_cost_cap_event(tmp_path: Path) -> None:
    log = EventLog(tmp_path, "t")
    m = CostMeter(cap_usd=0.01, log=log)
    with pytest.raises(CostCapExceededError):
        m.charge("nvidia/Nemotron-3-Ultra-550b-a55b", 10_000, 10_000, "fix")
    log.close()
    events = EventLog.read(tmp_path, "t")
    assert events and events[-1].kind == "cost_cap"
    assert events[-1].phase == "fix"
    assert events[-1].data["cap_usd"] == 0.01
    assert events[-1].data["spent_usd"] == pytest.approx(m.spent_usd)


def test_check_emits_cost_cap_event(tmp_path: Path) -> None:
    log = EventLog(tmp_path, "t")
    m = CostMeter(cap_usd=1.0, spent_usd=2.0, log=log)
    with pytest.raises(CostCapExceededError):
        m.check("verify")
    log.close()
    events = EventLog.read(tmp_path, "t")
    assert [(e.kind, e.phase) for e in events] == [("cost_cap", "verify")]


def test_charge_under_cap_emits_nothing(tmp_path: Path) -> None:
    log = EventLog(tmp_path, "t")
    m = CostMeter(cap_usd=10.0, log=log)
    m.charge("nvidia/Nemotron-3_5-Lightning", 100, 100, "fix")
    m.check("fix")
    log.close()
    assert EventLog.read(tmp_path, "t") == []
