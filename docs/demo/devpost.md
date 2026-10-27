# Devpost submission draft: Warden

Every number below is from a committed artefact: README "Results", `docs/status.md`,
`evals/fabric/campaign.md`, `FEEDBACK.md`. If the README results block is regenerated before
submission, re-check the numbers marked †.

- **Track:** Coding
- **Repo:** https://github.com/moiz-lakkadkutta/warden (MIT)
- **Demo URL (live console, Nebius Serverless AI endpoint):**
  https://port8080-h79fp774t88b2wc.tunnel.applications.eu-west2.nebius.cloud/
- **Video:** _(link after upload; script in `docs/demo/script.md`)_
- **Builders & Brews city:** Berlin (Sep 29). _Confirm attendance before submitting; the city
  prize needs it disclosed on the form._
- **New work:** Warden was written for this hackathon, starting 14 Sep 2026. Before that the
  hunt → plan → fix → verify idea existed only as a personal Claude Code skill (prompts, no
  shippable code); every line in this repo is new.
- **Try it without credentials:**
  `uv tool install git+https://github.com/moiz-lakkadkutta/warden && warden run --demo`

---

## Tagline

An autonomous coding agent that has to prove its fix before it asks for your review.

## Inspiration

I run a small ERP (FastAPI + Postgres, double-entry ledger, Indian GST) on my own. A wrong number in
a dashboard query or a missing tenant check reaches someone's books. Coding agents now write
plausible fixes in minutes, so the bottleneck has moved from writing the fix to trusting it: a
green test the agent wrote proves nothing if that test would have passed without the fix. I wanted
an agent whose second half exists to doubt the first, and that shows its evidence instead of
narrating confidence.

## What it does

Warden runs **Hunt → Plan → Fix → Verify → PR** on one issue:

- **Hunt** (one axis so far, IDOR / cross-tenant): boots the app in a sandbox fork, signs up two
  tenants, enumerates id-taking endpoints from `/openapi.json`, probes cross-org reads and
  cross-firm writes, and files an issue only for findings that reproduce twice. It uses no model
  calls.
- **Plan**: Nemotron 3 Ultra reads the issue and the code in the sandbox, cites docs found with
  Tavily, names the files and tests, and classifies risk (money / tax / schema / security).
- **Fix**: Nemotron 3 Super, in a tool-calling loop on its own **fork** of a base sandbox checkpoint,
  writes a failing test first, then the fix.
- **Verify**: the implementer's word is never evidence. A candidate is FAIL unless:
  - the new tests **fail on a fork of the unfixed base** (the theater check);
  - the suite and configured lint / type / migration / OpenAPI checks pass;
  - the base's related tests still pass on the candidate;
  - an Ultra spec review finds every behaviour the issue states implemented, quoting the issue
    verbatim.
  Best-of-N runs up to N candidates on separate forks and picks one by a fixed rule.
- **PR**: a PASS opens a **draft** PR whose body is the evidence table: each claim, the check behind
  it, and what was *not* verified. Risky changes are labelled `PENDING-HUMAN`. A FAIL opens nothing.
  Warden never merges.

Every model call and sandbox step goes to `events.jsonl` with tokens, cost and time. `warden
console` renders it as a page with the phase timeline, the fork tree, and the evidence table, and
that console is live on Nebius. It also runs as a GitHub Action: label an issue `warden` and it
opens a draft PR or comments why it didn't.

## How we built it

- **Nebius Token Factory** (OpenAI-compatible API) serves every model call: JSON-schema structured
  output for every plan, verdict and finding (pydantic schemas), and native function calling for
  the implementer loop.
- **NVIDIA Nemotron, routed by role** (set by env, swappable without code changes):
  - **Nemotron 3 Ultra** (`nvidia/Nemotron-3-Ultra-550b-a55b`) plans and writes the spec-review
    verdict, where reasoning and adversarial judgement matter;
  - **Nemotron 3 Super** (`nvidia/nemotron-3-super-120b-a12b`) is the implementer: 19 of 19 tool
    calls well-formed on the first Fabric fix, at about a third of Ultra's cost;
  - **Nemotron 3.5 Lightning** (`nvidia/Nemotron-3_5-Lightning`) handles small structured tasks
    such as research queries and parsing.
- **Nebius Token Factory Sandboxes**: every run is an immutable checkpoint, and two runs from one
  checkpoint are a fork (about 1 s each in our measurements). The fork tree *is* the verifier: the
  candidate, the theater check on the unfixed base, the regression check, and N best-of-N
  candidates are all siblings of the same base, and the gate runs on disposable forks, so the
  candidate is never modified. The patch leaves the sandbox via `git format-patch` and is pushed
  from the host, so no token ever enters a sandbox.
- **Preloaded SWE-rebench images** on Sandboxes: `warden eval` plans, fixes and verifies on the task
  image, then grades with the dataset's hidden tests, which the agent never sees.
- **Nebius Serverless AI endpoint + Container Registry**: the run console is a read-only FastAPI
  app shipped as a non-root image to Nebius Container Registry and run on a `cpu-d3` endpoint
  (`ops/deploy-console.sh`).
- Python 3.12, `uv`, typer-slim, pydantic, a plain state machine (no agent framework), `mypy
  --strict`, offline tests against a fake sandbox, and CI that replays a recorded run.

## Challenges we ran into

- **A PASS on under-specified issues.** On SWE-rebench our verifier's first PASS precision was 4 of
  14 (29%)†. Of the 10 false PASSes, 8 needed an API, message or name the issue never states; a
  verifier that reads only the issue cannot catch those. The 2 real misses (a repro that used a
  different exception class or resource kind than the issue's own example) we did fix.
- **Nemotron 3 Super on large files.** On Fabric's 900-line test files Super re-read files or
  re-sent them whole past the completion limit, and in the first 15 campaign attempts it called
  `finish` in only 2. We changed the tools (200-line read pages, no whole-file rewrite over 300
  lines, an edit mode that withholds read tools when it loops, auto-finish of a checked green
  candidate). Loops went down, but PASS on those issues did not go up.
- **Sandboxes beta transport.** 7 of 49 E1 tasks first ended in a timed-out or dropped operation
  poll, and `run()` stdout is cut silently at 65,535 bytes, which once made a correct patch grade
  as unresolved. We added retries and chunked reads, re-ran the affected tasks, and logged both in
  `FEEDBACK.md` with numbers.
- **Honest hunting.** On current Fabric the Hunter finds no cross-tenant hole, because the known one
  was fixed. Rather than inflate a non-finding, we replayed the commit before the fix (`--at
  d9c37cb^`): there it confirmed the cross-firm write on 6 endpoints and filed
  [#225](https://github.com/moiz-lakkadkutta/Fabric-Inventory/issues/225) and
  [#227](https://github.com/moiz-lakkadkutta/Fabric-Inventory/issues/227). The fix step then
  **failed** on both. The verifier rejected every candidate and no PR was opened, which is the
  safety property working, but it is not a fix.

## Accomplishments that we're proud of

- **A real draft PR from issue text alone:**
  [Fabric-Inventory#221](https://github.com/moiz-lakkadkutta/Fabric-Inventory/pull/221). A closed
  money bug was re-solved unattended, with comments hidden, on the code before the human fix: 24
  min, $0.17, and the target repo's own CI went 7/7 green. Its repro test fails on the unfixed base
  (`10498.95 == 0`) and passes on the candidate.
- **A measured number, with its error bars:** 18 / 49 SWE-rebench tasks resolved (37%, 95% CI
  25–51%) at $0.49 per task, $24.16 in total†, up from 11 / 49 (22%) on the same tasks a week
  earlier, with the verifier's false-PASS rate printed next to it rather than hidden.
- **Verifier PASS precision 29% → 48%** on those 49 tasks (4 of 14 → 13 of 27 PASS accepted by
  the hidden tests)† after the spec review learned to check the issue's own artefact, a guard-only
  test stopped counting as a repro, and a valid repro with a red check goes back once (62% on the
  15 tasks first marked PASS).
- **The theater check works:** it rejected 14 and then 10 new tests† that already passed without
  the fix, and on Fabric it rejected eight tests that failed for the wrong reason.
- **Nothing unsafe shipped:** no FAIL produced a branch or a PR, and every money or tax issue was
  labelled `PENDING-HUMAN`.

## What we learned

- **Fork-per-step is the right primitive for verification.** "Run the new tests on the unfixed
  code" is one line when sandboxes fork in about a second. Without forks it is a rebuild.
- **Precision is the metric that matters for trust,** and it has a ceiling set by the issue text.
  Once a hidden test demands something the issue never says, no issue-reading verifier can catch
  it. The honest output is "PASS is evidence, not proof" and a draft PR.
- **Model sizing by role pays off.** Ultra's judgement is worth its price on one plan and one
  verdict per issue. Super's well-formed tool calls are worth more in a 60-turn loop than raw
  strength. Lightning is cheap, but it reasons by default, which makes it slower than expected
  for a router.
- **A benchmark win is not a codebase win.** On a real ERP the campaign passed **1 of 9** issues
  ($7.70, 29 attempts). That is the number that tells us what to build next.

## What's next for Warden

- Make the implementer reliable on large real-world files (Fabric campaign: 1 of 9 today).
- More Hunter axes: race (concurrent finalize → duplicate vouchers), money, malformed input and
  flow are designed but not built (only the IDOR axis runs today); frontend is not covered.
- A concurrency check in verify and a migration downgrade round-trip in the gate.
- Spend the verifier's doubt where it matters: when the spec review sees an under-specified
  behaviour, say so in the PR instead of passing it.
- Run on more repos than Fabric and the SWE-rebench sample, and publish each run's console page.

## Built with

nebius-token-factory · nebius-sandboxes · nebius-serverless-ai-endpoints · nebius-container-registry ·
nvidia-nemotron-3-ultra · nvidia-nemotron-3-super · nvidia-nemotron-3.5-lightning · tavily · python ·
fastapi · pydantic · uv · github-actions

## Feedback field

Paste the dated entries from `FEEDBACK.md` (Sandboxes, Token Factory inference, Nemotron, Serverless).
