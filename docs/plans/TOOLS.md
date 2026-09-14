# TOOLS plan — fix/tools.py: JSON tool schemas + dispatcher over Box

## 1. Goal
Five OpenAI-style function tools (list_files, read_file, grep, write_file, run_shell) that Lightning can route,
plus a `ToolDispatcher` that validates the model's JSON args with pydantic, executes ONLY through `Box`
(`run` / `copy_in`), returns a `ToolResult` carrying the checkpoint to continue from (new child after
write_file / run_shell, same Box for reads), truncates output head+tail, never raises on model mistakes
(unknown tool, bad args → `ok=False` with a retry-able message), and emits one `kind="tool"` RunEvent per
call. Interface is final: B3's TDD loop only threads `result.box` and counts turns.

## 2. Signatures (exact)
warden/schemas.py — ADDITIVE only (tool args ARE model output → keeps the "all model output is a pydantic
schema in schemas.py" rule literally true; `ToolResult` stays in tools.py because it holds a `Box` and
schemas.py must not import sandbox code — same ruling as GateResult):
- `ToolName = Literal["list_files", "read_file", "grep", "write_file", "run_shell"]`
- `class ToolArgs(BaseModel)`: `model_config = ConfigDict(extra="forbid")` (→ `additionalProperties: false`).
- `class ListFilesArgs(ToolArgs)`: `path: str = "."`
- `class ReadFileArgs(ToolArgs)`: `path: str`
- `class GrepArgs(ToolArgs)`: `pattern: str`, `path: str = "."`
- `class WriteFileArgs(ToolArgs)`: `path: str`, `content: str`
- `class RunShellArgs(ToolArgs)`: `command: str`, `timeout: int = Field(600, ge=1, le=600)`
  Every field carries `Field(description=...)` written for Lightning (one sentence, cwd semantics); the
  descriptions flow into the JSON schema, so prompt text and schema never drift.

warden/fix/tools.py:
- `TOOL_NAMES: tuple[ToolName, ...]` in the order list_files, read_file, grep, write_file, run_shell.
- `TOOL_SCHEMAS: list[dict[str, Any]]` — one `{"type": "function", "function": {"name", "description",
  "parameters"}}` per tool, built at import from `<Args>.model_json_schema()` via `_strict_schema(schema)`:
  `required` = ALL property names (strict shape; pydantic defaults still forgive omissions at validation),
  `additionalProperties: false`, `title`/`default` keys stripped (fewer tokens). No `"strict": true` key on
  the function object (Token Factory support unverified — risk 2). Descriptions (one sentence each):
  list_files "List files under a directory, recursively; paths are relative to /app; call this first to
  find code and tests." · read_file "Return the whole contents of one file; path relative to /app unless
  absolute." · grep "Search file contents for a pattern under a directory (like grep -rn) and get
  path:line:text matches; use it to find definitions and call sites before editing." · write_file "Create
  or replace one whole file with content (no diffs or partial edits); path relative to /app." · run_shell
  "Run a shell command in /app (e.g. pytest -q tests/test_x.py) and get stdout, stderr and exit code;
  timeout defaults to 600 s."
- `@dataclass(frozen=True) class ToolResult`: `name: str`, `ok: bool`, `output: str` (the exact text the
  model gets back), `exit_code: int | None` (None for unknown tool / invalid args), `box: Box` (child
  checkpoint after write_file / run_shell, the input Box otherwise or on any failure), `ms: int`,
  `truncated: bool`.
- `def truncate(text: str, limit: int = 8000) -> tuple[str, bool]` — head `limit//2` + marker
  `"\n…[truncated {dropped} of {total} chars]…\n"` + tail `limit//2`; `(text, False)` when it fits.
- `class ToolDispatcher`: `__init__(self, log: EventLog, *, phase: Phase = "fix", cwd: str = "/app",
  max_output: int = 8000, shell_timeout: int = 600)`; attribute `calls: int` (every dispatch counts, including
  unknown/invalid — each cost a model round-trip); `dispatch(self, box: Box, name: str, args: Any) ->
  ToolResult` (args is the parsed dict from `ChatResult.tool_calls[i]["arguments"]`; a non-dict counts as
  invalid args).
- `def as_message(result: ToolResult, tool_call_id: str) -> dict[str, Any]` →
  `{"role": "tool", "tool_call_id": ..., "content": result.output}` (the B3 loop appends this verbatim).
Dispatch rules: unknown name → `ok=False, exit_code=None, output="unknown tool X; available: …"`; pydantic
`ValidationError` → `ok=False, exit_code=None, output="invalid arguments for X: " + one line per error
(`loc: msg`, no pydantic URLs)`; backend exceptions propagate (as in gate.py). Output is truncated to
`max_output` for every tool. All shell args go through `shlex.quote`. Per call one
`log.emit(phase, "tool", {"name", "args", "ok", "exit_code", "from": box.uuid, "to": result.box.uuid,
"truncated", "error"?}, ms=ms)` AFTER the Box call (so the sandbox event precedes it); `args` is scrubbed:
write_file → `{"path", "bytes"}` (never content), run_shell → `{"command"[:200], "timeout"}`, others verbatim.

## 3. Tool table (name → params → Box calls → mutating?)
| tool | params | Box call (cwd=/app) | output to model | ok | mutating |
|---|---|---|---|---|---|
| list_files | path="." | `run("find <p> -type f -not -path '*/.git/*' -not -path '*/__pycache__/*' \| sort", keep=False)` | stdout (paths spelled as given, find-style) | exit 0 | no |
| read_file | path | `run("cat <p>", keep=False)` — not `Box.read_file` (it raises and drops the exit code) | stdout, or stderr on failure | exit 0 | no |
| grep | pattern, path="." | `run("grep -rnI --exclude-dir=.git -e <pat> -- <p>", keep=False)` | `path:line:text` lines; `"(no matches)"` when exit 1 | exit 0 or 1 | no |
| write_file | path, content | relative: `copy_in({path: content.encode()}, cwd=cwd)`; absolute: `copy_in({path.lstrip("/"): …}, cwd="/")` | `"wrote {n} bytes to {path}"` | always (child exists) | yes → child |
| run_shell | command, timeout=600 | `run(command, cwd=cwd, timeout=min(timeout, shell_timeout), keep=True)` | stdout + (`"\n--- stderr ---\n"`+stderr if any) + `"\n[exit {code}]"` | exit 0 | yes → child |
run_shell always keeps (one checkpoint per command = the tool-turn lineage the console draws); `res.box is
None` → fall back to the input Box (defensive only).

## 4. FakeBackend vocabulary (fake.py; new branches in `_interpret` BEFORE the gate fallback; gate/pytest/echo/cat untouched)
- Path rule shared by all: relative → `{cwd}/{p}`; output paths are re-spelled with the argument as given
  (find/grep semantics), so `find src` prints `src/a.py`, `find /app/src` prints `/app/src/a.py`.
- `find <dir> [any flags ignored]`: every `img.files` key equal to dir or under `dir/`, sorted, one per
  line, exit 0; no such prefix → stderr `find: '<dir>': No such file or directory`, exit 1. Note `use()` seeds
  `/app/REF`, so `find .` lists `./REF` — tests list a subdirectory. Piped `| sort` is stripped (fake treats
  `|`-tails as no-ops: split on `|`, interpret the first segment).
- `ls [dir]`: immediate children (file names and first-level subdir names, deduped, sorted), exit 0.
- `grep [flags…] <pattern> <path>`: tokens starting with `-` are skipped (`-e` consumes the next token as the
  pattern; `--` skipped); case-sensitive SUBSTRING match per line over files under path (undecodable bytes
  skipped); prints `shown_path:lineno:line`; exit 0 with matches, 1 without, 2 + stderr if path missing.
- `false` → exit 1. Knob: `FakeBackend.timeouts: list[int]` appended on every `run` (parallel to `calls`) so
  tests can assert the 600 s default. Writes need nothing new: `copy_in` with `cwd="/"` yields `"/" + rel` keys.

## 5. Tests (tests/test_tools.py — FakeBackend + `log` fixture as in test_gate.py)
1. `test_tool_schemas_shape`: 5 entries in TOOL_NAMES order; each `type=="function"`; parameters `type
   object`, `additionalProperties is False`, `set(required)==set(properties)`, non-empty description, no `title` key.
2. `test_list_files_happy`: seed `/app/src/{a,b}.py`; `list_files(path="src")` → ok, output `src/a.py\nsrc/b.py\n`,
   `result.box is box`.
3. `test_read_file_happy_and_missing`: seeded file returned verbatim; missing → ok False, exit 1, "No such file".
4. `test_grep_match_and_no_match`: `path:2:def foo` line; no hit → ok True, exit_code 1, output "(no matches)".
5. `test_write_file_forks`: child uuid != parent; read_file on child sees content; on parent → ok False;
   `backend.images[parent.uuid].files` lacks the path; absolute path variant lands at that exact key.
6. `test_run_shell_happy`: `echo hi` → ok, exit 0, output starts "hi", ends "[exit 0]", new uuid,
   `backend.timeouts[-1] == 600`.
7. `test_run_shell_nonzero`: `pytest tests/t.py` with `test_results=1` → ok False, exit 1, output has
   summary line and "[exit 1]", box still a new child.
8. `test_truncation`: 20 000-char file, `max_output=1000` → len ≤ 1000+marker, starts with head, ends with
   tail, `truncated` True, marker names dropped count; small file → `truncated` False.
9. `test_unknown_tool`: ok False, exit_code None, output lists all five names, box unchanged, no raise.
10. `test_bad_args` (parametrised): `read_file {}` → "path"; `write_file` extra key → forbidden; `run_shell
    timeout 9999` → error; non-dict args → invalid; each ok False, exit_code None.
11. `test_every_call_emits_tool_event`: after 7 dispatches (incl. unknown + invalid) exactly 7 `kind=="tool"`
    events, each with name/ok/exit_code/ms; write_file event has `bytes` and no `content`; run_shell event's
    command ≤ 200 chars; for run_shell the preceding event is the `sandbox` one.
12. `test_calls_counter`: `calls` == number of dispatches, failures included.
13. `test_as_message`: role "tool", tool_call_id echoed, content == output.
14. `test_no_contree_import`: `"contree" not in inspect.getsource(warden.fix.tools)` and no `contree*`
    module in `sys.modules` after importing tools.
Existing tests (box/theater/gate/demo) run unchanged.

## 6. Done-when
`uv run ruff check . && uv run ruff format --check . && uv run mypy warden && uv run pytest -q` green (existing +
~16 new); commit `feat(TOOLS): tool schemas + dispatcher over Box` (tests as `test(TOOLS)`).

## 7. Risks
1. No `Box.write_file` — write goes through `copy_in`; for absolute paths `cwd="/"` relies on Contree
   `files=` accepting cwd "/" and creating parent dirs. Unverified until A6; if it fails, box.py needs a
   `write_file` (orchestrator rules). Fake models it exactly.
2. `"strict": true` on the function object omitted (Token Factory / vLLM tool-call strictness unknown);
   schema is strict-shaped regardless, and pydantic validation is the real guard.
3. `ContreeBackend.run` decodes stdout with strict `.decode()` — `cat` of a non-UTF-8 file would raise from
   box.py (forbidden here); recommend `errors="replace"` in a box.py follow-up.
4. Fake grep is substring, real grep is regex; fake `find` ignores exclusions; fake `|` stripping serves the
   list_files pipeline only (future piped commands must extend the fake). Tests use literal patterns.
5. A checkpoint per run_shell — Contree checkpoint cost/latency unmeasured (A3 numbers → FEEDBACK.md).
   `cwd` is /app; if Fabric's backend lives in `/app/backend` it is a constructor kwarg, not a code edit.

## 8. Size — tools.py ~150 lines, schemas.py +40, fake.py +55, tests/test_tools.py ~180. No other files.
