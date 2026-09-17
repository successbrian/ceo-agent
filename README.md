# CEO Agent

An **event-driven decision engine** for autonomous business systems. A single
Python service that acts as the strategic decision-maker for a fleet of AI
workers — it wakes only when there's something to decide, makes a decision, and
goes back to sleep.

```
producers ──INSERT──▶ intel_buffer ──trigger──▶ pg_notify('ceo_wakeup')
                                                     │
                                                     ▼
  ceo_agent.py (LISTEN) ──reads pending──▶ ONE LLM decision ──▶ intel_buffer
                                                                    │
                                          downstream workers (SBOS, fleet, CRM)
```

## Why event-driven, not a polling loop?

Most "agent orchestrators" run a `while True: sleep(60)` loop that wakes
1,440 times a day, queries a model, and finds nothing to do. That's a
*monitoring* cadence, not a *decision* cadence.

A CEO doesn't make decisions every 60 seconds. A CEO decides when something
arrives that needs deciding. So this agent:

- **Wakes on new intelligence** — a PostgreSQL `LISTEN/NOTIFY` trigger fires the
  moment a row lands in `intel_buffer`, and the engine does exactly one decision
  pass.
- **Does nothing when there's nothing to do** — no timer, no idle model calls,
  no "no decisions required" spam. Zero work when idle.
- **Makes one decision, then sleeps** — reads pending intel, produces a single
  LLM decision, writes it back, acknowledges the input, returns to sleep.

The result: near-zero CPU/API cost when idle, and decisions that arrive exactly
when there's something to act on.

## What it does

- Listens on a PostgreSQL channel (`ceo_wakeup`) via `LISTEN/NOTIFY`.
- On wake, pulls unacknowledged rows from `intel_buffer` addressed to it.
- Sends them to any **OpenAI-compatible** LLM (Ollama, DeepSeek, OpenAI, vLLM,
  KiloCode — your choice) with a CEO system prompt.
- Writes a structured decision (`DECISIONS` / `DIRECTIVES` / `STATUS`) back to
  `intel_buffer`, where downstream workers consume it.
- Acknowledges the input so it's never double-processed.
- Optionally pings an A2A hub as a liveness heartbeat (a ping, never a model
  call).

## Quick start

```bash
# 1. Create the queue + wakeup trigger (idempotent)
psql "$PG_DSN" -f schema.sql

# 2. Configure
cp config.example.env .env
# edit .env  (PG_DSN, LLM_BASE_URL, LLM_MODEL, LLM_API_KEY)

# 3. Run
python3 ceo_agent.py
```

To run as a systemd service, copy `systemd/ceo-agent.service` to
`/etc/systemd/system/`, edit the `WorkingDirectory`/`User`/`EnvironmentFile`
paths, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ceo-agent
```

## Configuration

Everything is environment-driven (see `config.example.env`):

| Variable | Purpose |
|----------|---------|
| `PG_DSN` | PostgreSQL connection string for `intel_buffer` |
| `LLM_BASE_URL` | OpenAI-compatible base URL (Ollama `/v1`, DeepSeek, OpenAI, vLLM…) |
| `LLM_MODEL` | Model name (e.g. `qwen2.5:7b`, `deepseek-chat`, `gpt-4o-mini`) |
| `LLM_API_KEY` | API key (use `ollama` for a local Ollama server) |
| `AGENT_ID` | This agent's identity (`source_agent` on its output) |
| `LISTEN_CHANNEL` | NOTIFY channel the trigger fires on (default `ceo_wakeup`) |
| `INTEL_TARGETS` | Comma-separated `target_agent` values this engine consumes |
| `DECISION_TARGET` | Who receives this engine's decisions |
| `HEARTBEAT_SEC` | Optional A2A liveness ping interval (`0` disables) |

## How it fits in a fleet

This is the **decision layer**, not the whole system. In production it's paired
with:

- **Producers** — scrapers, scouts, monitors, and other agents that `INSERT`
  rows into `intel_buffer` (`target_agent = 'ceo'`) whenever they have something
  to escalate.
- **Consumers** — workers that read the CEO's decisions out of `intel_buffer`
  and execute them (open tickets, dispatch jobs, send messages).
- **A periodic review** — a nightly "dream cycle" for pattern-spotting across
  the fleet, separate from the real-time decision engine.

The `intel_buffer` table is the contract: producers write intelligence, the CEO
writes decisions, consumers act. Everything is durable, queryable, and auditable
in PostgreSQL.

## License

MIT — see [LICENSE](LICENSE).
