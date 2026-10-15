"""Tool JSON schemas: list_files, read_file, grep, write_file, replace_in_file, run_shell.

`TOOL_SCHEMAS` is what Lightning routes over; `ToolDispatcher` validates the model's JSON args
with the pydantic models in `warden.schemas`, executes ONLY through `Box` (`run` / `copy_in`) and
hands back a `ToolResult` carrying the checkpoint to continue from (a child after write_file /
run_shell, the input Box for reads and for every failure). Model mistakes (unknown tool, bad args)
never raise — they come back as `ok=False` with a retry-able message; backend exceptions propagate.

Arguments are tolerated before they are validated (`tolerate_args`): Nemotron Super invents keys
and sends integers as strings, and each rejection burned a whole turn (9 of 25 on icalendar#1928).
Unknown keys are dropped, known aliases renamed, numeric strings coerced, out-of-range integers
clamped and nulls for defaulted keys removed; every repair is recorded on the `tool` event. A
missing or empty required argument is still an error.

Write guards (pilot 3: Super wrote a 309-byte fragment over 19 kB pennylane exp.py and spent 30
turns recovering): write_file refuses to shrink a file of SHRINK_MIN_BYTES or more below
SHRINK_RATIO of its size unless `allow_shrink` is set, and replace_in_file edits one exact,
unique span so large files never need a whole-file rewrite.

Large-file guards (Fabric campaign, 2026-10-15: 13 of 15 attempts never called `finish`): read_file
serves at most READ_PAGE_LINES lines per call with a "lines X-Y of N" footer, and write_file refuses
to rewrite an existing file over LARGE_FILE_LINES lines or LARGE_FILE_BYTES bytes (a whole-file
resend of a 926-line test file was cut at the 8192-token completion limit 7 times in one run); the
refusal points at replace_in_file. A new file is never refused.
"""

from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass
from typing import Any

from annotated_types import Ge, Le
from pydantic import BaseModel, ValidationError

from warden.runlog.events import EventLog
from warden.sandbox.box import Box, RunResult
from warden.schemas import (
    GrepArgs,
    ListFilesArgs,
    Phase,
    ReadFileArgs,
    ReplaceInFileArgs,
    RunShellArgs,
    ToolArgs,
    ToolName,
    WriteFileArgs,
)

TOOL_NAMES: tuple[ToolName, ...] = (
    "list_files",
    "read_file",
    "grep",
    "write_file",
    "replace_in_file",
    "run_shell",
)

_ARGS: dict[str, type[ToolArgs]] = {
    "list_files": ListFilesArgs,
    "read_file": ReadFileArgs,
    "grep": GrepArgs,
    "write_file": WriteFileArgs,
    "replace_in_file": ReplaceInFileArgs,
    "run_shell": RunShellArgs,
}

_DESCRIPTIONS: dict[str, str] = {
    "list_files": (
        "List files under a directory, recursively; paths are relative to the repo root; "
        "call this first to find code and tests."
    ),
    "read_file": (
        "Return the lines of one file, at most 200 per call. A longer file ends with a "
        "'lines X-Y of N' note: pass start_line (and end_line) to read only the part you need."
    ),
    "grep": (
        "Search file contents for a pattern under a directory (like grep -rn) and get "
        "path:line:text matches; use it to find definitions and call sites before editing."
    ),
    "write_file": (
        "Create a new file, or replace a small existing file whole; path relative to the repo "
        "root. Refused for an existing file over 300 lines or 12 kB: use replace_in_file."
    ),
    "replace_in_file": (
        "Edit an existing file: replace the one exact occurrence of `old` (copied from "
        "read_file, indentation included) with `new`; errors if `old` is missing or not unique."
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
    # read_file only: the 1-based lines shown (first, last) and the file's line count
    lines: tuple[int, int] | None = None
    total_lines: int | None = None
    # a write the dispatcher refused before touching the box, e.g.
    # {"reason": "shrink", "old_bytes": 19000, "new_bytes": 309}; also on the tool event
    refused: dict[str, Any] | None = None


def truncate(text: str, limit: int = 8000) -> tuple[str, bool]:
    """Head + tail of `text` when it exceeds `limit`; `(text, False)` when it fits."""
    total = len(text)
    if total <= limit:
        return text, False
    half = limit // 2
    dropped = total - 2 * half
    marker = f"\n…[truncated {dropped} of {total} chars]…\n"
    return text[:half] + marker + text[total - half :], True


READ_PAGE_LINES = 200  # read_file serves at most this many lines per call


def page_lines(
    text: str, start_line: int, limit: int, max_lines: int = READ_PAGE_LINES
) -> tuple[str | None, bool]:
    """Whole lines from `start_line`, at most `max_lines` of them and `limit` chars, plus a
    continue hint when cut. Unlike `truncate` this never drops the middle. `None` when
    start_line is past the end. A single over-long line is cut by chars."""
    page, cut, _ = _paginate(text, start_line, limit, max_lines)
    return page, cut


def _paginate(
    text: str, start_line: int, limit: int, max_lines: int = READ_PAGE_LINES, total: int = 0
) -> tuple[str | None, bool, int]:
    """`page_lines` plus the number of the last line shown. `total` is the file's line count
    when `text` is a prefix of it (an end_line read), for the footer."""
    lines = text.splitlines(keepends=True)
    total = max(total, len(lines))
    if start_line > max(len(lines), 1):
        return None, False, 0
    out: list[str] = []
    size = 0
    for line in lines[start_line - 1 :]:
        if out and (size + len(line) > limit or len(out) >= max_lines):
            break
        out.append(line[:limit])
        size += len(out[-1])
    end = start_line - 1 + len(out)
    if end >= len(lines):  # the end of the file, or the requested end_line
        return "".join(out), False, end
    hint = (
        f"…[lines {start_line}-{end} of {total} shown (pages are at most {max_lines} lines); "
        f"call read_file with start_line={end + 1} for more, or grep for what you need]"
    )
    body = "".join(out)
    return f"{body if body.endswith(chr(10)) else body + chr(10)}{hint}", True, end


def as_message(result: ToolResult, tool_call_id: str) -> dict[str, Any]:
    """The `role: tool` message the B3 loop appends verbatim."""
    return {"role": "tool", "tool_call_id": tool_call_id, "content": result.output}


def _format_errors(name: str, err: ValidationError) -> str:
    lines = [f"invalid arguments for {name}:"]
    for e in err.errors(include_url=False):
        loc = ".".join(str(p) for p in e["loc"]) or "<args>"
        lines.append(f"  {loc}: {e['msg']}")
    if name == "write_file" and any(
        e["loc"] == ("content",) and e["type"] == "missing" for e in err.errors()
    ):
        lines.append(TRUNCATED_WRITE_HINT)
    return "\n".join(lines)


# camp-head-1-210: Super re-sent a whole 926-line test file 9 times; each reply hit the 8192-token
# completion limit and arrived with no `content`, and the bare "Field required" never said why.
TRUNCATED_WRITE_HINT = (
    "Your reply was most likely cut off at the completion limit: the file is too long to send "
    "whole. Do not retry write_file with the whole file. Use replace_in_file(path, old, new) to "
    "insert or change a few lines, or put new tests in a new, short test file."
)


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


# Names Nemotron reaches for instead of the declared ones; applied only when the declared key is
# absent, so a call that sends both keeps the declared value and the alias is ignored.
_ALIASES: dict[str, str] = {
    "file": "path",
    "file_path": "path",
    "filepath": "path",
    "filename": "path",
    "dir": "path",
    "directory": "path",
    "start": "start_line",
    "line": "start_line",
    "from_line": "start_line",
    "end": "end_line",
    "to_line": "end_line",
    "regex": "pattern",
    "query": "pattern",
    "cmd": "command",
    "contents": "content",
    "old_string": "old",
    "old_str": "old",
    "old_text": "old",
    "search": "old",
    "find": "old",
    "new_string": "new",
    "new_str": "new",
    "new_text": "new",
    "replacement": "new",
    "text": "content",
}
_INT_STR = re.compile(r"^\s*[+-]?\d+(?:\.0*)?\s*$")


@dataclass(frozen=True)
class Tolerated:
    """`args` repaired for validation, with what was changed (for the tool event)."""

    args: Any
    ignored: dict[str, Any]  # unknown keys dropped, with their raw values
    repaired: list[str]  # short notes: aliases, coercions, clamps, dropped nulls


def _int_field(model: type[BaseModel], key: str) -> bool:
    ann = model.model_fields[key].annotation
    return ann is int or ann == (int | None)


def _bounds(model: type[BaseModel], key: str) -> tuple[int | None, int | None]:
    lo: int | None = None
    hi: int | None = None
    for meta in model.model_fields[key].metadata:
        if isinstance(meta, Ge) and isinstance(meta.ge, int):
            lo = meta.ge
        elif isinstance(meta, Le) and isinstance(meta.le, int):
            hi = meta.le
    return lo, hi


def tolerate_args(name: str, args: Any) -> Tolerated:
    """Repair a tool call's arguments before strict validation; never raises. A non-dict or an
    unknown tool passes through untouched (validation reports it). Required keys stay strict:
    nothing here invents a missing `path`, `content` or `command`."""
    model = _ARGS.get(name)
    if model is None or not isinstance(args, dict):
        return Tolerated(args, {}, [])
    fields = model.model_fields
    out: dict[str, Any] = {str(k): v for k, v in args.items() if str(k) in fields}
    ignored: dict[str, Any] = {}
    repaired: list[str] = []
    for key, value in args.items():
        k = str(key)
        if k in fields:
            continue
        target = _ALIASES.get(k)
        if target is not None and target in fields and target not in out:
            out[target] = value
            repaired.append(f"{k}→{target}")
        else:
            ignored[k] = value
    for key in list(out):
        value = out[key]
        field = fields[key]
        if value is None and not field.is_required() and field.default is not None:
            del out[key]  # strict schemas make the model send null for a defaulted key
            repaired.append(f"{key}: null→default")
            continue
        if not _int_field(model, key):
            continue
        if isinstance(value, str) and _INT_STR.match(value):
            value = int(float(value))
            repaired.append(f"{key}: str→int")
        elif isinstance(value, float) and value.is_integer():
            value = int(value)
            repaired.append(f"{key}: float→int")
        if isinstance(value, int) and not isinstance(value, bool):
            lo, hi = _bounds(model, key)
            clamped = value if lo is None else max(lo, value)
            clamped = clamped if hi is None else min(hi, clamped)
            if clamped != value:
                repaired.append(f"{key}: {value}→{clamped}")
            value = clamped
        out[key] = value
    return Tolerated(out, ignored, repaired)


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


MIN_SHELL_TIMEOUT = 120
SHRINK_MIN_BYTES = 1024  # smaller files are never guarded
SHRINK_RATIO = 0.5  # a rewrite below this fraction of the old size is refused

SHRINK_MSG = (
    "refused: write_file would shrink {path} from {old} to {new} bytes. write_file replaces the "
    "WHOLE file, so a fragment deletes everything else. To change part of the file use "
    "replace_in_file; to rewrite it, read_file every page and send the complete file. Pass "
    "allow_shrink=true only if deleting most of the file is intended."
)


def shrinks(old: int, new: int) -> bool:
    """Would replacing `old` bytes with `new` clobber the file (see SHRINK_MSG)?"""
    return old >= SHRINK_MIN_BYTES and new < old * SHRINK_RATIO


# A whole-file rewrite of an existing file this large is refused (camp-head-1-210: 7 of 40
# replies re-sent a 926-line test file and were cut at the 8192-token completion limit).
LARGE_FILE_LINES = 300
LARGE_FILE_BYTES = 12_000

LARGE_MSG = (
    "refused: {path} already exists and is {lines} lines / {old} bytes; write_file does not "
    "rewrite an existing file over {max_lines} lines or {max_bytes} bytes (a whole-file reply that "
    "long gets cut off). Use replace_in_file(path, old, new) to change or insert a few lines: to "
    "add a test at the end, pass the file's last few lines as `old` and those lines plus the new "
    "test as `new`. Or put the new tests in a new, short test file with write_file."
)


def too_large(old_bytes: int, old_lines: int) -> bool:
    """Is an existing file too large to rewrite whole (see LARGE_MSG)?"""
    return old_lines > LARGE_FILE_LINES or old_bytes > LARGE_FILE_BYTES


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
        read_page_lines: int = READ_PAGE_LINES,
        shell_timeout: int = 600,
        shell_wrap: str = "",
    ) -> None:
        """`shell_wrap` prefixes run_shell commands (quoted as one argument), e.g.
        `with-services sh -c`, so Postgres/Redis are up: each sandbox run is a fresh process."""
        self._log = log
        self._shell_wrap = shell_wrap
        self._read_limit = read_limit
        self._read_page_lines = read_page_lines
        self._phase: Phase = phase
        self._cwd = cwd
        self._max_output = max_output
        self._shell_timeout = shell_timeout
        self.calls = 0  # every dispatch, failures included: each cost a model round-trip

    @property
    def read_page_lines(self) -> int:
        """The most lines one read_file call serves."""
        return self._read_page_lines

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
        fixed = tolerate_args(name, args)
        if name not in _ARGS:
            error = f"unknown tool {name}; available: {', '.join(TOOL_NAMES)}"
            result = ToolResult(name, False, error, None, box, _ms(t0), False)
        else:
            try:
                parsed = _ARGS[name].model_validate(fixed.args)
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
        if result.refused is not None:
            data["error"] = result.output
            data["refused"] = result.refused
        if fixed.ignored:  # tolerated, not rejected: kept on the event so the habit is measurable
            data["ignored_args"] = scrub_args(fixed.ignored)
        if fixed.repaired:
            data["repaired_args"] = fixed.repaired
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
            source = res.stdout
            n = len(res.stdout.splitlines())
            if parsed.end_line is not None:
                source = "".join(source.splitlines(keepends=True)[: parsed.end_line])
            text, cut, end = _paginate(
                source, parsed.start_line, self._read_limit, self._read_page_lines, n
            )
            if text is None:
                msg = f"start_line={parsed.start_line} is past the end ({n} lines)"
                return ToolResult(name, False, msg, 1, box, _ms(t0), False)
            span = (parsed.start_line, end)
            return ToolResult(name, True, text, 0, box, _ms(t0), cut, span, n)
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
            old = box.run(f"cat {shlex.quote(parsed.path)}", cwd=cwd, keep=False)
            if old.ok:
                size = len(old.stdout.encode())
                n_lines = len(old.stdout.splitlines())
                if too_large(size, n_lines):
                    msg = LARGE_MSG.format(
                        path=parsed.path,
                        lines=n_lines,
                        old=size,
                        max_lines=LARGE_FILE_LINES,
                        max_bytes=LARGE_FILE_BYTES,
                    )
                    refused = {
                        "reason": "large",
                        "old_bytes": size,
                        "old_lines": n_lines,
                        "new_bytes": len(data),
                    }
                    return ToolResult(name, False, msg, None, box, _ms(t0), False, refused=refused)
                if not parsed.allow_shrink and shrinks(size, len(data)):
                    msg = SHRINK_MSG.format(path=parsed.path, old=size, new=len(data))
                    refused = {"reason": "shrink", "old_bytes": size, "new_bytes": len(data)}
                    return ToolResult(name, False, msg, None, box, _ms(t0), False, refused=refused)
            child = self._copy_in(box, parsed.path, data)
            return self._finish(
                child, name, True, f"wrote {len(data)} bytes to {parsed.path}", 0, t0
            )
        if isinstance(parsed, ReplaceInFileArgs):
            res = box.run(f"cat {shlex.quote(parsed.path)}", cwd=cwd, keep=False)
            if not res.ok:
                return self._read(box, name, res, t0)
            text, err = replace_once(res.stdout, parsed.old, parsed.new)
            if err:
                msg = f"refused: replace_in_file on {parsed.path}: {err}"
                return ToolResult(name, False, msg, 1, box, _ms(t0), False)
            data = text.encode()
            child = self._copy_in(box, parsed.path, data)
            msg = (
                f"replaced 1 occurrence in {parsed.path} "
                f"({len(res.stdout.encode())} → {len(data)} bytes)"
            )
            return self._finish(child, name, True, msg, 0, t0)
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

    def _copy_in(self, box: Box, path: str, data: bytes) -> Box:
        if path.startswith("/"):
            return box.copy_in({path.lstrip("/"): data}, cwd="/")
        return box.copy_in({path: data}, cwd=self._cwd)


def replace_once(text: str, old: str, new: str) -> tuple[str, str]:
    """`(text with the one occurrence of old replaced, "")`, or `(text, error)` when `old` is
    missing or occurs more than once. Shared by the dispatcher and the implementer's
    test-deletion guard so both see the same result."""
    n = text.count(old)
    if n == 0:
        return text, (
            "`old` not found; read_file the region again and copy the exact text, "
            "indentation and blank lines included"
        )
    if n > 1:
        return text, (
            f"`old` matches {n} places; include more surrounding lines so it matches exactly one"
        )
    out = text.replace(old, new, 1)
    if out and not out.endswith("\n") and text.endswith("\n"):
        out += "\n"
    return out, ""
