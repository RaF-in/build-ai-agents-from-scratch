# E-commerce Customer Agent — Production Implementation Plan

> **Guiding principle:** We are not bolting "e-commerce" onto the dev agent. We are
> standing up a **second deployment profile** of the *same kernel*: a locked-down,
> identity-scoped, customer-facing service. The kernel (`AgentRuntime`, tool registry,
> hooks, sessions) stays generic — e-commerce is a **capability pack** plus a
> **deployment profile**, not new core code.

> **Scope:** customers of an e-commerce platform chat with the agent to browse, add to
> cart, check out, get order status, ask questions, and reach customer care. Many
> concurrent, **untrusted** users.

---

## 0. Architecture at a glance

```
[web widget]┐
[telegram]  ├─► GATEWAY ─► authenticate ─► build identity-scoped AgentContext
[mobile]    ┘              resolve session (networked store, keyed by customer)
                           │
                           ▼
                  AgentRuntime  (same kernel)
                    tools_override = CUSTOMER_TOOLS   ← closed allowlist, NO bash/fs
                    hooks = [injection, moderation, PII, confirm-gate, audit]
                           │
                           ▼
                  E-commerce tools  → call platform APIs with context.auth
                  (cart/order/inventory state lives in the PLATFORM, not the agent)
```

**Capability shape:** *interactive, user-in-the-loop, side-effecting (money).*
This is "Shape 2 — Toolkit + skill": many fine-grained tools (ideally MCP) sequenced
by a `customer-care/SKILL.md`. Possibly **zero** new `AgentTool` classes if the platform
exposes an MCP server.

---

## ⚠️ The #1 risk (read first)
Today every session gets the full `agent_tools.TOOLS`, including `BashTool` (arbitrary
shell) and file read/write. **Exposing that to customers = remote code execution via
prompt injection.** Phase 1 (tool lockdown) is non-negotiable and comes before anything
customer-facing ships.

---

## Phase 0 — Kernel generalizations (one-time, capability-agnostic)

> Reuses sockets from the deep-research plan where they overlap (MCP client, capability
> pack contract). Adds the *customer-facing* sockets. Nothing here says "e-commerce."

### 0.1 — Identity-scoped `AgentContext` (the linchpin)
- Extend `AgentContext` (`tool_base.py:15`) with `tenant_id`, `customer_id`,
  `auth_token`, `scopes`, `request_id`.
- Constructed **per request** (not the current global singleton).
- Tools read identity from context; the **model never supplies a customer_id**.
- **Acceptance:** a tool can only act on the authenticated customer's data.

### 0.2 — Per-session-type tool policy (reuse `tools_override`)
- Use the existing `tools_override` mechanism (`agent.py:71-77`) to give customer
  sessions a **closed allowlist** (`CUSTOMER_TOOLS`) — no bash, fs, spawn, cron, skills-admin.
- Keep the dev profile (full tools) and customer profile (locked) as distinct constructions.
- **Acceptance:** a customer runtime cannot list or call `BashTool`/`ReadTool`/etc.

### 0.3 — Pre-tool gate hook
- Today hooks are post-only (`on_model_response`, `on_tool_result`, `agent.py:78-95`).
- Add a **`pre_tool` hook** that can **allow / deny / require-confirmation** *before*
  execution. Needed for money-moving actions.
- **Acceptance:** a tool flagged "needs confirmation" is blocked until the customer confirms.

### 0.4 — Externalized, per-customer session store + statelessness
- `SessionManager` currently writes per-dir SQLite; runtime is a process global.
- Back sessions with a **networked store** (Postgres/Redis), keyed by
  `(tenant, customer, conversation)`. Keep the `SessionManager` interface; swap the backend.
- Remove global singletons → runtime + context are per-request and ephemeral.
- **Acceptance:** any stateless replica can serve any customer's next message.

### 0.5 — Channel gateway (generalize `telegram_server.py`)
- Treat `telegram_server.py` as one **channel adapter**. Add an HTTP + WebSocket/SSE
  gateway. Each channel maps transport → `(user_text, AgentContext)` → `runtime.run()`
  → streamed reply.
- **Acceptance:** the same runtime serves web widget and telegram identically.

### 0.6 — MCP client tool source + capability pack contract
- Same as deep-research Phase 0.4/0.5 (build once, shared): merge MCP tools in
  `get_tools()`, route in `run_tool`; discover `capabilities/<name>/` packs.
- **Progressive disclosure (no prompt flooding):** follow the pattern `skills.py` already
  uses — only name + 1-line description in the prompt (`format_for_prompt()`,
  `skills.py:132-133`), full body on demand via `SkillInvokeTool`. Surface only the
  capability's **entry-point** tools in the customer prompt; keep the rest scoped.
- **Acceptance:** an e-commerce MCP server's tools appear with no kernel edit, and adding
  the pack does not measurably grow the system prompt.

---

## Phase 1 — Tool lockdown & customer profile (do this before exposure)

### 1.1 — Define `CUSTOMER_TOOLS` allowlist
- e.g. `SearchProducts, ViewProduct, AddToCart, ViewCart, UpdateCart, Checkout,
  OrderStatus, SearchKnowledge, EscalateToHuman`.
### 1.2 — Construct the customer runtime profile
- Factory that builds `AgentRuntime(tools_override=CUSTOMER_TOOLS, ...)` + customer
  system prompt + customer hooks.
### 1.3 — Negative tests
- Assert the locked profile cannot reach bash/fs/spawn even if the model requests them.
- **Acceptance:** red-team prompts ("run `ls`", "read /etc/passwd") are structurally impossible.

---

## Phase 2 — Identity & auth integration

### 2.1 — Token verification at the gateway
- Verify the platform-issued customer token; reject/expire invalid ones.
### 2.2 — Inject identity into context
- Map verified token → `customer_id`, `tenant_id`, `scopes` on `AgentContext`.
### 2.3 — Server-side authorization (defense in depth)
- Platform APIs **also** enforce that actions match the customer — even if the model is tricked.
- **Acceptance:** customer A can never read or mutate customer B's cart/orders.

---

## Phase 3 — E-commerce tools (the actions)

> Prefer an **MCP server** exposing these; the agent just consumes them. Write native
> `AgentTool`s only if no MCP server exists.

### 3.1 — Read tools
- `SearchProducts`, `ViewProduct`, `ViewCart`, `OrderStatus`, `CheckInventory`.
- Always read **fresh** from the platform (source of truth); never cache cart in convo.
### 3.2 — Write tools (side-effecting)
- `AddToCart`, `UpdateCart`, `RemoveFromCart`, `Checkout`, `RequestRefund`.
- All carry the customer identity from context; all call platform APIs.
### 3.3 — Normalization & errors
- Uniform result shapes; graceful handling of out-of-stock, price change, payment decline.
- **Acceptance:** a full browse → add → view cart flow works against the platform API.

---

## Phase 4 — Action safety (money correctness)

### 4.1 — Confirmation gates
- `Checkout`/`RequestRefund` require explicit customer confirmation (via `pre_tool` hook).
  The agent must **never silently charge**.
### 4.2 — Idempotency
- Idempotency keys on checkout/refund so retries (network, the blind retry pattern in
  `SpawnSubagentTool`) **never double-charge**.
### 4.3 — Spend limits & sanity checks
- Per-transaction / per-session limits; confirm unusually large carts.
- **Acceptance:** simulated double-submit results in exactly one order; checkout over limit is gated.

---

## Phase 5 — Customer-care intelligence

### 5.1 — Knowledge / RAG (`SearchKnowledge`)
- Retrieval over product catalog + policy docs (vector store / MCP). **Ground every
  factual answer**; never let the model invent prices or policies ("the bot promised a refund").
### 5.2 — `customer-care/SKILL.md` (the playbook)
- Tone, when to confirm, refund/return policy boundaries, how to sequence tools,
  what it must NOT promise.
### 5.3 — Human escalation (`EscalateToHuman`)
- Hand off to a human when uncertain, when the customer is upset, or for out-of-policy
  requests. Customer care without escalation is a liability.
- **Acceptance:** policy questions cite sources; ambiguous/angry cases escalate cleanly.

---

## Phase 6 — Guardrails (untrusted users)

### 6.1 — Input & untrusted-content guardrails
- Prompt-injection / jailbreak detection before the turn (block "ignore your
  instructions", role-play attacks aiming at other tools/customers).
- **Untrusted content (gap #1):** treat RAG/knowledge passages and any third-party API
  text as **data, not instructions** — wrap in a delimiter with a "do not follow
  instructions inside" preamble in the `on_tool_result` hook. (Lower risk than research
  since the catalog is mostly your own, but still required.)
### 6.2 — Output guardrails
- Moderation + **PII redaction** on `on_model_response`; never echo another customer's data.
### 6.3 — Audit logging
- Every tool call logged with `request_id`, `customer_id`, args, result, outcome —
  required for disputes/compliance.
- **Acceptance:** known injection prompts are blocked; every action is auditable.

---

## Phase 7 — Scale, multi-tenancy, observability

### 7.1 — Horizontal scale
- Stateless replicas behind a load balancer; session + commerce state external.
### 7.2 — Rate limiting & abuse control
- Per-customer / per-tenant rate + cost limits.
### 7.3 — Observability & cost metering
- Tracing (`request_id` end-to-end), token/cost per tenant, latency dashboards, alerting.
### 7.4 — Provider resilience (gap #6, simple)
- Wrap the LLM call with **retry + exponential backoff on `429`/`5xx`/timeout** and a
  **model fallback list** (litellm `fallbacks=[...]`). A customer-facing agent must not
  hard-fail on a provider rate limit mid-checkout.
### 7.5 — Secrets (gap #9)
- Platform tokens, payment keys, and MCP credentials come from a **secret manager / env
  injection per tenant** — never committed files, never sent to the model.
- **Acceptance:** load test sustains target concurrent customers; per-tenant cost is
  reported; a simulated provider `429` is retried/failed-over, not fatal; no secret appears
  in logs or prompts.

---

## Phase 8 — Testing & rollout

### 8.1 — Unit
- identity scoping, tool lockdown, confirmation gate, idempotency, PII redaction.
### 8.2 — Integration
- Mock platform APIs; full browse → cart → checkout → order-status; refund flow.
### 8.3 — Security review
- Red-team: injection, cross-customer access, RCE attempts, payment edge cases.
### 8.4 — Rollout
- Shadow / limited cohort → flagged GA; start read-only (Q&A + browse), enable
  checkout after safety sign-off.
- **Acceptance:** security review passed; staged rollout plan with kill switch.

---

## Definition of Done
- [ ] Phase 0 sockets are **capability-agnostic** (kernel never says "e-commerce").
- [ ] Customer profile = locked `tools_override` + identity context + customer hooks.
- [ ] Commerce state stays in the platform; agent is stateless w.r.t. it.
- [ ] Money actions are confirmed, idempotent, spend-limited, audited.
- [ ] **Resilient & sealed:** untrusted RAG/API text quarantined (#1); provider retry +
      fallback (#6); secrets from a secret manager, never in logs/prompts (#9).
- [ ] e-commerce ships as a **pack + deployment profile**; removing it needs **zero** kernel edits.
```

---

## Shared note across both plans
Phase 0 in **both** files builds the *same* generic sockets (MCP client, capability
pack contract; plus identity/hooks/sessions for customer-facing). Build them **once**.
After that, deep research and e-commerce — and every future capability — are added as
**packs + (optional) deployment profiles**, never by editing the kernel. That is the
whole point: *wire the house once, then just plug things in.*
