You are planning a fix in a FastAPI + SQLAlchemy 2 + Postgres codebase with row-level security and a
double-entry ledger. Money is stored as NUMERIC and passed as strings. Every mutation carries an
Idempotency-Key. Previously applied Alembic migrations are never edited.
