import pytest

from warden.llm.cost import CostCapExceededError, CostMeter, usd_for


def test_usd_for_known_model() -> None:
    assert usd_for("nvidia/Nemotron-3_5-Lightning", 1_000_000, 1_000_000) == pytest.approx(0.30)


def test_cap_message_is_actionable() -> None:
    m = CostMeter(cap_usd=0.01)
    with pytest.raises(CostCapExceededError) as e:
        m.charge("nvidia/Nemotron-3-Ultra-550b-a55b", 10_000, 10_000, "fix")
    assert "Raise with --max-usd" in str(e.value)
    assert "Fix" in str(e.value)
