# Spike A5 — first real plan (Fabric #196)

**Issue:** [Fabric-Inventory#196](https://github.com/moiz-lakkadkutta/Fabric-Inventory/issues/196),
"Dashboard sales/GST KPIs count DRAFT invoices". It was fixed in `43f3851` (PR #212). The
baseline is the human implementation plan posted as the issue's only comment.

**Setup:** `spikes/a5_first_plan.py`, 2026-10-04. Issue comments are excluded from the planner
input because the only comment *is* the human plan. Code context is read at `43f3851^`, the code
as it was before the fix (see "Leakage" below).

**Pipeline:**
1. Grep the repo for identifiers named in the issue (inside the `warden/fabric-base` sandbox).
2. Add the numbered source of the top hit file.
3. Lightning: doc queries plus follow-up identifiers.
4. Tavily `search(advanced, include_domains=docs)`.
5. Ultra produces the `Plan`.
6. Keep only citations whose URL came back from Tavily.

## Done-criterion

| Criterion | Result |
|---|---|
| `Plan` JSON produced | ✓ every run |
| ≥ 1 citation whose URL resolves | ✓ run 4: `docs.sqlalchemy.org/.../associationproxy.html` → HTTP 200 |
| Diff vs human plan written | ✓ this file |

## Runs

| Run | Context | Root cause found | Citations kept | Cost | Notes |
|---|---|---|---|---|---|
| 1 | HEAD (`8cfc493`, already fixed) | ✗ guessed `finalized_at IS NULL` | 1 (PG 7.1 PDF) | $0.015 | **leak**: cited `test_outstanding_ar_unchanged_by_draft` as "existing", a test the fix added |
| 2 | `43f3851^`, grep hits only | ✗ same guess | 0 | $0.010 | only saw KPI keys as dict entries, never the query functions |
| 3 | `43f3851^` + top-file source | ✓ **`_NON_CANCELLED_LIFECYCLES`** in `_sales_in_range` / `_gst_collected_in_range` | 0 (http/https mismatch dropped it; fixed) | $0.070 | first Ultra call hit the 8192 completion-token cap; the retry succeeded |
| 4 | same as 3 | ~ garbled: "functions lack a lifecycle_status filter" | 1 (resolves; weak page) | $0.026 | right files, 3 tests incl. cache invalidation |

The variance between runs 3 and 4 (same inputs, temperature 0.1) is the main finding. A single
plan is not reliable enough to hand to the implementer unchecked.

## Diff vs the human plan

| Aspect | Human plan | Warden (best: run 3 · latest: run 4) |
|---|---|---|
| File to fix | `dashboard_service.py` | ✓ both runs |
| Root cause | `_NON_CANCELLED_LIFECYCLES` includes DRAFT and CONFIRMED | ✓ run 3 exact · ✗ run 4 garbled |
| Fix | new `_BILLED_LIFECYCLES` = FINALIZED, POSTED, PARTIALLY_PAID, **PAID**, OVERDUE (mirrors `_GSTR1_LIFECYCLE`) | ✗ run 3 reuses `_OPEN_AR_LIFECYCLES`, which **drops PAID**: the exact trap the human plan warns about · ~ run 4 invents `_FINALIZED_PLUS_LIFECYCLES` without listing members |
| Test file | `test_int12_dashboard_kpis.py` and/or `test_dashboard_service.py` | ✓ both |
| Tests | 5: draft repro, finalize moves KPIs, PAID still counts, CANCELLED excluded, AR unchanged | run 3: 1 · run 4: 3 (repro, finalize, cache). **Neither has the PAID guard**, which is the test that catches run 3's wrong fix |
| CONFIRMED status | must be excluded | not mentioned |
| Cache | already invalidated on create; tests call `clear_cache()` | run 4 adds a cache test: plausible but unneeded |
| Risk class | "pure bugfix, may proceed" | `money` (+ `concurrency` in run 4, wrong). `money` triggers PENDING-HUMAN, which is conservative but defensible for a revenue KPI |
| Citations | none (internal code) | 1 SQLAlchemy page for `in_()`: decorative, but honest (from research) |
| Acceptance | issue repro numbers, finalize, AR unchanged, lint, no OpenAPI change | ✓ repro numbers, finalize, AR guard; ✗ no lint / OpenAPI item |

## What this changes

1. **Replays must read the pre-fix revision.** Planning against HEAD leaks the answer through test
   names and missing code. `gather_context(..., rev=...)` reads `git ls-tree` and `git grep <rev>`
   without a checkout. B2 and D3 must pass the fix commit's parent.
2. **Grep hits are not enough; the model needs source.** Run 2 → 3 is the only change that moved
   root-cause accuracy. The planner now gets the numbered source of the top two non-test hit files.
3. **The verifier has to catch semantic slips the plan makes.** Run 3's `_OPEN_AR_LIFECYCLES`
   would pass a draft-only test and silently drop PAID invoices from revenue. Two places can catch
   it: an acceptance item like "every status counted before still counts unless the issue says
   otherwise", and best-of-N plans ranked by Ultra (D4).
4. **Ultra needs a larger completion budget.** Reasoning consumed the default 8192 tokens in run 3
   before the JSON was finished. Set `max_tokens` explicitly for the PLANNER role.
5. **Lightning spends 2.6–4.6k completion tokens** (11–19 s) to emit ~100 tokens of JSON. It's
   cheap in dollars ($0.001) but it's the slowest step. Check whether Lightning accepts a lower
   reasoning budget.

Cost of the four runs: **$0.12** total. Runs are in `runs/a5-issue-196*/`, which is gitignored.
