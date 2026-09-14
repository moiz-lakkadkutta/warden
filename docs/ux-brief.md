# UX & Developer-Experience Brief

For the designer working on Warden (CLI + run console) and RegWatch (merchant web app). Read this once end to end; the per-surface sections stand alone afterwards.

## 1. What we are designing and who judges it

Two products, one deadline (Oct 28), one shared audience that matters more than any other: a hackathon judge with ten minutes. They will open the README, watch a 3-minute video, click the demo URL, and score four criteria equally: Technological Implementation, **Design** ("a complete, coherent product experience"), Potential Impact, Quality of the Idea. Design is a quarter of the score and the one criterion where a designer moves the number directly.

Second audience: the actual users. For Warden, a solo developer or a small team who will run it on their own repo and decide whether to trust a PR it opened. For RegWatch, a small EU shop owner who is not a lawyer and does not want to become one.

Design principle for both: **evidence over reassurance**. Every screen answers "how do I know this is true?" before it answers "what should I feel?" Agent products lose trust when they narrate; they win it when they show the command, the exit code, and the link.

## 2. Warden

### 2.1 Surfaces

1. Terminal output of the `warden` CLI (the first thing anyone sees).
2. The run console: a read-only web page per run, served from a Nebius Serverless Endpoint.
3. The pull-request body (rendered by GitHub; we control the Markdown).
4. README hero (GIF + three commands).

### 2.2 The user's mental model

Warden is a pipeline of five phases: **Hunt → Plan → Fix → Verify → PR**. Each phase has a start, an end, a cost, and an artefact. Every surface should make those five phases visible in the same order with the same names and the same five colours so that the terminal, the console and the PR feel like one product. Nothing in the product should use a sixth phase name.

Inside Verify there is a second concept the user has never seen before and must understand in one glance: a **branch tree**. The base state of the repo is forked into a candidate (the fix) and into a theater check (the new tests run against the *unfixed* base). The candidate must pass; the theater check must fail. That "must fail" is the counter-intuitive bit — design it so the red on the base branch reads as *good* (label it "fails on unfixed base — as required").

### 2.3 CLI (terminal) requirements

Follow clig.dev and the Heroku CLI style guide; the research behind these rules is in §5.

- **First 100 ms**: print something before any network call. `warden --help` and `warden doctor` must never touch the network or import heavy modules.
- **Onboarding**: `warden init` (detects repo, test runner, writes `warden.toml`), `warden doctor` (checks Python, git, `gh`, Nebius key, Sandboxes reachability, model reachability, Tavily key — one line per check, ✓/✗, and the fix for each ✗), `warden login` with env-var fallback. No wizard longer than five questions.
- **Progress**: streaming phase log, not a full-screen TUI. One line per phase with elapsed time and running cost: `Plan   ▸ 00:42  $0.31  reading issue #201, 2 citations`. Sub-steps indented. Full-screen TUIs lose scrollback and are hated when things go wrong (Turborepo's did). `--plain` disables animation; animation is off automatically when stdout is not a TTY.
- **Cost**: show `$` after every model call in the log and a total at the end; hard cap per issue with a clear message when hit ("cost cap $3.00 reached at Fix; stopping. Raise with --max-usd").
- **stdout vs stderr**: data to stdout, progress and warnings to stderr, so `warden run --json | jq` works. `--json` emits JSONL events and a final `result` line with `run_id`, `verdict`, `pr_url`, `cost_usd`.
- **Confirmation**: writing inside a sandbox needs no prompt. Opening a PR is the one risky action: prompt by default, `--yes` for CI. Never prompt for anything else.
- **Dry run**: `warden run --dry-run` stops after Plan and prints the plan.
- **Resume**: every run prints its `run_id` at start; `warden resume <run_id>` continues after an interruption.
- **Errors**: human sentence first, then the fix, then a `run_id`; tracebacks only with `--debug`. No `ERROR:` prefixes.
- **Colour**: respect `NO_COLOR` and `FORCE_COLOR`; red and yellow reserved for failure and warning; phase colours must survive on both light and dark terminals (test on Solarized light and default dark).
- **Config precedence**: flags > env > `warden.toml` in repo > `~/.config/warden/config.toml`.

### 2.4 Run console

A single page per run, scanned not read. Information architecture, top to bottom:

1. **Header**: repo, issue, verdict pill (PASS / PASS WITH CONCERNS / FAIL / PENDING-HUMAN), total cost, total time, model routing chips (Ultra · Super · Lightning).
2. **Phase timeline**: five horizontal bars scaled by duration, coloured by phase, cost printed on each. Click a bar to scroll the log to that phase (Devin's "click to jump", Braintrust's timeline).
3. **Branch tree**: base → candidate (green, "214 passed") and base → theater (red, "3 failed on unfixed base — as required"); with best-of-N, N candidates fan out and the chosen one is emphasised. Two modes like Langfuse's agent graph: *aggregated* (loops collapse into a node with a counter) and *expanded*.
4. **Evidence table**: identical to the PR body (§2.5). This is the single source of truth; the console shows it, the PR embeds it.
5. **Log**: collapsible per phase; tool calls shown as `name(args) → exit_code` with expandable stdout/stderr; model calls show tokens and $.
6. **Footer**: run_id, `warden resume` command, link to raw `events.jsonl`.

States to design: running (live), finished PASS, finished FAIL, PENDING-HUMAN, cost-cap stop, sandbox error. Empty state: "no runs yet — `warden run --demo`".

Must work on a phone (judges will open it from the Devpost page on mobile); the branch tree stacks vertically under 600 px.

### 2.5 PR body — the evidence table

Rows are claims; every row has the exact command, exit code, a link to the log line, and a status. Include what was **not** verified — an honest "untested" row beats an implied one (Devin marks untested explicitly; Copilot's unverifiable screenshots drew complaints).

```
| Check                       | Command                          | Result                      |
| New tests on unfixed base   | pytest tests/test_finalize.py    | FAIL 3/3 — as required  ↗   |
| Full suite on candidate     | pytest -q                        | 214 passed  ↗               |
| Race probe                  | 8 threads, Barrier               | 1 voucher (was 3)  ↗        |
| mypy / ruff / alembic / api | …                                | clean · clean · 1 head · no drift |
| Not verified                | frontend, e-invoice path         | —                           |
| Risk class                  | money                            | PENDING-HUMAN               |
| Cost                        | Ultra 2 · Super 11 · Lightning 37| $1.42                       |
```

Keep the narrative under 80 words above the table. Reviewers complain about "lots of words, not much substance"; the table is the substance.

### 2.6 README hero

First screen: name, one sentence, a VHS-generated GIF (reviewable `.tape` file, regenerated in CI; 1200×720, 20 pt), then three commands: `uvx warden doctor`, `uvx warden run --demo`, and the console URL. The `--demo` flag runs against a bundled tiny repo so a judge needs no GitHub token. Below the fold: "Runs on Nebius", "NVIDIA models", "Results".

### 2.7 Visual direction

Utility, not spectacle. Monospace for anything that is a command or a number; a humanist sans for labels. Five phase colours chosen to be distinguishable for deuteranopia and to hold on dark and light grounds; semantic red/amber/green reserved for verdicts and separate from the phase palette. No gradients, no illustration, no mascot. The branch tree is the one place the product may look distinctive — give it care.

## 3. RegWatch

### 3.1 Surfaces

1. Profile form (two minutes, once).
2. Feed (dated changes, grouped by instrument).
3. Month view ("what to do this month").
4. Change detail (summary, obligations with verbatim quotes, to-dos with citations, "why this applies to you").

### 3.2 The user's mental model

A shop owner thinks in obligations and dates, not in directives and articles. Every screen leads with *what I must do* and *by when*, and puts the legal name second. A to-do without a date is a worry, not a task — give each a due date or "already in force since <date>". Applicability must be explained in the owner's own profile terms: "applies because you ship to France and sell wooden furniture", never "Art. 3(1)(b)".

Trust design: every to-do carries a quote and a link. The quote is rendered as a real quotation (indented, with the source name) so the owner can see the product is not paraphrasing. "Unclear" is a first-class state with its own copy ("we couldn't tell from your profile — answer one question"), not a hidden bucket.

### 3.3 Screen requirements

- **Profile form**: four fields (establishment country, ship-to countries multi-select with "all EU" shortcut, product categories from a fixed list with plain names, platform). Inline explanation of why each field matters ("ship-to decides which consumer-rights rules apply"). No account, no email.
- **Feed**: list grouped by instrument; each item: date, instrument short name, applicability pill (Applies / Doesn't apply / Unclear), one-line summary, count of to-dos. Footer: "last crawl <time> · <n> sources · <credits> Tavily credits" — the integration is measured on-screen.
- **Month view**: to-dos sorted by due date; overdue first with a calm, factual label ("in force since 19 June 2026"); check-off persists; empty state when nothing is due ("Nothing due this month. Next: EUDR SME date, 30 June 2026.").
- **Change detail**: summary → "why this applies to you" (profile terms) → obligations as quotations with anchor links → to-dos → source metadata. A small "report a wrong call" link (mailto is fine) — it signals honesty and costs nothing.
- **Mobile first**: shop owners will read this on a phone between orders.

### 3.4 Visual direction

Calm and official without imitating any government or platform's branding. Generous line-height, a readable serif for quotations to set them apart from UI text, a neutral ground with a slight cool bias, one accent for actions and one for "applies". Dates in the owner's locale. No red except for genuinely overdue obligations with legal consequence, and even then paired with the plain next step.

### 3.5 Copy rules

Active voice, second person, no legalese in headings. A to-do is a verb phrase with an object ("Add the two-step withdrawal button to checkout"). Errors and empty states say what happened and what to do next. Never "AI-powered", never "we think"; say "applies" or "unclear" and show the quote.

## 4. Shared deliverables from the designer

1. Phase palette (5 colours) + semantic palette, validated for contrast on light and dark.
2. Warden console: desktop and 390 px layouts for the six states in §2.4.
3. Branch tree component: aggregated and expanded modes; best-of-N variant.
4. PR evidence table Markdown template (final wording).
5. Terminal output mock for `warden run` (a text file, not an image) and for `warden doctor`.
6. RegWatch: four screens at 390 px and 1024 px, with empty, unclear and overdue states.
7. README hero layouts for both repos.
8. VHS `.tape` script for the Warden GIF (with the designer's timing notes).

Order of work if time is short: 4, 1, 3, 2, 6, 5, 7, 8. The PR table and the branch tree are what judges will screenshot.

## 5. Research basis (short)

CLI rules come from clig.dev (print within 100 ms; disable colour/animation when not a TTY; `--json`, `--plain`, `--dry-run`; stderr for messaging; flags > env > project config > user config) and the Heroku CLI style guide (stdout is data, stderr is progress; every prompt bypassable by a flag; red/yellow only for errors/warnings). Colour conventions from no-color.org and force-color.org. Progress display: Codex `exec` streams progress to stderr and only the final message to stdout; Turborepo's full-screen TUI drew complaints about lost scrollback and unfindable failures, and users fell back to JSON summaries. Cost display: mini-swe-agent prints `(step N, $X.XX)` after each call with a default $3 cap and a prompt to raise it; Claude Code's JSON output carries `total_cost_usd`. Confirmation: Codex approval policies, Claude Code permission modes, OpenHands' confirm-by-default with `--always-approve`. Resume: `codex exec resume --last`, `claude --resume <id>`, `openhands --resume --last`. Evidence in PRs: Devin's per-test passed/failed/untested annotations and "proof" framing; Copilot coding agent's checklist body with session-log links and its unreliable screenshot complaints; CodeRabbit's collapsible walkthrough; Ellipsis attaching evidence links to every finding; reviewer complaints about summary theater. Trace UIs: Braintrust's spans/thread/timeline views, Langfuse's responsive timeline and aggregated/expanded agent graph, Devin's click-to-jump progress tab. README: lead with examples; VHS tapes over hand-recorded GIFs; `uvx` one-liners. Python CLI gotchas: `[project.scripts]` entry point matching the package name, `requires-python` and `uvx --python 3.12`, Typer+Rich startup ≈400 ms unless heavy imports are lazy.

Sources: https://clig.dev/ · https://devcenter.heroku.com/articles/cli-style-guide · https://no-color.org/ · https://force-color.org/ · https://learn.chatgpt.com/docs/non-interactive-mode · https://github.com/vercel/turborepo/discussions/8381 · https://mini-swe-agent.com/latest/usage/mini/ · https://code.claude.com/docs/en/headless · https://developers.openai.com/codex/agent-approvals-security · https://github.com/OpenHands/OpenHands-CLI/blob/main/README.md · https://cognition.com/blog/testing-development · https://github.blog/ai-and-ml/github-copilot/assigning-and-completing-issues-with-coding-agent-in-github-copilot/ · https://github.com/orgs/community/discussions/169913 · https://docs.coderabbit.ai/pr-reviews/walkthroughs · https://www.braintrust.dev/docs/observe/examine-traces · https://langfuse.com/docs/observability/features/agent-graphs · https://docs.devin.ai/work-with-devin/devin-session-tools · https://tenthirtyam.org/dispatches/2026/04/16/how-to-create-terminal-demos-as-code-with-vhs-by-charm/ · https://docs.astral.sh/uv/guides/tools/ · https://github.com/fastapi/typer/discussions/744
