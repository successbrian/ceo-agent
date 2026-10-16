# Contributing to ceo-agent

## What this repo is
An event-driven CEO decision engine: it wakes on PostgreSQL `LISTEN`/`NOTIFY`,
makes one decision per wake, writes it back, and sleeps. No polling loop.
See `README.md` for the full picture.

## Ground rules
- **One decision-flow change per PR.** Small PRs get reviewed fast; big ones sit.
- **Python 3.11+, stdlib + psycopg2 only.** No new dependencies without discussion — open an issue first.
- **Decisions are the product.** If your change alters what the engine decides or how it decides, say so plainly in the PR description.
- **The engine never polls and never decides without intel.** Keep it that way.
- **No CLAUDE.md files in this repo** (project convention).

## Workflow
1. Fork and branch from `main`.
2. `cp config.example.env .env` and point it at a **scratch** Postgres — never production.
3. Add or update tests in `tests/` (see below).
4. `python3 -m pytest` must pass. CI runs the same suite, plus the schema idempotency test against a Postgres service.
5. Open a PR with the template filled in. Simple PRs may be merged without ceremony.

## Tests
- `tests/test_ceo_agent.py` — pure unit tests (prompt building, env parsing). Run anywhere; no services needed.
- `tests/test_schema.py` — applies `schema.sql` twice against Postgres and verifies the table and trigger. Needs `TEST_PG_DSN` (CI provides a Postgres service); skips cleanly without one.

## Style
Match the existing file: section banners, docstrings on every function, stdlib-first.
