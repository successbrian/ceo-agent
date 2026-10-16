"""schema.sql idempotency — needs Postgres; skips cleanly without TEST_PG_DSN."""
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.getenv("TEST_PG_DSN")


def _load_schema():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "schema.sql")) as f:
        return f.read()


@pytest.mark.skipif(not DSN, reason="TEST_PG_DSN not set")
def test_schema_is_idempotent():
    schema = _load_schema()
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    for _ in range(2):  # apply twice — must not error
        cur.execute(schema)
    cur.execute("SELECT to_regclass('public.intel_buffer')")
    assert cur.fetchone()[0] == "intel_buffer"
    cur.execute("SELECT tgname FROM pg_trigger WHERE tgname = 'ceo_wakeup_trigger'")
    assert cur.fetchone()[0] == "ceo_wakeup_trigger"
    cur.close()
    conn.close()
