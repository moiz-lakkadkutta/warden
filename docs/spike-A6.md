# Spike A6 — first fix (Fabric #196)

**Setup:** `spikes/a6_first_fix.py`, 2026-10-06.
1. Fork `warden/fabric-base:latest` and check out `43f3851^` (#196 was fixed in `43f3851`).
2. Plan with Ultra (issue comments excluded).
3. Implement with Super, using the five tools plus `finish`, running at `/src` wrapped in
   `with-services sh -c`, with up to 40 tool turns.
4. Rerun the new tests independently in the candidate.

## Done-criterion

| Criterion | Result |
|---|---|
| Candidate box exists | ✓ `ca4365f8-3109-43b6-a71c-18367d027182` |
| `pytest <new tests>` passes in it | ✓ **3 passed** in 0.54 s, and the existing `test_dashboard_service.py` stays green |
| Cost < $3 | ✓ **$0.154** total (plan $0.029 + implement $0.125), 187 s wall clock |

The three tests are recorded as node ids in an existing file:
- `backend/tests/test_dashboard_service.py::test_sales_gst_kpis_exclude_draft_invoices`
- `...::test_sales_gst_kpis_include_finalized_invoices`
- `...::test_outstanding_overdue_ar_still_exclude_drafts`

## The fix vs the human one

| | Human (`43f3851`) | Warden (run 4) |
|---|---|---|
| Status set | FINALIZED, POSTED, PARTIALLY_PAID, PAID, OVERDUE | **identical** (`_FINALIZED_PLUS_LIFECYCLES`) |
| Old constant | removed | removed |
| Docstrings | updated | updated |
| Tests | draft repro, finalize moves KPIs, PAID still counts, CANCELLED excluded, AR unchanged | draft repro, finalized counts (with GST), AR unchanged. **No PAID or CANCELLED guard** |
| Test hygiene | `clear_cache()` | `clear_cache()`, and sets `gst_amount` explicitly |
| Nits | — | drops the file's trailing newline |

## How it got there: four runs, three Warden bugs

| Run | Outcome | Cause | Fix |
|---|---|---|---|
| 1 | 25 turns re-reading one file, no write | `read_file` cut the 16 KB file to 8 KB head+tail, so the functions to fix were never visible. `grep A\|B` used basic regex | `read_file` pages whole lines with a `start_line` hint; `grep -E` (`f700939`) |
| 2 | 25 turns exploring the filesystem | Super ran `cd /app && pytest backend/...` out of Docker habit (`/app` is `backend/` in the image) | system message names the workdir and says never `cd` (`a33db0a`) |
| 3 | real fix + test, out of turns while debugging | the base test file was read from `/app`, so it looked new; `/tmp/debug_test.py` counted as a test. The model dropped POSTED/OVERDUE, compared GST to `invoice_amount` and ignored the 60 s KPI cache | read base files in the workdir; scratch paths aren't tests; two semantic rules in the implementer prompt (`d526bbf`) |
| 4 | ✓ done in 18 turns | — | — |

Runs 1–3 cost $0.13–0.30 each. Every failure was in Warden's tools or instructions, not
the model's ability to write the fix.

## Numbers (run 4)

- Super: 19 calls, 368.5k prompt tokens (max 34k in one call), 16.5k completion tokens, 87 s
  total model time. The Lightning router was never needed: every Super turn was a tool call.
- TDD order: the test was written first. But the model did not run it before implementing
  (step 2 of the protocol), so on its own A6 doesn't show the test fails on the pre-fix code.
  That's the theater check's job (A7).

## Follow-ups

1. **A7 theater check** must copy the candidate's version of the test files onto the pre-fix
   base and run the node ids. Here they don't exist on base (`no tests ran`, exit 4), which
   must not count as "failed on base".
2. Make the "PAID still counts" guard required: the planner's `acceptance` should name it, and
   the verifier should check that each acceptance item maps to a test.
3. Default `WARDEN_MAX_TOOL_TURNS` 25 → 40. Run 4 needed 18, but a run that has to debug a
   test needs headroom.
4. Prompt growth: 34k prompt tokens by turn 19, because every file read stays in history.
   Consider dropping earlier pages of a file once it has been rewritten.
