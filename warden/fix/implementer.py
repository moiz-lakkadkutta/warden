"""TDD loop: write failing test → run → implement (whole-file rewrites) → run → `finish`.

Super (`Role.IMPLEMENTER`) drives the five TOOLS schemas plus a `finish` pseudo-tool directly; a
prose-only turn is routed once through Lightning (`Role.TOOLS`) into tool calls and spliced into
Super's history as if Super had made them. Every sandbox effect goes through `ToolDispatcher` over
`Box`, and `ToolResult.box` threads the checkpoint from call to call. The only handler in this
module is for pydantic's ValidationError on the `finish` arguments; `CostCapExceededError` and
backend errors propagate to the caller. Progress goes through the local `Progress` protocol
(ui.PhaseLog satisfies it) so this module never imports the UI.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shlex
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from warden.config import Role
from warden.fix.tools import TOOL_SCHEMAS, ToolDispatcher, as_message, strict_schema
from warden.llm.client import ChatResult
from warden.llm.cost import usd_for
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.schemas import Candidate, FixReport, Phase, Plan

PROMPT_PATH: Path = Path(__file__).resolve().parents[2] / "prompts" / "implementer.md"
PHASE: Phase = "fix"

FINISH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": (
            "Stop working and report the outcome. Call with done=true once every acceptance "
            "item passes, or done=false with the unmet items when blocked or near the budget."
        ),
        "parameters": strict_schema(FixReport.model_json_schema()),
    },
}
ALL_TOOLS: list[dict[str, Any]] = [*TOOL_SCHEMAS, FINISH_TOOL]

ROUTER_SYSTEM = (
    "You are a tool router for a coding agent. The user message is the agent's prose reply. "
    "Translate the concrete next action it describes into exactly one tool call (use `finish` "
    "when it says the work is done or blocked). If it describes no action, reply with no tool call."
)
NUDGE = "Reply with a tool call (or finish)."
BUDGET_MSG = "tool budget exhausted; call finish"

_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.MULTILINE)


@runtime_checkable
class LLMLike(Protocol):
    def chat(
        self,
        role: Role,
        phase: Phase,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult: ...


@runtime_checkable
class Progress(Protocol):
    def step(self, text: str) -> None: ...
    def cost(self, usd: float) -> None: ...


class _NoProgress:
    def step(self, text: str) -> None:
        return None

    def cost(self, usd: float) -> None:
        return None


# --------------------------------------------------------------------------- helpers


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _norm(path: str, root: str = "/app") -> str:
    """Repo-relative form of `path`: absolute paths under the workdir `root` lose that prefix."""
    return posixpath.normpath(path.strip()).removeprefix(root.rstrip("/") + "/")


def is_test_path(path: str, plan: Plan, root: str = "/app") -> bool:
    """A test file by location (`tests/`), by name (`test_*.py`, `*_test.py`) or by the plan
    (whose tests_to_add may be pytest node ids, `path::test_name`)."""
    p = _norm(path, root)
    name = posixpath.basename(p)
    return (
        p.startswith("tests/")
        or "/tests/" in p
        or fnmatch(name, "test_*.py")
        or fnmatch(name, "*_test.py")
        or p in {_norm(t.split("::", 1)[0], root) for t in plan.tests_to_add}
    )


def removed_tests(before: str, after: str) -> list[str]:
    """`def test_*` names present in `before` and missing from `after` (a rename counts)."""
    kept = set(_TEST_DEF.findall(after))
    out: list[str] = []
    for name in _TEST_DEF.findall(before):
        if name not in kept and name not in out:
            out.append(name)
    return out


def build_messages(
    prompt_text: str, plan: Plan, max_tool_turns: int, *, workdir: str = "/app"
) -> list[dict[str, Any]]:
    tools = "\n".join(
        f"- {t['function']['name']} — {t['function']['description']}" for t in ALL_TOOLS
    )
    system = (
        f"{prompt_text.rstrip()}\n\n## Available tools\n{tools}\n\n"
        f"Working directory: {workdir} (the repo root). Every command already runs there and "
        "every path is relative to it, so never `cd` (other directories such as /app are not "
        "the repo).\n"
        f"Budget: {max_tool_turns} tool turns\n"
    )

    def block(title: str, items: list[str]) -> str:
        body = "\n".join(f"- {i}" for i in items) if items else "- (none)"
        return f"{title}:\n{body}"

    acceptance = "\n".join(f"{i}. {a}" for i, a in enumerate(plan.acceptance, 1)) or "(none)"
    user = "\n\n".join(
        [
            f"Issue #{plan.issue}\n\nSummary: {plan.summary}",
            block("Files to touch", plan.files_to_touch),
            block("Tests to add", plan.tests_to_add),
            block("Landing order", plan.landing_order),
            block("Citations", [f"{c.url} — {c.claim}" for c in plan.citations]),
            f"Acceptance:\n{acceptance}",
        ]
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def assistant_message(content: str | None, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Request-side assistant turn: `function.arguments` must be a JSON string, not a dict."""
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [
            {
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
            }
            for c in tool_calls
        ]
    return msg


def _tool_message(tool_call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def parse_finish(args: Any) -> FixReport | str:
    """The validated report, or the error text fed back to the model (no URLs)."""
    # The only handler in this module: a pure in-memory check of the model's arguments.
    # CostCapExceededError and sandbox/backend errors are never caught here.
    try:
        return FixReport.model_validate(args)
    except ValidationError as err:
        errors = err.errors(include_url=False)
    lines = ["invalid arguments for finish:"]
    for e in errors:
        loc = ".".join(str(p) for p in e["loc"]) or "<args>"
        lines.append(f"  {loc}: {e['msg']}")
    return "\n".join(lines)


def _charge(res: ChatResult, progress: Progress) -> float:
    usd = usd_for(res.model, res.prompt_tokens, res.completion_tokens)
    progress.cost(usd)
    return usd


def _cat(box: Box, path: str) -> Any:
    return box.run(f"cat {shlex.quote(path)}", keep=False)


def _step_text(name: str, args: Any, exit_code: int | None) -> str:
    subject = str(args.get("path") or args.get("command") or "") if isinstance(args, dict) else ""
    head = f"{name} {subject}" if subject else name
    return f"{head} → exit {exit_code}"


# --------------------------------------------------------------------------- loop


def run_implementer(
    llm: LLMLike,
    base: Box,
    plan: Plan,
    log: EventLog,
    *,
    dispatcher: ToolDispatcher | None = None,
    max_tool_turns: int,
    progress: Progress | None = None,
) -> Candidate:
    """Drive the implementer over `base` until `finish` or the tool-turn budget; return the
    Candidate whose `image_uuid` is the last checkpoint. A "turn" is one model action: a
    dispatched call, a refused write, an invalid `finish` or an unroutable prose reply."""
    dispatch = dispatcher if dispatcher is not None else ToolDispatcher(log)
    hooks: Progress = progress if progress is not None else _NoProgress()

    def note(what: str, **data: Any) -> None:
        log.emit(PHASE, "note", {"implementer": what, **data})

    prompt_text = PROMPT_PATH.read_text(encoding="utf-8")
    root = dispatch.cwd
    messages = build_messages(prompt_text, plan, max_tool_turns, workdir=root)
    box = base
    turns = 0
    tests_added: list[str] = []
    first_write: str | None = None
    cost = 0.0
    report: FixReport | None = None
    on_base: dict[str, bool] = {}
    base_tests: dict[str, set[str]] = {}  # test function names per test file on base

    note(
        "start",
        prompt_hash=prompt_hash(prompt_text),
        issue=plan.issue,
        base=base.uuid,
        max_tool_turns=max_tool_turns,
    )
    while report is None and turns < max_tool_turns:
        res = llm.chat(Role.IMPLEMENTER, PHASE, messages, tools=ALL_TOOLS)
        cost += _charge(res, hooks)
        calls = res.tool_calls
        if not calls and res.content:
            routed = llm.chat(
                Role.TOOLS,
                PHASE,
                [
                    {"role": "system", "content": ROUTER_SYSTEM},
                    {"role": "user", "content": res.content},
                ],
                tools=ALL_TOOLS,
            )
            cost += _charge(routed, hooks)
            calls = routed.tool_calls
            if calls:
                note("routed", tool_calls=[c["name"] for c in calls])
        messages.append(assistant_message(res.content, calls))
        if not calls:
            turns += 1
            messages.append({"role": "user", "content": NUDGE})
            note("unroutable", content=(res.content or "")[:200])
            continue
        for call in calls:
            cid, name, args = call["id"], call["name"], call["arguments"]
            if name == "finish":  # free: never dispatched, honoured even once the budget is spent
                parsed = parse_finish(args)
                if isinstance(parsed, FixReport):
                    report = parsed
                    break
                turns += 1
                messages.append(_tool_message(cid, parsed))
                continue
            if turns >= max_tool_turns:
                messages.append(_tool_message(cid, BUDGET_MSG))
                continue
            is_test_write = (
                name == "write_file"
                and isinstance(args, dict)
                and isinstance(args.get("path"), str)
                and isinstance(args.get("content"), str)
                and is_test_path(args["path"], plan, root)
            )
            if is_test_write:
                path = str(args["path"])
                before = _cat(box, path)
                removed = removed_tests(before.stdout, str(args["content"])) if before.ok else []
                if removed:
                    turns += 1
                    messages.append(
                        _tool_message(
                            cid,
                            f"refused: write_file would delete {', '.join(removed)} from "
                            f"{path}; add tests, never remove them",
                        )
                    )
                    note("refused_write", path=path, removed=removed)
                    continue
                key = _norm(path, root)
                if key not in on_base:
                    base_file = _cat(base, path)
                    on_base[key] = base_file.ok
                    base_tests[key] = (
                        set(_TEST_DEF.findall(base_file.stdout)) if base_file.ok else set()
                    )
            result = dispatch.dispatch(box, name, args)
            box = result.box
            turns += 1
            hooks.step(_step_text(name, args, result.exit_code))
            messages.append(as_message(result, cid))
            if name == "write_file" and result.ok:
                path = str(args["path"])
                first_write = path if first_write is None else first_write
                key = _norm(path, root)
                if is_test_write and not on_base.get(key, True):
                    if key not in tests_added:
                        tests_added.append(key)
                elif is_test_write:  # existing file: each new test function is a node id
                    for name_ in _TEST_DEF.findall(str(args["content"])):
                        node = f"{key}::{name_}"
                        if name_ not in base_tests[key] and node not in tests_added:
                            tests_added.append(node)

    if report is None:
        note("budget", tool_turns=turns)
        hooks.step(f"tool budget exhausted after {turns} turns")
    test_first = first_write is not None and is_test_path(first_write, plan, root)
    note("tdd_order", first_write=first_write, test_first=test_first)
    if tests_added:
        cmd = "pytest -q " + " ".join(shlex.quote(p) for p in tests_added)
        final = dispatch.run(box, cmd, timeout=600)
        note(
            "final_tests",
            tests=list(tests_added),
            exit_code=final.exit_code,
            tail=(final.stdout + final.stderr)[-500:],
        )
    outcome = "done" if report is not None and report.done else "blocked"
    note(
        outcome,
        tool_turns=turns,
        tests_added=list(tests_added),
        cost_usd=round(cost, 6),
        image_uuid=box.uuid,
        report=None if report is None else report.model_dump(),
    )
    return Candidate(
        issue=plan.issue,
        image_uuid=box.uuid,
        tool_turns=turns,
        tests_added=list(tests_added),
        cost_usd=round(cost, 6),
    )
