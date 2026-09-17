-- CEO Agent — intelligence queue + event-driven wakeup trigger.
-- Run this once on the PostgreSQL database that backs your agent fleet.
-- Idempotent: safe to re-run.

-- The durable queue where producers INSERT intelligence and the CEO agent
-- writes its decisions back. `acknowledged` marks items the CEO has consumed.
CREATE TABLE IF NOT EXISTS intel_buffer (
    id           BIGSERIAL PRIMARY KEY,
    source_agent TEXT,
    target_agent TEXT,
    content      TEXT,
    category     TEXT,
    title        TEXT,
    priority     INTEGER      DEFAULT 50,
    acknowledged BOOLEAN      DEFAULT false,
    created_at   TIMESTAMPTZ  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_intel_buffer_target_unacked
    ON intel_buffer (target_agent, acknowledged)
    WHERE acknowledged = false;

-- Wake the CEO agent whenever new intelligence lands. The engine LISTENs on
-- this channel and only runs a decision pass in response to a notification.
CREATE OR REPLACE FUNCTION notify_ceo_wakeup() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('ceo_wakeup', NEW.id::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ceo_wakeup_trigger ON intel_buffer;
CREATE TRIGGER ceo_wakeup_trigger
    AFTER INSERT ON intel_buffer
    FOR EACH ROW
    EXECUTE FUNCTION notify_ceo_wakeup();
