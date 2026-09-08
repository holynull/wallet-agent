# Wallet Agent Productionization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing LangGraph wallet-agent skeleton into a tested, resumable Swap MVP that a mobile wallet can safely call through REST and SSE.

**Architecture:** FastAPI remains the app boundary, LangGraph remains the resumable state machine, and a session service projects graph state into an app-facing swap session. Provider and chain clients stay outside checkpointed state; only validated identifiers, normalized domain models, and user-visible actions cross the API boundary.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, LangGraph 0.6.x, LangChain Core 0.3.x, SQLAlchemy/aiosqlite, httpx, pytest, pytest-asyncio, and ruff.

**Spec:** `docs/superpowers/specs/2026-09-04-wallet-langgraph-agent-design.md`

## Global Constraints

- Never accept or store private keys, seed phrases, signers, wallet clients, or provider credentials in requests, prompts, checkpoints, logs, or database payloads.
- `/broadcast` accepts only a chain-qualified transaction hash and is idempotent for the same hash.
- Provider writes are guarded by stable session/provider references and are never replayed after an already persisted success.
- LangGraph state is typed, JSON-serializable, versionable, and contains no live clients.
- Unsupported chain capabilities return `CHAIN_CAPABILITY_UNAVAILABLE`; no model-generated fallback is allowed.
- Every retry and polling loop has a maximum attempt count, timeout, and explicit terminal state.
- External Provider, RPC, and DeepSeek calls run only in explicit integration tests; unit tests use deterministic fakes.

---

### Task 1: Complete Swap session lifecycle

**Files:**
- Modify: `src/wallet_agent/graph/state.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `src/wallet_agent/api/app.py`
- Modify: `src/wallet_agent/persistence/store.py`
- Test: `tests/api/test_swap_flow.py`

**Interfaces:**
- `POST /v1/agent/turn` returns `{run_id, conversation_id, session_id?, status}`.
- `SwapSessionRecord` stores the normalized quote, pending transaction/deposit order, provider order, tx hash, and status.
- `POST /v1/swap/{session_id}/confirm` resumes the same LangGraph `thread_id`.
- `POST /v1/swap/{session_id}/broadcast` registers a hash exactly once and updates the projection.

- [ ] Write failing tests for quote → interrupt → confirm → prepare, same-hash duplicate broadcast, conflicting hash rejection, and session ownership.
- [ ] Run `pytest tests/api/test_swap_flow.py -q` and verify the new lifecycle assertions fail.
- [ ] Implement a session coordinator that creates a session before graph execution, persists `thread_id`, and projects graph updates after every run.
- [ ] Add idempotency checks before provider writes and ensure a resumed graph does not call `prepare` twice.
- [ ] Run the focused tests and the full suite.
- [ ] Commit with `feat: complete swap session lifecycle`.

### Task 2: Harden FastAPI and SSE contract

**Files:**
- Modify: `src/wallet_agent/api/app.py`
- Test: `tests/api/test_api_contract.py`

**Interfaces:**
- Add `GET /health` and `GET /ready`.
- Use stable error envelopes with `code`, `message`, and optional `details`.
- SSE emits `update`, `action_required`, `complete`, and `error`, and can replay persisted events after reconnect.

- [ ] Write failing tests for health/readiness, malformed requests, rejected signing material, SSE completion, SSE action-required, missing run, and unsupported chain errors.
- [ ] Run the focused tests and verify failures.
- [ ] Implement the endpoints, bounded event replay, and consistent HTTP status mapping without leaking exception internals.
- [ ] Run API tests, full tests, Ruff, and compileall.
- [ ] Commit with `feat: harden REST and SSE contracts`.

### Task 3: Durable checkpoints and session persistence

**Files:**
- Create: `src/wallet_agent/persistence/checkpoints.py`
- Create: `src/wallet_agent/persistence/migrations.py`
- Modify: `src/wallet_agent/persistence/store.py`
- Modify: `src/wallet_agent/main.py`
- Test: `tests/persistence/test_recovery.py`

**Interfaces:**
- Production app uses a durable LangGraph checkpointer selected by `PERSISTENCE_URL`.
- SQLite remains available for local development; PostgreSQL is the production target.
- `SessionStore` supports ownership-safe reads, atomic broadcast updates, and status projection.

- [ ] Write failing restart tests that interrupt a swap, rebuild the graph/store, resume with the same `thread_id`, and assert no duplicate provider write.
- [ ] Run the focused tests and verify they fail against the current in-memory default.
- [ ] Implement durable checkpointer selection, schema initialization/migration, and an atomic compare-and-set broadcast update.
- [ ] Run recovery tests with SQLite and full local verification.
- [ ] Commit with `feat: add durable graph recovery`.

### Task 4: Authentication and ownership boundaries

**Files:**
- Create: `src/wallet_agent/api/auth.py`
- Create: `src/wallet_agent/api/dependencies.py`
- Modify: `src/wallet_agent/api/app.py`
- Test: `tests/api/test_auth.py`

**Interfaces:**
- API derives `user_id` from a bearer token instead of trusting a request-body identity.
- Session and wallet operations verify authenticated ownership before reading or mutating data.
- Unit tests use a deterministic token verifier; no external identity provider is called.

- [ ] Write failing tests for missing token, invalid token, cross-user session access, and valid owner access.
- [ ] Run focused tests and verify failures.
- [ ] Implement a narrow verifier interface and dependency injection for tests and deployment.
- [ ] Remove client-controlled `user_id` from mutation authority while preserving a migration-compatible response field.
- [ ] Run full tests and lint.
- [ ] Commit with `feat: enforce API authentication and ownership`.

### Task 5: Request-level model routing

**Files:**
- Create: `src/wallet_agent/models/registry.py`
- Modify: `src/wallet_agent/config.py`
- Modify: `src/wallet_agent/main.py`
- Modify: `src/wallet_agent/api/app.py`
- Test: `tests/models/test_registry.py`

**Interfaces:**
- Supported model IDs are configured server-side, for example `deepseek-chat`, `deepseek-reasoner`, and `gpt-4o-mini`.
- A request may select a whitelisted `model_id`; it may not provide an API key, arbitrary base URL, or provider credentials.
- Checkpoints persist only the selected model ID, never the model client object.

- [ ] Write failing tests for default DeepSeek selection, allowed temporary model override, unknown model rejection, and secret-field rejection.
- [ ] Run focused tests and verify failures.
- [ ] Implement a model factory/router that creates OpenAI-compatible clients from server configuration and injects the selected model into graph runtime.
- [ ] Add DeepSeek structured-output compatibility coverage with a deterministic fake response.
- [ ] Run full tests and lint.
- [ ] Commit with `feat: add request-level model routing`.

### Task 6: Explicit integration gate

**Files:**
- Create: `tests/integration/test_deepseek.py`
- Create: `tests/integration/test_providers.py`
- Create: `tests/integration/test_chain_rpc.py`
- Modify: `pyproject.toml`
- Modify: `README.md`

**Interfaces:**
- Integration tests are skipped unless `RUN_INTEGRATION_TESTS=1` and the required environment variables are present.
- No integration test runs during the default `pytest -q` command.

- [ ] Write skipped-by-default tests for DeepSeek intent parsing, Bridgers/OmniBridge quote/order status, and one read-only RPC per supported chain.
- [ ] Run default tests and verify integrations remain skipped.
- [ ] Implement explicit environment guards, redacted diagnostics, and bounded network timeouts.
- [ ] Document staging setup and testnet-only verification in README.
- [ ] Run full tests, Ruff, and compileall.
- [ ] Commit with `test: add opt-in integration gate`.

### Task 7: Mobile integration and release hardening

**Files:**
- Create: `docs/mobile-integration.md`
- Create: `docs/openapi-wallet-agent.json`
- Modify: `README.md`
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Mobile documentation specifies the exact turn/SSE/confirm/broadcast/status sequence and signing boundary.
- CI runs tests, Ruff, compileall, and verifies no secret-like configuration fields are introduced.
- Release checklist covers migrations, health probes, rate limits, metrics, log redaction, and rollback.

- [ ] Write documentation examples for Bridgers calldata and OmniBridge `platformAddr` deposit flows.
- [ ] Generate or export the OpenAPI contract and validate it against the FastAPI app.
- [ ] Add CI commands matching local verification.
- [ ] Run the final release gate: `pytest -q`, `ruff check src tests`, `python3 -m compileall src`, and `git diff --check`.
- [ ] Commit with `docs: publish mobile integration contract`.
