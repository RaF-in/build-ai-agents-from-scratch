# Build Your Own AI Agent

A from-scratch async AI agent: a turn loop over an LLM, tool dispatch, sub-agent
spawning, and a Planner→Generator→Evaluator capability pipeline with parallel
worker fan-out. Runs as a CLI and a Telegram webhook server.

## Setup

```bash
uv sync
cp .env.example .env   # then fill in keys (see .env.example for every variable)
```

Run the CLI:

```bash
uv run python agent.py
```

Run the full service (Telegram webhook + cron + CLI):

```bash
uv run python main.py
```

Register the Telegram webhook:

```
https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<your-host>/webhook
```

## Observability (Logfire / OpenTelemetry tracing)

The agent is instrumented with [Logfire](https://logfire.pydantic.dev/) (traces +
lightweight metrics) so the otherwise-invisible runtime is visible in the Logfire
**UI** and the **console**.

**Token-gated, zero-overhead by default.** With no `LOGFIRE_TOKEN`, every span and
metric is a no-op — nothing is configured, sent, or printed, and behavior is
byte-for-byte unchanged. Set the token to turn tracing on.

### Span tree

```
agent.request → agent.turn → litellm <model> (auto) → agent.tool {name}
                                                       └─ capability.pipeline {name}
                                                          ├─ capability.phase plan → agent.subagent role:planner
                                                          ├─ capability.phase gen  → agent.subagent role:generator
                                                          │     └─ agent.tool DelegateWebSearchTool → capability.delegate → agent.subagent worker ×N
                                                          └─ capability.phase eval → agent.subagent role:evaluator
(also) session.compaction
```

LLM calls are auto-instrumented via litellm's native logfire callback (no
hand-instrumentation). Parentage across `await` / `asyncio.gather` is automatic.

### Metrics

- `tool.errors` (by `tool`) — tool dispatch failures.
- `subagent.failures` (by `role`, `status`) — non-completed sub-agent spawns.
- `llm.tokens` (by `kind`: input/output) — token usage read from litellm's response.

### Configuration

See `.env.example` for the full list. The tracing variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LOGFIRE_TOKEN` | *(unset)* | **The gate.** Unset ⇒ telemetry is a full no-op. |
| `OTEL_SERVICE_NAME` | `ai-agent` | Service name in the Logfire UI. |
| `AGENT_ENV` | `dev` | Environment label (dev/staging/prod). |
| `AGENT_VERSION` | *(unset)* | Build/version tag stamped on traces. |
| `TRACE_CAPTURE_CONTENT` | `0` | `0` = summaries (byte count + 200-char preview); `1` = full payloads. |
| `TRACE_CONSOLE` | `1` | `0` = send to the UI only (no console spans). |

Secrets (`api_key`, `telegram`, `tavily`, `groq` patterns) are scrubbed from spans.

### Verification runbook

1. **No-op safety** — with `LOGFIRE_TOKEN` unset, the suite passes unchanged:
   ```bash
   uv run python testing_files/run_all.py
   ```
2. **Console** — with the token set, run `uv run python agent.py` and send a
   prompt; you should see `agent.request → agent.turn → litellm → agent.tool`.
3. **UI** — run a deep-research prompt and confirm `capability.pipeline` + phases +
   per-role `agent.subagent` + concurrent `capability.delegate` fan-out, with
   tokens/cost on the litellm spans.
4. **Metrics** — force a tool error and a sub-agent timeout; confirm the
   `tool.errors` / `subagent.failures` counters increment.
5. **Scrubbing / gating** — default run shows previews + byte counts and no
   secrets; with `TRACE_CAPTURE_CONTENT=1` the full payloads appear.
6. **FastAPI** — `curl` the `/webhook` endpoint and confirm the HTTP span parents
   an `agent.request` subtree.
