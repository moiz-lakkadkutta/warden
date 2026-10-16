# Warden demo video: script and shot list (target 2:45, hard cap 3:00)

Rules for this video: every number on screen comes from a committed artefact (README, `docs/status.md`,
`evals/`), every shot names the exact command or URL to capture, and nothing is staged as live that
was not live. Where a shot shows a recording (the `--demo` run, a past run in the console), the
narration says so.

Capture setup: terminal 1400×720 or larger, font ≥ 16 pt, dark theme (the hero GIF uses VHS
"GitHub Dark"); browser at 1280×800, plus one phone-width shot (390 px). Record the voice-over
separately and lay it over the cuts; the narration below is ~390 words, about 2:40 at a calm pace.

Sources for every figure: README "Results" (E1 table, E1 + verifier fixes table, Fabric campaign
table), `docs/status.md` §C5 (Hunter), §1 B6-live (PR #221), `evals/fabric/campaign.md` (PR #226).

---

## Shot 1 · 0:00–0:15 · The problem

**Capture:** the closed Fabric issue that #221 re-solved, title visible:
`https://github.com/moiz-lakkadkutta/Fabric-Inventory/issues/196`

**Narration:**
> A solo founder ships an ERP. One bug in a dashboard query counts draft invoices as revenue, and
> the GST figure is wrong. Coding agents can write a fix for this in minutes. The hard part is
> knowing whether to trust it. Warden is an agent whose second half exists to doubt the first.

## Shot 2 · 0:15–0:35 · Doctor and the offline demo

**Capture:** the hero GIF, or record it live from an empty directory with no `.env`:
```bash
uv tool install git+https://github.com/moiz-lakkadkutta/warden
warden doctor        # ✗ on the four keys, with the fix for each; no network call
warden run --demo    # hunt → plan → fix → verify → PR on the fake sandbox
```
(`vhs docs/demo/hero.tape` reproduces this exactly.)

**Narration:**
> `warden doctor` checks tools and keys without touching the network and tells you the fix for each
> missing one. `warden run --demo` needs no keys at all: it replays a recorded run on a fake
> sandbox, so you see every phase, the running cost, and the evidence table in a few seconds.

On screen, pause on: `theater: FAIL on unfixed base — as required` and the evidence table.

## Shot 3 · 0:35–1:00 · The Hunter finds the bug first

**Capture A (always):** issue #227, then #225, scrolled to the curl repro and the "historical
replay" banner:
`https://github.com/moiz-lakkadkutta/Fabric-Inventory/issues/227` ·
`https://github.com/moiz-lakkadkutta/Fabric-Inventory/issues/225`

**Capture B (optional, live, needs `.env` + Sandboxes; $0 model spend, under a minute):**
```bash
warden hunt --axis idor --at 'd9c37cb^' --no-file
```
The run on record is `20261015-163638-ce14` (`docs/status.md` §C5 replay table).

**Narration:**
> Warden can also find the bug. The Hunter boots the app in a sandbox fork, signs up two tenants,
> reads the API schema and tries to write into the other tenant's firm. On Fabric's current code it
> finds nothing, because the hole was fixed. Replayed at the commit before that fix, it confirmed a
> cross-firm write on six endpoints, twice each, and filed these issues. That probing is
> deterministic: no model spend.

## Shot 4 · 1:00–1:30 · Fork tree and the theater check

**Capture:** the live console page for the run behind PR #221, scrolled from the header through
the branch tree:
`https://port8080-cs9x5ndsvxz54x7.tunnel.applications.eu-west2.nebius.cloud/20261010-123442-279b/`
Then cut to `docs/spike-A7.png` (the repro failing on base with `10498.95 == 0`).

**Narration:**
> Every issue gets a fork of a base sandbox checkpoint. The implementer, Nemotron 3 Super, writes a
> failing test first, then the fix. Then the verifier forks the *unfixed* base and runs only the new
> tests. They must fail there. If they pass without the fix, the test is theater and doesn't count.
> Here it fails on the unfixed base, as required, and the full suite, 1,508 tests, passes on the
> candidate.

On screen, hold on the red card "failed on unfixed base — as required" for two seconds.

## Shot 5 · 1:30–1:55 · The evidence table on a real draft PR

**Capture:** PR #221, the body's evidence table, the `PENDING-HUMAN` label, then the Checks tab
showing 7/7 green:
`https://github.com/moiz-lakkadkutta/Fabric-Inventory/pull/221` ·
`https://github.com/moiz-lakkadkutta/Fabric-Inventory/pull/221/checks`
Optional cut: the campaign's PR `https://github.com/moiz-lakkadkutta/Fabric-Inventory/pull/226`.

**Narration:**
> The PR body is the evidence: each claim, the check that backs it, and what was *not* verified.
> This was a closed issue, re-solved unattended from the issue text alone, on the code before the
> human fix: 24 minutes, 17 cents. Fabric's own CI ran seven jobs, all green. It touches money, so
> it's labelled pending-human. Warden opens drafts; it never merges.

## Shot 6 · 1:55–2:05 · The live console on Nebius

**Capture:** the console index on desktop, then the same URL at 390 px (Chrome DevTools device
toolbar, iPhone 12 Pro):
`https://port8080-cs9x5ndsvxz54x7.tunnel.applications.eu-west2.nebius.cloud/`

**Narration:**
> Every model call and sandbox step is logged with tokens, cost and time. The console renders that
> log, and it is live right now on a Nebius Serverless endpoint. It works on a phone.

## Shot 7 · 2:05–2:35 · Results, honestly

**Capture:** README, scrolled to "Results": the E1 table, then the "E1 + verifier fixes" table,
then "Fabric campaign":
`https://github.com/moiz-lakkadkutta/warden#results`

**Narration:**
> On fifty SWE-rebench tasks, graded by hidden tests Warden never sees, it resolved 11 of 49, at 54
> cents a task. Its own PASS verdict was right only 29% of the time, so we fixed the verifier: on
> the same 15 tasks, PASS precision went from 29 to 62%. The misses left are issues that never state
> what the hidden test demands. And on Fabric's nine-issue campaign, one passed. That's in the
> README too. A FAIL never opens a PR.

On screen: highlight `11 / 49`, `$0.54 / task`, `4 of 14 PASS (29%)` → `5 of 8 PASS (62%)`, and the
campaign row `1 … 8 … $7.29`.

## Shot 8 · 2:35–2:50 · Model routing, close

**Capture:** `warden doctor`'s "Model routing" block (or the README "NVIDIA models" table), then
the README hero.

**Narration:**
> Nemotron 3 Ultra plans and judges. Nemotron 3 Super writes the code. Nemotron 3.5 Lightning
> handles the small structured calls. All on Nebius Token Factory and Sandboxes. Warden: an agent
> that has to prove its fix before it asks for your review.

---

## Checklist before upload

- [ ] Voice-over recorded and synced (by hand; nothing here generates audio).
- [ ] Console URL still answers (`curl -s <url>/healthz` → `{"status":"ok",…}`) on recording day;
      the endpoint costs ~$0.07/h and may have been stopped with `ops/deploy-console.sh --down`.
- [ ] If the README results block was regenerated after this script was written, re-check the
      four numbers in shot 7 against it.
- [ ] Runtime under 3:00; captions on (Devpost viewers often watch muted).
