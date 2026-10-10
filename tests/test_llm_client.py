"""LLM client: request shape sent to Token Factory (fake OpenAI client, no network)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel

from warden.config import Role, Settings
from warden.llm.client import LLM
from warden.llm.cost import CostMeter
from warden.runlog.events import EventLog


class Out(BaseModel):
    ok: bool


class FakeCompletions:
    def __init__(self) -> None:
        self.kwargs: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        msg = SimpleNamespace(content='{"ok": true}', tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg)],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )


def _llm(tmp_path: Path) -> tuple[LLM, FakeCompletions]:
    llm = LLM(Settings(), CostMeter(5.0), EventLog(tmp_path / "runs", "t"))
    fake = FakeCompletions()
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))  # type: ignore[assignment]
    return llm, fake


def test_planner_calls_get_a_large_completion_budget(tmp_path: Path) -> None:
    llm, fake = _llm(tmp_path)
    assert llm.structured(Role.PLANNER, "plan", "s", "u", Out).ok
    assert fake.kwargs[0]["max_tokens"] == 16384  # reasoning counts against it (A5: 8192 hit)


def test_tools_calls_keep_the_server_default(tmp_path: Path) -> None:
    llm, fake = _llm(tmp_path)
    llm.structured(Role.TOOLS, "plan", "s", "u", Out)
    assert "max_tokens" not in fake.kwargs[0]


def test_explicit_max_tokens_wins(tmp_path: Path) -> None:
    llm, fake = _llm(tmp_path)
    llm.structured(Role.PLANNER, "plan", "s", "u", Out, max_tokens=4000)
    assert fake.kwargs[0]["max_tokens"] == 4000


def test_provider_402_becomes_a_budget_error_with_an_event(tmp_path: Path) -> None:
    import json

    import pytest

    from warden.llm.client import ProviderBudgetError

    class PaymentRequiredError(Exception):
        status_code = 402

    llm, fake = _llm(tmp_path)

    def boom(**kwargs: Any) -> Any:
        raise PaymentRequiredError("Payment Required: You have exhausted your budget.")

    fake.create = boom  # type: ignore[method-assign]
    with pytest.raises(ProviderBudgetError, match="budget") as ei:
        llm.structured(Role.TOOLS, "plan", "s", "u", Out)
    assert "tokenfactory" in ei.value.fix
    events = (tmp_path / "runs" / "t" / "events.jsonl").read_text().splitlines()
    assert any(json.loads(e)["kind"] == "error" for e in events)


def test_other_provider_errors_propagate_unchanged(tmp_path: Path) -> None:
    import pytest

    class TeapotError(Exception):
        status_code = 418

    llm, fake = _llm(tmp_path)

    def boom(**kwargs: Any) -> Any:
        raise TeapotError("no")

    fake.create = boom  # type: ignore[method-assign]
    with pytest.raises(TeapotError):
        llm.structured(Role.TOOLS, "plan", "s", "u", Out)
