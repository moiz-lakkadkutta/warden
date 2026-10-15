"""LLM client: request shape sent to Token Factory (fake OpenAI client, no network)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
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


def test_tools_role_is_sent_with_thinking_off(tmp_path: Path) -> None:
    llm, fake = _llm(tmp_path)
    llm.structured(Role.TOOLS, "plan", "s", "u", Out)
    llm.structured(Role.PLANNER, "plan", "s", "u", Out)
    assert fake.kwargs[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "extra_body" not in fake.kwargs[1]


def test_schema_echo_envelope_is_unwrapped(tmp_path: Path) -> None:
    llm, fake = _llm(tmp_path)
    echo = '{"title": "Out", "type": "object", "properties": {"ok": true}, "required": ["ok"]}'

    def create(**kwargs: Any) -> Any:
        fake.kwargs.append(kwargs)
        msg = SimpleNamespace(content=echo, tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg)],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )

    fake.create = create  # type: ignore[method-assign]
    assert llm.structured(Role.TOOLS, "plan", "s", "u", Out).ok is True


def test_schema_echo_with_a_real_schema_body_is_still_invalid() -> None:
    from warden.llm.client import _schema_echo

    schema_text = '{"type": "object", "properties": {"ok": {"type": "boolean"}}}'
    assert _schema_echo(schema_text, Out) is None
    assert _schema_echo("not json", Out) is None


def _implementer_call(tmp_path: Path, settings: Settings) -> dict[str, Any]:
    llm = LLM(settings, CostMeter(5.0), EventLog(tmp_path / "runs", "t"))
    fake = FakeCompletions()
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))  # type: ignore[assignment]
    llm.chat(Role.IMPLEMENTER, "fix", [{"role": "user", "content": "x"}], tools=[])
    return fake.kwargs[0]


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_implementer_completion_budget_comes_from_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # camp-head-1-210: 7 of 40 replies cut at the server default (8192); 32768 served 24980
    monkeypatch.delenv("WARDEN_IMPLEMENTER_MAX_TOKENS", raising=False)
    assert _settings().implementer_max_tokens == 16384
    assert _implementer_call(tmp_path, _settings())["max_tokens"] == 16384
    monkeypatch.setenv("WARDEN_IMPLEMENTER_MAX_TOKENS", "32768")
    assert _implementer_call(tmp_path, _settings())["max_tokens"] == 32768
    monkeypatch.setenv("WARDEN_IMPLEMENTER_MAX_TOKENS", "0")  # the server default
    assert "max_tokens" not in _implementer_call(tmp_path, _settings())


def test_implementer_completion_budget_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    monkeypatch.setenv("WARDEN_IMPLEMENTER_MAX_TOKENS", "100000")
    with pytest.raises(ValidationError):
        _settings()


def test_doctor_reports_the_implementer_budget() -> None:
    from typer.testing import CliRunner

    from warden.cli import app

    env = {"WARDEN_IMPLEMENTER_MAX_TOKENS": "0", "NO_COLOR": "1"}
    res = CliRunner().invoke(app, ["doctor"], env=env)
    assert "WARDEN_IMPLEMENTER_MAX_TOKENS (server default, 8192)" in res.stderr
    assert "stop at 8192 completion tokens" in res.stderr
    env = {"WARDEN_IMPLEMENTER_MAX_TOKENS": "16384", "NO_COLOR": "1"}
    res = CliRunner().invoke(app, ["doctor"], env=env)
    assert "WARDEN_IMPLEMENTER_MAX_TOKENS (16384)" in res.stderr
    assert "stop at" not in res.stderr


def test_env_example_documents_the_implementer_budget() -> None:
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    assert "WARDEN_IMPLEMENTER_MAX_TOKENS=16384" in text
