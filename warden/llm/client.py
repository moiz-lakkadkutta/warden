"""Token Factory client: structured output, tool calling, cost logging.

Heavy imports (openai) are deferred so `warden --help` and `warden doctor` stay fast.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, ValidationError

from warden.config import Role, Settings
from warden.llm.cost import CostMeter
from warden.runlog.events import EventLog
from warden.schemas import Phase

if TYPE_CHECKING:
    from openai import OpenAI

T = TypeVar("T", bound=BaseModel)


@dataclass
class ChatResult:
    content: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    raw: Any = None


class LLM:
    """One instance per run. Every call is metered and logged."""

    def __init__(self, settings: Settings, meter: CostMeter, log: EventLog) -> None:
        self._s = settings
        self._meter = meter
        self._log = log
        if meter.log is None:
            meter.log = log  # cap hits land in events.jsonl as `cost_cap`
        self._client: OpenAI | None = None

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                base_url=self._s.tokenfactory_base_url, api_key=self._s.nebius_api_key
            )
        return self._client

    # ------------------------------------------------------------------ core
    def _complete(
        self,
        role: Role,
        phase: Phase,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.1,
    ) -> ChatResult:
        model = self._s.model_for(role)
        self._meter.check(phase)
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if response_format:
            kwargs["response_format"] = response_format
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        t0 = time.perf_counter()
        resp = self.client.chat.completions.create(**kwargs)
        ms = int((time.perf_counter() - t0) * 1000)
        usage = resp.usage
        pt = int(getattr(usage, "prompt_tokens", 0) or 0)
        ct = int(getattr(usage, "completion_tokens", 0) or 0)
        usd = self._meter.charge(model, pt, ct, phase)
        choice = resp.choices[0].message
        tool_calls = [
            {
                "id": tc.id,
                "name": tc.function.name,
                "arguments": json.loads(tc.function.arguments or "{}"),
            }
            for tc in (choice.tool_calls or [])
        ]
        self._log.emit(
            phase,
            "llm",
            {
                "role": role.value,
                "model": model,
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "tool_calls": [t["name"] for t in tool_calls],
            },
            ms=ms,
            usd=usd,
        )
        return ChatResult(choice.content, tool_calls, pt, ct, model, resp)

    # ------------------------------------------------------------- structured
    def structured(
        self,
        role: Role,
        phase: Phase,
        system: str,
        user: str,
        schema: type[T],
        *,
        max_retries: int = 2,
    ) -> T:
        """JSON-schema constrained output with a documented fallback ladder.

        1. strict json_schema response_format + schema text in the prompt
        2. on validation error: retry once with the pydantic error appended
        3. fall back to json_object mode + validate
        4. raise
        """
        json_schema = schema.model_json_schema()
        sys_msg = (
            f"{system}\n\nRespond ONLY with JSON matching this schema:\n{json.dumps(json_schema)}"
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": user},
        ]
        strict_fmt = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": json_schema, "strict": True},
        }
        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            fmt = strict_fmt if attempt < max_retries else {"type": "json_object"}
            result = self._complete(role, phase, messages, response_format=fmt)
            try:
                return schema.model_validate_json(result.content or "")
            except ValidationError as err:
                last_err = err
                messages.append({"role": "assistant", "content": result.content or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": f"Invalid. Fix these errors and resend only JSON:\n{err}",
                    }
                )
        raise RuntimeError(f"structured output failed for {schema.__name__}: {last_err}")

    # ------------------------------------------------------------------ tools
    def chat(
        self,
        role: Role,
        phase: Phase,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        return self._complete(role, phase, messages, tools=tools)
