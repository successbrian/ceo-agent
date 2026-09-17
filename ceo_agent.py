#!/usr/bin/env python3
"""
CEO Agent — event-driven decision engine.

Wakes only when intelligence arrives (PostgreSQL LISTEN/NOTIFY), reads the
pending intel, makes ONE decision, writes it back, and returns to sleep.
There is no polling loop: if nothing arrives, the engine does nothing but a
lightweight heartbeat ping.

Why event-driven: a CEO makes decisions when there is something to decide,
not on a fixed timer. A 60-second poll loop wakes ~1,440 times/day to find
nothing; this engine wakes exactly once per inbound message.

Data flow:
  producers ──INSERT──▶ intel_buffer ──trigger──▶ pg_notify('ceo_wakeup')
                                                      │
                                                      ▼
  this engine (LISTEN) ──reads pending──▶ ONE LLM decision ──▶ intel_buffer
                                                                   │
                                          downstream consumers (SBOS, fleet)

All configuration is via environment variables. See config.example.env.
The LLM call uses the OpenAI-compatible /chat/completions format, so it works
with Ollama (http://host:11434/v1), DeepSeek, OpenAI, KiloCode, vLLM, etc.
"""

import json
import os
import select
import signal
import sys
import time
import urllib.request
import urllib.error

import psycopg2
import psycopg2.extensions

# ────────────────────────────────────────────────────────────────────────────
# Configuration (environment)
# ────────────────────────────────────────────────────────────────────────────

def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default

# PostgreSQL (where intel_buffer lives)
PG_DSN = os.getenv("PG_DSN", "postgresql://localhost:5432/ecosystem_central")
PG_TABLE = os.getenv("PG_TABLE", "intel_buffer")

# LLM (OpenAI-compatible)
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b")
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")  # "ollama" is a common placeholder
LLM_MAX_TOKENS = _int("LLM_MAX_TOKENS", 2048)
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.4"))
LLM_TIMEOUT = _int("LLM_TIMEOUT", 300)

# Identity / routing
AGENT_ID = os.getenv("AGENT_ID", "ceo")          # source_agent on its own output
LISTEN_CHANNEL = os.getenv("LISTEN_CHANNEL", "ceo_wakeup")
INTEL_TARGETS = os.getenv("INTEL_TARGETS", "ceo,all")  # comma-separated targets it reads
DECISION_TARGET = os.getenv("DECISION_TARGET", "fleet")  # who consumes its decisions

# Heartbeat (lightweight; only a ping, never a model query)
HEARTBEAT_SEC = _int("HEARTBEAT_SEC", 300)      # 0 disables the ping
A2A_HUB = os.getenv("A2A_HUB", "")              # optional A2A hub URL for the ping
A2A_AGENT_ID = os.getenv("A2A_AGENT_ID", AGENT_ID)

# Optional AnythingLLM memory enrichment (off by default; the self-referential
# "What does <agent> know about..." probe was REMOVED — it triggered model
# refusals. Use a neutral retrieval query if you enable this.)
ANYTHING_URL = os.getenv("ANYTHING_URL", "")
ANYTHING_KEY = os.getenv("ANYTHING_KEY", "")
ANYTHING_WORKSPACE = os.getenv("ANYTHING_WORKSPACE", "")

# Batch limits
INTEL_BATCH = _int("INTEL_BATCH", 20)

TARGETS = [t.strip() for t in INTEL_TARGETS.split(",") if t.strip()]

LOG_PREFIX = f"[{AGENT_ID}]"

SYSTEM_PROMPT = """You are the CEO intelligence of an autonomous business system.

Your job:
1. Read incoming intelligence from your agents and systems.
2. Make clear decisions — approve, reject, dispatch, investigate, escalate.
3. Issue directives that downstream workers can act on.
4. Flag anything that needs the human operator's attention.

Rules:
- Decide based ONLY on the facts you are given. Do not invent status.
- If there is nothing to decide, say so in one line — do not pad.
- Be decisive and practical. Short sentences. No fluff.

Respond with EXACTLY these sections:

### DECISIONS
- (decisions you made, approvals/rejections)

### DIRECTIVES
- (instructions for your workers to execute)

### STATUS
- (anything to flag, in one line)
"""


def log(msg):
    print(f"{LOG_PREFIX} {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# ────────────────────────────────────────────────────────────────────────────
# PostgreSQL
# ────────────────────────────────────────────────────────────────────────────

def pg_connect():
    return psycopg2.connect(PG_DSN, connect_timeout=10)


def read_pending_intel(conn):
    """Read unacknowledged intel addressed to this agent (not its own output)."""
    cur = conn.cursor()
    placeholders = ",".join(["%s"] * len(TARGETS))
    cur.execute(
        f"""
        SELECT id, source_agent, content, priority, created_at, category
        FROM {PG_TABLE}
        WHERE target_agent IN ({placeholders})
          AND acknowledged = false
          AND (source_agent IS DISTINCT FROM %s)
        ORDER BY priority DESC, created_at ASC
        LIMIT %s
        """,
        (*TARGETS, AGENT_ID, INTEL_BATCH),
    )
    rows = cur.fetchall()
    cur.close()
    return rows


def acknowledge(conn, ids):
    if not ids:
        return
    cur = conn.cursor()
    cur.execute(
        f"UPDATE {PG_TABLE} SET acknowledged = true WHERE id = ANY(%s)",
        (list(ids),),
    )
    conn.commit()
    cur.close()


def write_decision(conn, text, category="decision", priority=60):
    cur = conn.cursor()
    cur.execute(
        f"""
        INSERT INTO {PG_TABLE}
            (source_agent, target_agent, content, category, title, priority, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, now())
        RETURNING id
        """,
        (AGENT_ID, DECISION_TARGET, text, category, "CEO Decision", priority),
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    return new_id


# ────────────────────────────────────────────────────────────────────────────
# LLM (OpenAI-compatible)
# ────────────────────────────────────────────────────────────────────────────

def query_llm(prompt, system_prompt=SYSTEM_PROMPT):
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": LLM_MAX_TOKENS,
        "temperature": LLM_TEMPERATURE,
        "stream": False,
    }
    req = urllib.request.Request(
        f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            result = json.loads(resp.read())
            return result["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        log(f"LLM HTTP error {e.code}: {e.read().decode()[:200]}")
        return None
    except Exception as e:
        log(f"LLM error: {e}")
        return None


def build_prompt(rows):
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    parts = [f"Incoming intelligence at {now}:"]
    for row in rows:
        intel_id, source, content, priority, created_at, category = row
        parts.append(f"- [from {source}] (priority {priority}, {category}): {content[:500]}")
    if not rows:
        parts.append("- (no pending intelligence)")
    parts.append("\nMake your decision and reply in the required format.")
    return "\n".join(parts)


# ────────────────────────────────────────────────────────────────────────────
# Heartbeat (optional lightweight ping, no model)
# ────────────────────────────────────────────────────────────────────────────

def a2a_ping():
    if not A2A_HUB:
        return
    payload = {
        "jsonrpc": "2.0",
        "method": "agents/heartbeat",
        "params": {"agentId": A2A_AGENT_ID, "ts": int(time.time())},
        "id": int(time.time() * 1000),
    }
    req = urllib.request.Request(
        A2A_HUB,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # heartbeat is best-effort


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def handle_wakeup(conn):
    rows = read_pending_intel(conn)
    if not rows:
        return 0
    log(f"woke: {len(rows)} intel item(s) pending")
    decision = query_llm(build_prompt(rows))
    if not decision:
        log("LLM returned no decision — leaving intel unacknowledged for retry")
        return 0
    new_id = write_decision(conn, decision)
    acknowledge(conn, [r[0] for r in rows])
    log(f"decided (intel_buffer id {new_id}): {decision[:120]}")
    return len(rows)


def main():
    log("starting event-driven CEO agent")
    log(f"PG={PG_DSN}  LLM={LLM_MODEL} ({LLM_BASE_URL})  channel={LISTEN_CHANNEL}")

    conn = pg_connect()
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(f"LISTEN {LISTEN_CHANNEL}")
    cur.close()
    log(f"LISTENing on '{LISTEN_CHANNEL}'")

    # One optional warmup call to confirm the model is reachable.
    warm = query_llm("Reply with exactly: OK")
    log(f"LLM warmup: {'OK' if warm else 'no response (will retry on wake)'}")

    running = True

    def _stop(signum, frame):
        nonlocal running
        log("shutdown signal received")
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    last_heartbeat = 0.0

    while running:
        # Block for up to HEARTBEAT_SEC (or 60s if heartbeat disabled) waiting
        # for a notification. A timeout is NOT a reason to query the model.
        timeout = HEARTBEAT_SEC if HEARTBEAT_SEC > 0 else 60
        try:
            ready, _, _ = select.select([conn], [], [], timeout)
        except InterruptedError:
            continue
        except Exception as e:
            log(f"select error: {e}")
            time.sleep(5)
            continue

        if ready:
            conn.poll()
            while conn.notifies:
                notify = conn.notifies.pop(0)
                try:
                    handle_wakeup(conn)
                except Exception as e:
                    log(f"handle_wakeup error: {e}")
                    time.sleep(2)
        else:
            # Timeout: lightweight heartbeat only.
            now = time.time()
            if HEARTBEAT_SEC > 0 and (now - last_heartbeat) >= HEARTBEAT_SEC:
                a2a_ping()
                last_heartbeat = now

    conn.close()
    log("stopped")


if __name__ == "__main__":
    main()
