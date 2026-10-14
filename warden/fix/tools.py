"""Tool JSON schemas: list_files, read_file, grep, write_file, run_shell (repo root, 600 s).

`TOOL_SCHEMAS` is what Lightning routes over; `ToolDispatcher` validates the model's JSON args
with the pydantic models in `warden.schemas`, executes ONLY through `Box` (`run` / `copy_in`) and
hands back a `ToolResult` carrying the checkpoint to continue from (a child after write_file /
run_shell, the input Box for reads and for every failure). Model mistakes (unknown tool, bad args)
never raise — they come back as `ok=False` with a retry-able message; backend exceptions propagate.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from warden.runlog.events import EventLog
from warden.sandbox.box import Box, RunResult
from warden.schemas import (
    GrepArgs,
    ListFilesArgs,
    Phase,
    ReadFileArgs,
    RunShellArgs,
    ToolArgs,
    ToolName,
    WriteFileArgs,
)

TOOL_NAMES: tuple[ToolName, ...] = ("list_files", "read_file", "grep", "write_file", "run_shell")

_ARGS: dict[str, type[ToolArgs]] = {
    "list_files": ListFilesArgs,
    "read_file": ReadFileArgs,
    "grep": GrepArgs,
    "write_file": WriteFileArgs,
    "run_shell": RunShellArgs,
}

_DESCRIPTIONS: dict[str, str] = {
    "list_files": (
        "List files under a directory, recursively; paths are relative to the repo root; "
        "call this first to find code and tests."
    ),
    "read_file": (
        "Return the contents of one file. Long files come in pages of whole lines with a note "
        "telling you the start_line to pass to continue; read every page before write_file."
    ),
    "grep": (
        "Search file contents for a pattern under a directory (like grep -rn) and get "
        "path:line:text matches; use it to find definitions and call sites before editing."
    ),
    "write_file": (
        "Create or replace one whole file with content (no diffs or partial edits); "
        "path relative to the repo root."
    ),
    "run_shell": (
        "Run a shell command at the repo root (e.g. pytest -q backend/tests/test_x.py) and get "
        "stdout, stderr and exit code; timeout defaults to 600 s."
    ),
}

_FIND = "find {path} -type f -not -path '*/.git/*' -not -path '*/__pycache__/*' | sort"
_GREP = "grep -rnIE --exclude-dir=.git -e {pattern} -- {path}"


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Strict-shaped parameters: every property required, no extras, no title/default noise."""
    props = {
        name: {k: v for k, v in prop.items() if k not in ("title", "default")}
        for name, prop in schema.get("properties", {}).items()
    }
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


_strict_schema = strict_schema  # backwards-compatible alias


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": _DESCRIPTIONS[name],
            "parameters": strict_schema(_ARGS[name].model_json_schema()),
        },
    }
    for name in TOOL_NAMES
]


@dataclass(frozen=True)
class ToolResult:
    name: str
    ok: bool
    output: str  # the exact text the model gets back
    exit_code: int | None  # None for unknown tool / invalid args
    box: Box  # child after write_file / run_shell; the input Box otherwise or on failure
    ms: int
    truncated: bool


def truncate(text: str, limit: int = 8000) -> tuple[str, bool]:
    """Head + tail of `text` when it exceeds `limit`; `(text, False)` when it fits."""
    total = len(text)
    if total <= limit:
        return text, False
    half = limit // 2
    dropped = total - 2 * half
    marker = f"\n…[truncated {dropped} of {total} chars]…\n"
    return text[:half] + marker + text[total - half :], True


def page_lines(text: str, start_line: int, limit: int) -> tuple[str | None, bool]:
    """Whole lines from `start_line` up to `limit` chars, plus a continue hint when cut.
    Unlike `truncate` this never drops the middle: a model rewriting a whole file must see all
    of it. `None` when start_line is past the end. A single over-long line is cut by chars."""
    lines = text.splitlines(keepends=True)
    total = len(lines)
    if start_line > max(total, 1):
        return None, False
    out: list[str] = []
    size = 0
    for line in lines[start_line - 1 :]:
        if out and size + len(line) > limit:
            break
        out.append(line[:limit])
        size += len(out[-1])
    end = start_line - 1 + len(out)
    if end >= total:
        return "".join(out), False
    hint = (
        f"…[lines {start_line}-{end} of {total} shown; call read_file with "
        f"start_line={end + 1} for the rest]"
    )
    body = "".join(out)
    return f"{body if body.endswith(chr(10)) else body + chr(10)}{hint}", True


def as_message(result: ToolResult, tool_call_id: str) -> dict[str, Any]:
    """The `role: tool` message the B3 loop appends verbatim."""
    return {"role": "tool", "tool_call_id": tool_call_id, "content": result.output}


def _format_errors(name: str, err: ValidationError) -> str:
    lines = [f"invalid arguments for {name}:"]
    for e in err.errors(include_url=False):
        loc = ".".join(str(p) for p in e["loc"]) or "<args>"
        lines.append(f"  {loc}: {e['msg']}")
    return "\n".join(lines)


_ARG_MAX = 200


def scrub_args(args: Any) -> dict[str, Any]:
    """Event-safe view of tool args, by key, valid or not: `content` becomes `bytes`, every string
    is cut to 200 chars, anything else is a truncated repr; a non-dict is `{"raw": ...}`."""
    if not isinstance(args, dict):
        return {"raw": repr(args)[:_ARG_MAX]}
    out: dict[str, Any] = {}
    for key, value in args.items():
        k = str(key)[:_ARG_MAX]
        if k == "content":
            out["bytes"] = len(value.encode()) if isinstance(value, str) else len(repr(value))
        elif isinstance(value, str):
            out[k] = value[:_ARG_MAX]
        elif value is None or isinstance(value, bool | int | float):
            out[k] = value
        else:
            out[k] = repr(value)[:_ARG_MAX]
    return out


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


MIN_SHELL_TIMEOUT = 120


class ToolDispatcher:
    """Validates and executes one tool call over a Box; emits one `kind="tool"` event per call."""

    def __init__(
        self,
        log: EventLog,
        *,
        phase: Phase = "fix",
        cwd: str = "/app",
        max_output: int = 8000,
        read_limit: int = 40_000,
        shell_timeout: int = 600,
        shell_wrap: str = "",
    ) -> None:
        """`shell_wrap` prefixes run_shell commands (quoted as one argument), e.g.
        `with-services sh -c`, so Postgres/Redis are up: each sandbox run is a fresh process."""
        self._log = log
        self._shell_wrap = shell_wrap
        self._read_limit = read_limit
        self._phase: Phase = phase
        self._cwd = cwd
        self._max_output = max_output
        self._shell_timeout = shell_timeout
        self.calls = 0  # every dispatch, failures included: each cost a model round-trip

    @property
    def cwd(self) -> str:
        """The workdir every tool runs in (the repo root inside the image)."""
        return self._cwd

    def run(self, box: Box, command: str, *, timeout: int = 600, keep: bool = False) -> RunResult:
        """A command outside the model's tool budget (final test run, autofix), with the same
        workdir and service wrapper as run_shell; forks only with keep=True."""
        if self._shell_wrap:
            command = f"{self._shell_wrap} {shlex.quote(command)}"
        return box.run(command, cwd=self._cwd, timeout=timeout, keep=keep)

    def dispatch(self, box: Box, name: str, args: Any) -> ToolResult:
        """`args` is the parsed dict from `ChatResult.tool_calls[i]["arguments"]`; never raises on
        model mistakes (unknown tool, invalid args); backend exceptions propagate."""
        self.calls += 1
        t0 = time.perf_counter()
        error: str | None = None
        scrubbed = scrub_args(args)  # raw args on every failure path: never log file content
        if name not in _ARGS:
            error = f"unknown tool {name}; available: {', '.join(TOOL_NAMES)}"
            result = ToolResult(name, False, error, None, box, _ms(t0), False)
        else:
            try:
                parsed = _ARGS[name].model_validate(args)
            except ValidationError as err:
                error = _format_errors(name, err)
                result = ToolResult(name, False, error, None, box, _ms(t0), False)
            else:
                scrubbed = scrub_args(parsed.model_dump())
                result = self._execute(box, name, parsed, t0)
        data: dict[str, Any] = {
            "name": name,
            "args": scrubbed,
            "ok": result.ok,
            "exit_code": result.exit_code,
            "from": box.uuid,
            "to": result.box.uuid,
            "truncated": result.truncated,
        }
        if error is not None:
            data["error"] = error
        self._log.emit(self._phase, "tool", data, ms=result.ms)
        return result

    def _finish(
        self, box: Box, name: str, ok: bool, output: str, exit_code: int, t0: float
    ) -> ToolResult:
        text, cut = truncate(output, self._max_output)
        return ToolResult(name, ok, text, exit_code, box, _ms(t0), cut)

    def _read(self, box: Box, name: str, res: RunResult, t0: float) -> ToolResult:
        """Read-only tools: stdout on success, stderr on failure; the input Box either way."""
        return self._finish(
            box, name, res.ok, res.stdout if res.ok else res.stderr, res.exit_code, t0
        )

    def _execute(self, box: Box, name: str, parsed: ToolArgs, t0: float) -> ToolResult:
        cwd = self._cwd
        if isinstance(parsed, ListFilesArgs):
            shell = _FIND.format(path=shlex.quote(parsed.path))
            return self._read(box, name, box.run(shell, cwd=cwd, keep=False), t0)
        if isinstance(parsed, ReadFileArgs):
            res = box.run(f"cat {shlex.quote(parsed.path)}", cwd=cwd, keep=False)
            if not res.ok:
                return self._read(box, name, res, t0)
            text, cut = page_lines(res.stdout, parsed.start_line, self._read_limit)
            if text is None:
                n = len(res.stdout.splitlines())
                msg = f"start_line={parsed.start_line} is past the end ({n} lines)"
                return ToolResult(name, False, msg, 1, box, _ms(t0), False)
            return ToolResult(name, True, text, 0, box, _ms(t0), cut)
        if isinstance(parsed, GrepArgs):
            shell = _GREP.format(pattern=shlex.quote(parsed.pattern), path=shlex.quote(parsed.path))
            res = box.run(shell, cwd=cwd, keep=False)
            if res.exit_code == 1:
                return self._finish(box, name, True, "(no matches)", 1, t0)
            return self._read(box, name, res, t0)
        if isinstance(parsed, WriteFileArgs):
            content = parsed.content
            if content and not content.endswith("\n"):  # models drop it; linters flag W292
                content += "\n"
            data = content.encode()
            if parsed.path.startswith("/"):
                child = box.copy_in({parsed.path.lstrip("/"): data}, cwd="/")
            else:
                child = box.copy_in({parsed.path: data}, cwd=cwd)
            return self._finish(
                child, name, True, f"wrote {len(data)} bytes to {parsed.path}", 0, t0
            )
        if isinstance(parsed, RunShellArgs):
            # Models pick tight limits (10 s, 30 s) for installs and suites; a floor keeps a
            # slow first import from looking like a failure.
            timeout = min(self._shell_timeout, max(MIN_SHELL_TIMEOUT, parsed.timeout))
            command = parsed.command
            if self._shell_wrap:
                command = f"{self._shell_wrap} {shlex.quote(command)}"
            res = box.run(command, cwd=cwd, timeout=timeout, keep=True)
            output = res.stdout
            if res.stderr:
                output += "\n--- stderr ---\n" + res.stderr
            output += f"\n[exit {res.exit_code}]"
            return self._finish(res.box or box, name, res.ok, output, res.exit_code, t0)
        raise AssertionError(f"unhandled tool args {type(parsed).__name__}")  # pragma: no cover
