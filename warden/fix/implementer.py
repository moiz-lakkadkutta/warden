"""TDD loop: write failing test → run → implement (replace_in_file / write_file) → run → `finish`.

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
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from warden.config import Role
from warden.fix.nodes import test_nodes
from warden.fix.tools import (
    TOOL_SCHEMAS,
    ToolDispatcher,
    as_message,
    replace_once,
    strict_schema,
    tolerate_args,
)
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
    if p.startswith("/"):  # outside the repo (e.g. /tmp/debug_test.py): scratch, not a test
        return False
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
    prompt_text: str,
    plan: Plan,
    max_tool_turns: int,
    *,
    workdir: str = "/app",
    lint: str = "",
    variant: str = "",
    read_cache: bool = True,
) -> list[dict[str, Any]]:
    tools = "\n".join(
        f"- {t['function']['name']} — {t['function']['description']}" for t in ALL_TOOLS
    )
    system = (
        f"{prompt_text.rstrip()}\n\n## Available tools\n{tools}\n\n"
        f"Working directory: {workdir} (the repo root). Every command already runs there and "
        "every path is relative to it, so never `cd` (other directories such as /app are not "
        "the repo).\n"
        + (
            f"Before `finish`, run `{lint}` and fix everything it reports (unused variables "
            "included); the target repo's CI rejects any lint error.\n"
            if lint
            else ""
        )
        + (
            "Re-reading a file range you were already shown, unchanged, returns a short note "
            "instead of the content: read once, then edit.\n"
            if read_cache
            else ""
        )
        + f"Budget: {max_tool_turns} tool turns\n"
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
            *([variant] if variant else []),
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


KEEP_RECENT = 6  # newest tool results (and write_file calls) sent whole
ELIDE_MIN = 1500  # shorter outputs are cheap to keep


def compact_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The history to send: older tool outputs become a one-line stub and older write_file
    calls lose their content (the file is in the box, a read_file away). Without this every
    earlier page is re-sent on every turn: on the SWE-rebench pilot prompts reached 173k tokens
    (pennylane-5831, 3.0M prompt tokens in 34 calls). Pairing and order are untouched."""
    tool_idx = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    call_idx = [
        i for i, m in enumerate(messages) if m["role"] == "assistant" and m.get("tool_calls")
    ]
    old_tools = set(tool_idx[:-KEEP_RECENT]) if len(tool_idx) > KEEP_RECENT else set()
    old_calls = set(call_idx[:-KEEP_RECENT]) if len(call_idx) > KEEP_RECENT else set()
    if not old_tools and not old_calls:
        return messages  # short history: nothing to elide, no copy
    out: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        if i in old_tools and len(m["content"] or "") > ELIDE_MIN:
            text = m["content"]
            first = text.splitlines()[0][:120] if text else ""
            m = {
                **m,
                "content": f"[elided by Warden after newer turns: {len(text)} chars; first line: "
                f"{first!r}. Re-run the tool if you need it again.]",
            }
        elif i in old_calls:
            m = {**m, "tool_calls": [_elide_write(c) for c in m["tool_calls"]]}
        out.append(m)
    return out


_ELIDE_KEYS: dict[str, tuple[str, ...]] = {
    "write_file": ("content",),
    "replace_in_file": ("old", "new"),
}


def _elide_write(call: dict[str, Any]) -> dict[str, Any]:
    fn = call["function"]
    keys = _ELIDE_KEYS.get(fn["name"])
    if keys is None:
        return call
    args = json.loads(fn["arguments"])  # built by assistant_message: always valid JSON
    changed = False
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and len(value) > ELIDE_MIN:
            args[key] = f"[{len(value)} chars written]"
            changed = True
    if not changed:
        return call
    return {**call, "function": {**fn, "arguments": json.dumps(args)}}


def _tool_message(tool_call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


# ------------------------------------------------------------------ read-loop guards
# Live evidence (icalendar#1928, run 20261014-123340-85cd): 26 of 40 turns re-read files the
# model had already been shown, and it never reached the edit. Two guards, both config values:
# a re-read of an unchanged range still visible in the history gets a short note instead of
# the content, and a run of `idle_nudge_after` turns without write_file/run_shell gets a nudge.

REREAD_MSG = (
    "unchanged since turn {turn}: {path} lines {first}-{last} have not changed and that "
    "read_file result is still in this conversation. You already have it — now write the test "
    "or make the edit with replace_in_file or write_file, or run the tests with run_shell."
)
RESENT_PREFIX = "[unchanged since turn {turn}; re-sent because that output was elided]\n"
IDLE_NUDGE = (
    "You have spent {idle} tool turns in a row reading and searching without write_file or "
    "run_shell, and {left} of {budget} turns are left. You already have what you read; stop "
    "re-reading. Next: write the failing test, make the edit (write_file), or run the tests "
    "(run_shell)."
)


# Finish nudge. Pilot 3: fromager's new tests were green at turn 10 and the implementer never
# called `finish`; 7 of 10 runs spent every turn. Once a test run exits 0 with new tests written
# and no edit since, FINISH_NUDGE_AFTER more turns without `finish` get one note (once a run).
FINISH_NUDGE_AFTER = 2
FINISH_NUDGE = (
    "Your new tests pass. If the fix is complete, call finish now; otherwise say what's left."
)
_TEST_CMD = re.compile(r"(?<![\w.-])(pytest|py\.test|unittest|tox|nox)(?![\w-])")


@dataclass(frozen=True)
class _Served:
    first: int
    last: int
    total: int
    turn: int
    msg: int  # index of its tool message in the loop's `messages`


class ReadMemory:
    """Line ranges read_file has shown the model, per file, since the file last changed.
    `forget` on every write to the file; `forget(None)` after run_shell (it may write anything)."""

    def __init__(self, root: str) -> None:
        self._root = root
        self._seen: dict[str, list[_Served]] = {}

    def forget(self, path: str | None) -> None:
        if path is None:
            self._seen.clear()
        else:
            self._seen.pop(_norm(path, self._root), None)

    def record(self, path: str, first: int, last: int, total: int, turn: int, msg: int) -> None:
        self._seen.setdefault(_norm(path, self._root), []).append(
            _Served(first, last, total, turn, msg)
        )

    def covering(
        self, path: str, start: int, end: int | None, visible: Callable[[int], bool]
    ) -> tuple[int, int, int] | None:
        """`(turn, first, last)` when the visible servings together cover `start..end` (`end`
        None: to the end of the file), with `turn` the latest serving used; None otherwise."""
        spans = [s for s in self._seen.get(_norm(path, self._root), []) if visible(s.msg)]
        if not spans:
            return None
        want = spans[-1].total if end is None else min(end, spans[-1].total)
        if start > want:
            return None  # past the end: let read_file report it
        need, turn = start, 0
        for s in sorted(spans, key=lambda s: s.first):
            if s.first <= need <= s.last:
                need, turn = s.last + 1, max(turn, s.turn)
            if need > want:
                return turn, start, want
        return None


def _visible(messages: list[dict[str, Any]], idx: int) -> bool:
    """Will message `idx` still be sent whole by `compact_history` after one more tool result?"""
    if len(messages[idx]["content"] or "") <= ELIDE_MIN:
        return True
    tool_idx = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    return idx in tool_idx[-(KEEP_RECENT - 1) :]


def _read_range(args: Any) -> tuple[str, int, int | None] | None:
    """`(path, start_line, end_line)` of a read_file call, tolerated as the dispatcher would."""
    fixed = tolerate_args("read_file", args).args
    if not isinstance(fixed, dict):
        return None
    path, start, end = fixed.get("path"), fixed.get("start_line", 1), fixed.get("end_line")
    if not isinstance(path, str) or not path.strip() or not isinstance(start, int):
        return None
    return path, start, end if isinstance(end, int) else None


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


def _cat(box: Box, path: str, cwd: str) -> Any:
    return box.run(f"cat {shlex.quote(path)}", cwd=cwd, keep=False)


EDIT_TOOLS = ("write_file", "replace_in_file")


def _edit_path(name: str, args: Any) -> str | None:
    """The file a write_file / replace_in_file call edits, when its arguments are usable."""
    if name not in EDIT_TOOLS or not isinstance(args, dict):
        return None
    path = args.get("path")
    need = ("content",) if name == "write_file" else ("old", "new")
    if not isinstance(path, str) or not all(isinstance(args.get(k), str) for k in need):
        return None
    return path


def _after_edit(name: str, args: dict[str, Any], before: str | None) -> str | None:
    """The file's content once the edit lands (None: the edit would fail)."""
    if name == "write_file":
        return str(args["content"])
    if before is None:
        return None
    text, err = replace_once(before, str(args["old"]), str(args["new"]))
    return None if err else text


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
    lint: str = "",
    autofix: str = "",
    variant: str = "",
    idle_nudge_after: int = 8,
    read_cache: bool = True,
) -> Candidate:
    """Drive the implementer over `base` until `finish` or the tool-turn budget; return the
    Candidate whose `image_uuid` is the last checkpoint. A "turn" is one model action: a
    dispatched call, a refused write, a cached re-read, an invalid `finish` or an unroutable
    prose reply. `idle_nudge_after` (0 = off) and `read_cache` are the read-loop guards.
    `variant` (best-of-N) is appended to the user message so candidates do not converge."""
    dispatch = dispatcher if dispatcher is not None else ToolDispatcher(log)
    hooks: Progress = progress if progress is not None else _NoProgress()

    def note(what: str, **data: Any) -> None:
        log.emit(PHASE, "note", {"implementer": what, **data})

    prompt_text = PROMPT_PATH.read_text(encoding="utf-8")
    root = dispatch.cwd
    messages = build_messages(
        prompt_text,
        plan,
        max_tool_turns,
        workdir=root,
        lint=lint,
        variant=variant,
        read_cache=read_cache,
    )
    box = base
    turns = 0
    tests_added: list[str] = []
    first_write: str | None = None
    cost = 0.0
    report: FixReport | None = None
    on_base: dict[str, bool] = {}
    base_tests: dict[str, set[str]] = {}  # test function names per test file on base
    memory = ReadMemory(root)
    idle = 0  # tool turns since the last write_file / run_shell
    green_at: int | None = None  # turn of the last passing test run with no edit since
    finish_nudged = False

    def visible(idx: int) -> bool:
        return _visible(messages, idx)

    note(
        "start",
        prompt_hash=prompt_hash(prompt_text),
        issue=plan.issue,
        base=base.uuid,
        max_tool_turns=max_tool_turns,
        idle_nudge_after=idle_nudge_after,
        read_cache=read_cache,
    )
    while report is None and turns < max_tool_turns:
        res = llm.chat(Role.IMPLEMENTER, PHASE, compact_history(messages), tools=ALL_TOOLS)
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
            cid, name, raw = call["id"], call["name"], call["arguments"]
            # what the dispatcher will act on (aliases, coercions); it gets `raw` and logs repairs
            args = tolerate_args(name, raw).args
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
            edit_path = _edit_path(name, args)
            is_test_write = edit_path is not None and is_test_path(edit_path, plan, root)
            after: str | None = None
            if is_test_write and edit_path is not None and isinstance(args, dict):
                path = edit_path
                before = _cat(box, path, root)
                after = _after_edit(name, args, before.stdout if before.ok else None)
                removed = (
                    removed_tests(before.stdout, after) if before.ok and after is not None else []
                )
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
                    base_file = _cat(base, path, root)
                    on_base[key] = base_file.ok
                    base_tests[key] = set(test_nodes(base_file.stdout)) if base_file.ok else set()
            wanted = _read_range(args) if read_cache and name == "read_file" else None
            resent: tuple[int, int, int] | None = None
            if wanted is not None:
                rpath, rstart, rend = wanted
                seen = memory.covering(rpath, rstart, rend, visible)
                if seen is not None:
                    turns += 1
                    idle += 1
                    since, first, last = seen
                    messages.append(
                        _tool_message(
                            cid,
                            REREAD_MSG.format(turn=since, path=rpath, first=first, last=last),
                        )
                    )
                    note("reread", path=rpath, first=first, last=last, since_turn=since)
                    hooks.step(f"read_file {rpath} → unchanged since turn {since}")
                    continue
                resent = memory.covering(rpath, rstart, rend, lambda _: True)
            result = dispatch.dispatch(box, name, raw)
            box = result.box
            turns += 1
            hooks.step(_step_text(name, args, result.exit_code))
            message = as_message(result, cid)
            if resent is not None and result.ok:
                message["content"] = RESENT_PREFIX.format(turn=resent[0]) + message["content"]
            messages.append(message)
            if result.refused is not None and edit_path is not None:
                note(
                    "refused_shrink",
                    path=edit_path,
                    **{k: v for k, v in result.refused.items() if k != "reason"},
                )
                hooks.step(f"{name} {edit_path} → refused: would shrink the file")
            mutating = name in (*EDIT_TOOLS, "run_shell") and result.exit_code is not None
            idle = 0 if mutating else idle + 1
            if name == "run_shell" and result.exit_code is not None:
                memory.forget(None)  # a command may have rewritten any file
                command = args.get("command") if isinstance(args, dict) else None
                if isinstance(command, str) and _TEST_CMD.search(command):
                    green_at = turns if result.exit_code == 0 and tests_added else None
            if edit_path is not None and result.ok:
                green_at = None  # an edit after the green run: it no longer speaks for the fix
            if wanted is not None and result.ok and result.lines and result.total_lines is not None:
                memory.record(
                    wanted[0], *result.lines, result.total_lines, turns, len(messages) - 1
                )
            if edit_path is not None and result.ok:
                memory.forget(edit_path)
                path = edit_path
                first_write = path if first_write is None else first_write
                key = _norm(path, root)
                if is_test_write and not on_base.get(key, True):
                    if key not in tests_added:
                        tests_added.append(key)
                elif is_test_write and after is not None:  # existing file: new tests are nodes
                    for name_ in test_nodes(after):
                        node = f"{key}::{name_}"
                        if name_ not in base_tests[key] and node not in tests_added:
                            tests_added.append(node)
        # after the whole batch: a user message may not split tool_calls from their results
        if (
            report is None
            and turns < max_tool_turns
            and idle_nudge_after
            and idle >= idle_nudge_after
        ):
            left = max(0, max_tool_turns - turns)
            nudge = IDLE_NUDGE.format(idle=idle, left=left, budget=max_tool_turns)
            messages.append({"role": "user", "content": nudge})
            note("nudge", idle=idle, tool_turns=turns)
            hooks.step(f"nudge: {idle} turns without an edit")
            idle = 0
        elif (
            report is None
            and turns < max_tool_turns
            and not finish_nudged
            and green_at is not None
            and turns - green_at >= FINISH_NUDGE_AFTER
        ):
            messages.append({"role": "user", "content": FINISH_NUDGE})
            note("finish_nudge", green_turn=green_at, tool_turns=turns)
            hooks.step(f"nudge: tests green since turn {green_at}, no finish")
            finish_nudged = True

    if report is None:
        note("budget", tool_turns=turns)
        hooks.step(f"tool budget exhausted after {turns} turns")
    if autofix and first_write is not None:
        fixed = dispatch.run(box, autofix, timeout=600, keep=True)
        if fixed.box is not None:
            box = fixed.box
        note("autofix", command=autofix, exit_code=fixed.exit_code, image_uuid=box.uuid)
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
        done=report is not None and report.done,
    )
