## Warden evidence — #196 [P1] Dashboard sales/GST KPIs count DRAFT invoices (revenue & GST-collected overstated)

> Replay of a closed issue at `43f3851^`: re-solved from the issue text alone (comments hidden) on the code as it was before the human fix. Evidence, not for merging.

**Verdict: FAIL**

| Check | Result |
|---|---|
| New tests on unfixed base | FAIL — as required |
| Full suite on candidate | 1507 passed, 0 failed |
| Race probe | n/a |
| Gate | mypy clean · ruff FAIL · 1 alembic head · no openapi drift |
| Not verified | frontend type codegen drift (pnpm gen:types; no node in fabric-base) |
| Risk class | money, schema → **PENDING-HUMAN** |
| Cost | $0.26 · Nemotron-3_5-Lightning 1 · Nemotron-3-Ultra-550b-a55b 1 · nemotron-3-super-120b-a12b 23 |

**Plan citations**
- [SQLAlchemy ColumnOperators.in_() produces a column IN clause; we use it to filter lifecycle_status against a tuple of allowed statuses.](http://docs.sqlalchemy.org/en/latest/core/operators.html)
