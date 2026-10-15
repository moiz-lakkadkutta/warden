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
    truncate,
)
from warden.llm.client import ChatResult
from warden.llm.cost import usd_for
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.schemas import Candidate, FixReport, Phase, Plan, RunTestsUnfixedArgs
from warden.verify.tail import keep_tail
from warden.verify.theater import DEFAULT_CMD as THEATER_CMD

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

# Theater retry only (run_implementer(unfixed=...)): the verifier's theater probe, on demand. The
# model's current test files are copied onto a fork of the unfixed base and run there; its own
# checkpoint, with the fix, is never touched, so checking "does it fail without the fix" cannot
# lose the fix (a stash the model forgets to pop would).
UNFIXED_TOOL_NAME = "run_tests_unfixed"
UNFIXED_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": UNFIXED_TOOL_NAME,
        "description": (
            "Run tests against the UNFIXED source: your current versions of the given test "
            "files are copied onto the code as it was before your fix, then pytest -rA runs "
            "them there. Your working copy is not changed. A good repro FAILS here (on an "
            "assertion about the issue's behaviour) and passes in run_shell."
        ),
        "parameters": strict_schema(RunTestsUnfixedArgs.model_json_schema()),
    },
}

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
    tool_schemas: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    tools = "\n".join(
        f"- {t['function']['name']} — {t['function']['description']}"
        for t in (ALL_TOOLS if tool_schemas is None else tool_schemas)
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


KEEP_RECENT = 6  # newest large tool results (and write_file calls) sent whole
ELIDE_MIN = 1500  # shorter outputs are cheap to keep


def compact_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The history to send: older tool outputs become a one-line stub and older write_file
    calls lose their content (the file is in the box, a read_file away). Without this every
    earlier page is re-sent on every turn: on the SWE-rebench pilot prompts reached 173k tokens
    (pennylane-5831, 3.0M prompt tokens in 34 calls). Pairing and order are untouched. Only large
    outputs count towards the KEEP_RECENT window: short ones (a cached re-read note, an exit code)
    used to push the page they referred to out of it (camp-replay-212-c-196)."""
    tool_idx = _large_tool_idx(messages)
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
# Fabric campaign (camp-replay-212-b-200): after the note, Super asked for the same 14-line tail
# of an 813-line test file 40 more times in one candidate (44 of its 60 turns). From the
# REREAD_ESCALATE_AFTER+1-th cached answer for one file (since it last changed) the note says
# plainly that reading again cannot help and names the only useful next calls.
REREAD_ESCALATE_AFTER = 2
REREAD_STOP_MSG = (
    "STOP re-reading {path}: this is request {n} for text you already have. Lines {first}-{last} "
    "are unchanged and were shown to you at turn {turn}; they are in this conversation, and "
    "reading again shows nothing new. Your next call must be one of: replace_in_file (to add a "
    "test at the end of a long file, pass its last few lines as `old` and those lines plus the "
    "new test as `new`), write_file for a NEW short test file, run_shell to run the tests, or "
    "finish (done=false with the unmet items if you are stuck)."
)
# The note alone did not break the loop live (camp-replay-212-c: 16 cached answers for a 50-line
# file on #196, 13 of them escalated, and no edit). After an escalated answer the next model calls
# are sent without the read-only tools, until it edits a file, runs a command or EDIT_MODE_TURNS
# turns pass: it can write, replace, run or finish, nothing else.
READ_ONLY_TOOLS = frozenset({"list_files", "read_file", "grep"})
EDIT_MODE_TURNS = 3
EDIT_MODE_MSG = (
    "read_file, grep and list_files are switched off for your next turns: you have the text. "
    "Make the edit (replace_in_file / write_file), run the tests (run_shell), or finish."
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

# Auto-finish. Campaign + hunter replays: 13 of 15 Fabric attempts never called `finish`, and the
# hunter replay 20261015-175548-8d5d's chosen candidate (service-layer fix, valid repro) went green
# at turn 58 of 60 and was then marked "did not finish" by the verifier. A candidate with new tests,
# a source edit and a green test run is finished by Warden instead: AUTO_FINISH_AFTER turns after the
# green run without another edit or `finish`, or at the turn cap, once Warden has re-run the new
# tests and the lint command itself (both outside the turn budget). The verifier still judges it.
AUTO_FINISH_AFTER = 4
AUTO_FINISH_DECLINED = (
    "Warden checked your work before finishing for you and it is not ready: {why}. Fix that, run "
    "the tests again, then call finish."
)


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
        self._repeats: dict[str, int] = {}

    def forget(self, path: str | None) -> None:
        if path is None:
            self._seen.clear()
            self._repeats.clear()
        else:
            self._seen.pop(_norm(path, self._root), None)
            self._repeats.pop(_norm(path, self._root), None)

    def repeat(self, path: str) -> int:
        """Count one more cached answer for `path` since it last changed; return the count."""
        key = _norm(path, self._root)
        self._repeats[key] = self._repeats.get(key, 0) + 1
        return self._repeats[key]

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


def _large_tool_idx(messages: list[dict[str, Any]]) -> list[int]:
    """Indexes of the tool results compact_history may elide (longer than ELIDE_MIN)."""
    return [
        i
        for i, m in enumerate(messages)
        if m["role"] == "tool" and len(m["content"] or "") > ELIDE_MIN
    ]


def _visible(messages: list[dict[str, Any]], idx: int) -> bool:
    """Will message `idx` still be sent whole by `compact_history` after one more tool result?"""
    if len(messages[idx]["content"] or "") <= ELIDE_MIN:
        return True
    return idx in _large_tool_idx(messages)[-(KEEP_RECENT - 1) :]


def _read_range(args: Any, page: int = 0) -> tuple[str, int, int | None] | None:
    """`(path, start_line, end_line)` of a read_file call, tolerated as the dispatcher would.
    With `page` (> 0) the end is the last line one call can serve: a whole-file read of a long
    file returns its first page, so asking again is a repeat of that page."""
    fixed = tolerate_args("read_file", args).args
    if not isinstance(fixed, dict):
        return None
    path, start, end = fixed.get("path"), fixed.get("start_line", 1), fixed.get("end_line")
    if not isinstance(path, str) or not path.strip() or not isinstance(start, int):
        return None
    end = end if isinstance(end, int) else None
    if page > 0:
        last = start + page - 1
        end = last if end is None else min(end, last)
    return path, start, end


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


def run_unfixed(
    box: Box, unfixed: Box, args: Any, *, dispatch: ToolDispatcher, plan: Plan, root: str
) -> tuple[str, int | None, list[str]]:
    """`run_tests_unfixed`: (text for the model, exit code or None on bad args, targets)."""
    # Checked by hand, not by RunTestsUnfixedArgs.model_validate: this module's one except
    # clause stays parse_finish's. A bare string is tolerated as a one-item list.
    tests = args.get("tests") if isinstance(args, dict) else None
    tests = [tests] if isinstance(tests, str) else tests
    if not isinstance(tests, list) or not all(isinstance(t, str) for t in tests):
        return (
            f"invalid arguments for {UNFIXED_TOOL_NAME}: tests must be a list of test files "
            "or node ids",
            None,
            [],
        )
    targets = [_norm(t, root) for t in tests if t.strip()]
    files = sorted({t.split("::", 1)[0] for t in targets})
    not_tests = [f for f in files if not is_test_path(f, plan, root)]
    if not targets or not_tests:
        return (
            f"{UNFIXED_TOOL_NAME} copies test files only; not a test file: "
            f"{', '.join(not_tests) or '(none given)'}",
            None,
            targets,
        )
    content: dict[str, bytes] = {}
    for f in files:
        got = _cat(box, f, root)
        if not got.ok:
            return f"{f}: no such file in your working copy", None, targets
        content[f] = got.stdout.encode()
    probe = unfixed.copy_in(content, cwd=root)
    cmd = keep_tail(f"{THEATER_CMD} {' '.join(shlex.quote(t) for t in targets)}")
    res = dispatch.run(probe, cmd, timeout=600)
    text, _ = truncate(res.stdout + res.stderr, 6000)
    head = f"exit {res.exit_code} on the UNFIXED source (your test files copied in)\n"
    return head + text, res.exit_code, targets


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
    unfixed: Box | None = None,
    prior_tests: list[str] | None = None,
) -> Candidate:
    """Drive the implementer over `base` until `finish` or the tool-turn budget; return the
    Candidate whose `image_uuid` is the last checkpoint. A "turn" is one model action: a
    dispatched call, a refused write, a cached re-read, an invalid `finish` or an unroutable
    prose reply. `idle_nudge_after` (0 = off) and `read_cache` are the read-loop guards.
    `variant` (best-of-N) is appended to the user message so candidates do not converge; a theater
    retry puts the verifier's feedback there. `unfixed` (theater retry: `base` is then the
    candidate checkpoint) offers `run_tests_unfixed` against that unfixed base; `prior_tests` are
    the earlier attempt's new tests, kept in `tests_added`."""
    dispatch = dispatcher if dispatcher is not None else ToolDispatcher(log)
    hooks: Progress = progress if progress is not None else _NoProgress()

    def note(what: str, **data: Any) -> None:
        log.emit(PHASE, "note", {"implementer": what, **data})

    prompt_text = PROMPT_PATH.read_text(encoding="utf-8")
    root = dispatch.cwd
    tools = ALL_TOOLS if unfixed is None else [*ALL_TOOLS, UNFIXED_TOOL]
    messages = build_messages(
        prompt_text,
        plan,
        max_tool_turns,
        workdir=root,
        lint=lint,
        variant=variant,
        read_cache=read_cache,
        tool_schemas=tools,
    )
    box = base
    turns = 0
    tests_added: list[str] = list(prior_tests or [])
    first_write: str | None = None
    cost = 0.0
    report: FixReport | None = None
    on_base: dict[str, bool] = {}
    base_tests: dict[str, set[str]] = {}  # test function names per test file on base
    memory = ReadMemory(root)
    idle = 0  # tool turns since the last write_file / run_shell
    green_at: int | None = None  # turn of the last passing test run with no edit since
    finish_nudged = False
    # a theater retry continues from the candidate, whose fix is already in `base`
    src_edited = unfixed is not None  # a successful edit to a non-test file
    auto_checked: int | None = None  # the green turn the auto-finish check last ran for
    auto: str | None = None  # "green" | "cap": Warden finished for the model
    page = dispatch.read_page_lines
    edit_mode_until = 0  # while turns < this, the read-only tools are withheld
    edit_mode_pending = False  # tell the model, after the current batch
    edit_tools = [t for t in tools if t["function"]["name"] not in READ_ONLY_TOOLS]

    def ready(at: Box) -> tuple[Box, str]:
        """Warden's own check before finishing for the model, outside the turn budget: the new
        tests pass and lint is clean (after `autofix`, kept only if it makes lint clean).
        `(box to finish from, "")`, or `(box, why not)`."""
        cmd = "pytest -q " + " ".join(shlex.quote(p) for p in tests_added)
        res = dispatch.run(at, cmd, timeout=600)
        if res.exit_code != 0:
            tail = (res.stdout + res.stderr)[-300:].strip()
            return at, f"`{cmd}` exits {res.exit_code}:\n{tail}"
        if not lint:
            return at, ""
        checked = dispatch.run(at, lint, timeout=600)
        if checked.exit_code == 0:
            return at, ""
        if autofix:
            fixed = dispatch.run(at, autofix, timeout=600, keep=True)
            if fixed.box is not None and dispatch.run(fixed.box, lint, timeout=600).ok:
                return fixed.box, ""
        tail = (checked.stdout + checked.stderr)[-300:].strip()
        return at, f"`{lint}` exits {checked.exit_code}:\n{tail}"

    def viable() -> bool:
        return report is None and src_edited and bool(tests_added)

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
        **({} if unfixed is None else {"unfixed": unfixed.uuid, "prior_tests": tests_added[:]}),
    )
    while report is None and turns < max_tool_turns:
        offered = edit_tools if turns < edit_mode_until else tools
        res = llm.chat(Role.IMPLEMENTER, PHASE, compact_history(messages), tools=offered)
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
                tools=offered,
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
            if turns < edit_mode_until and name in READ_ONLY_TOOLS:  # called though not offered
                turns += 1
                messages.append(_tool_message(cid, f"refused: {EDIT_MODE_MSG}"))
                note("edit_mode_refused", name=name, tool_turns=turns)
                continue
            if name == UNFIXED_TOOL_NAME and unfixed is not None:
                text, code, targets = run_unfixed(
                    box, unfixed, args, dispatch=dispatch, plan=plan, root=root
                )
                turns += 1
                idle = 0
                messages.append(_tool_message(cid, text))
                note("unfixed_run", tests=targets, exit_code=code)
                hooks.step(f"{UNFIXED_TOOL_NAME} {' '.join(targets)} → exit {code}")
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
            wanted = _read_range(args, page) if read_cache and name == "read_file" else None
            resent: tuple[int, int, int] | None = None
            if wanted is not None:
                rpath, rstart, rend = wanted
                seen = memory.covering(rpath, rstart, rend, visible)
                if seen is not None:
                    turns += 1
                    idle += 1
                    since, first, last = seen
                    n = memory.repeat(rpath)
                    escalated = n > REREAD_ESCALATE_AFTER
                    fmt = REREAD_STOP_MSG if escalated else REREAD_MSG
                    messages.append(
                        _tool_message(
                            cid,
                            fmt.format(turn=since, path=rpath, first=first, last=last, n=n + 1),
                        )
                    )
                    note(
                        "reread",
                        path=rpath,
                        first=first,
                        last=last,
                        since_turn=since,
                        repeat=n,
                        escalated=escalated,
                    )
                    hooks.step(f"read_file {rpath} → unchanged since turn {since}")
                    if escalated and turns >= edit_mode_until:
                        edit_mode_until = turns + EDIT_MODE_TURNS
                        edit_mode_pending = True
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
                reason = result.refused.get("reason")
                note(
                    "refused_large" if reason == "large" else "refused_shrink",
                    path=edit_path,
                    **{k: v for k, v in result.refused.items() if k != "reason"},
                )
                why = "file too large to rewrite" if reason == "large" else "would shrink the file"
                hooks.step(f"{name} {edit_path} → refused: {why}")
            mutating = name in (*EDIT_TOOLS, "run_shell") and result.exit_code is not None
            idle = 0 if mutating else idle + 1
            if mutating:
                edit_mode_until = 0  # it acted: the read tools come back
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
                src_edited = src_edited or not is_test_write
                path = edit_path
                first_write = path if first_write is None else first_write
                key = _norm(path, root)
                if is_test_write and not on_base.get(key, True):
                    if key not in tests_added:
                        tests_added.append(key)
                elif is_test_write and after is not None:  # existing file: new tests are nodes
                    for name_ in test_nodes(after):
                        node = f"{key}::{name_}"
                        new = name_ not in base_tests[key] and key not in tests_added
                        if new and node not in tests_added:
                            tests_added.append(node)
        # after the whole batch: a user message may not split tool_calls from their results
        if edit_mode_pending and report is None:
            edit_mode_pending = False
            messages.append({"role": "user", "content": EDIT_MODE_MSG})
            note("edit_mode", tool_turns=turns, until_turn=edit_mode_until)
            hooks.step(f"edit mode: read tools off until turn {edit_mode_until}")
        elif (
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
        if (
            viable()
            and turns < max_tool_turns
            and green_at is not None
            and auto_checked != green_at
            and turns - green_at >= AUTO_FINISH_AFTER
        ):
            auto_checked = green_at
            box, why = ready(box)
            if not why:
                auto = "green"
                break
            messages.append({"role": "user", "content": AUTO_FINISH_DECLINED.format(why=why)})
            note("auto_finish_declined", green_turn=green_at, tool_turns=turns, why=why[:300])
            hooks.step(f"auto-finish declined: {why.splitlines()[0]}")

    if report is None and auto is None:
        note("budget", tool_turns=turns)
        hooks.step(f"tool budget exhausted after {turns} turns")
        if viable():  # the cap: finish from here when the work checks out
            box, why = ready(box)
            if not why:
                auto = "cap"
            else:
                note("auto_finish_declined", green_turn=green_at, tool_turns=turns, why=why[:300])
    if autofix and first_write is not None:
        fixed = dispatch.run(box, autofix, timeout=600, keep=True)
        if fixed.box is not None:
            box = fixed.box
        note("autofix", command=autofix, exit_code=fixed.exit_code, image_uuid=box.uuid)
    test_first = first_write is not None and is_test_path(first_write, plan, root)
    note("tdd_order", first_write=first_write, test_first=test_first)
    final_exit: int | None = None
    if tests_added:
        cmd = "pytest -q " + " ".join(shlex.quote(p) for p in tests_added)
        final = dispatch.run(box, cmd, timeout=600)
        final_exit = final.exit_code
        note(
            "final_tests",
            tests=list(tests_added),
            exit_code=final.exit_code,
            tail=(final.stdout + final.stderr)[-500:],
        )
    if auto is not None and final_exit != 0:
        auto = None  # autofix broke the new tests: not finished after all
    if auto is not None:
        note("auto_finish", reason=auto, green_turn=green_at, tool_turns=turns)
        hooks.step(f"auto-finish ({auto}): new tests green, source edited, lint clean")
    done = (report is not None and report.done) or auto is not None
    note(
        "done" if done else "blocked",
        tool_turns=turns,
        tests_added=list(tests_added),
        cost_usd=round(cost, 6),
        image_uuid=box.uuid,
        report=None if report is None else report.model_dump(),
        **({"auto_finish": auto} if auto is not None else {}),
    )
    return Candidate(
        issue=plan.issue,
        image_uuid=box.uuid,
        tool_turns=turns,
        tests_added=list(tests_added),
        cost_usd=round(cost, 6),
        done=done,
    )
