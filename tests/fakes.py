"""Offline doubles for the implementer loop.

`ScriptedLLM` replays a fixed list of `ChatResult`s (or raises the listed error) and records every
call; `TddBackend` is a `FakeBackend` where writing an implementation file makes its test pass;
`RecordingProgress` collects the loop's progress hooks. Nothing here touches the network.
"""

from __future__ import annotations

import copy
import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from warden.config import Role
from warden.llm.client import ChatResult
from warden.llm.cost import usd_for
from warden.runlog.events import EventLog
from warden.sandbox.fake import FakeBackend
from warden.schemas import Phase

_ids = itertools.count(1)


def tool_call(name: str, /, **args: Any) -> dict[str, Any]:
    """The exact dict `LLM._complete` builds for one tool call."""
    return {"id": f"call_{next(_ids)}", "name": name, "arguments": args}


def turn(
    *calls: dict[str, Any],
    content: str | None = None,
    model: str = "",
    pt: int = 1000,
    ct: int = 100,
) -> ChatResult:
    return ChatResult(content, list(calls), pt, ct, model)


def text_turn(content: str) -> ChatResult:
    return turn(content=content)


def finish_turn(done: bool = True, **fields: Any) -> ChatResult:
    return turn(tool_call("finish", done=done, **fields))


@dataclass
class Call:
    role: Role
    phase: Phase
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None


class ScriptedLLM:
    """`chat()` pops the next scripted item; an extra call fails fast with `AssertionError`."""

    def __init__(
        self, turns: Sequence[ChatResult | BaseException], *, log: EventLog | None = None
    ) -> None:
        self._turns = list(turns)
        self._log = log
        self.calls: list[Call] = []
        # Live reference to the most recent `messages` list: shows what the loop appended after
        # its last chat (the deep copies in `calls` are snapshots taken at call time).
        self.last_messages: list[dict[str, Any]] | None = None

    def chat(
        self,
        role: Role,
        phase: Phase,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        if not self._turns:
            raise AssertionError("script exhausted")
        item = self._turns.pop(0)
        self.calls.append(Call(role, phase, copy.deepcopy(messages), tools))
        self.last_messages = messages
        if isinstance(item, BaseException):
            raise item
        if self._log is not None:
            self._log.emit(
                phase,
                "llm",
                {
                    "role": role.value,
                    "model": item.model,
                    "prompt_tokens": item.prompt_tokens,
                    "completion_tokens": item.completion_tokens,
                    "tool_calls": [c["name"] for c in item.tool_calls],
                },
                ms=0,
                usd=usd_for(item.model, item.prompt_tokens, item.completion_tokens),
            )
        return item


class TddBackend(FakeBackend):
    """Writing an implementation path (a `files=` key) makes its test path pass from then on."""

    def __init__(self, passes_after: dict[str, str]) -> None:
        super().__init__()
        self.passes_after = dict(passes_after)

    def run(
        self,
        image: Any,
        *,
        shell: str,
        cwd: str,
        env: dict[str, str] | None,
        timeout: int,
        files: dict[str, bytes] | None,
        keep: bool,
    ) -> tuple[str, str, int, Any | None, str]:
        out = super().run(
            image, shell=shell, cwd=cwd, env=env, timeout=timeout, files=files, keep=keep
        )
        child = out[3]
        if child is not None and files:
            for rel in files:
                if rel in self.passes_after:
                    child.test_results[self.passes_after[rel]] = 0
        return out


@dataclass
class RecordingProgress:
    steps: list[str] = field(default_factory=list)
    costs: list[float] = field(default_factory=list)

    def step(self, text: str) -> None:
        self.steps.append(text)

    def cost(self, usd: float) -> None:
        self.costs.append(usd)
