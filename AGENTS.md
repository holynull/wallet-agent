# AGENTS.md

Guidance for coding agents working in this repository.

## Project overview

`wallet-agent` is a non-custodial Python/LangGraph service for a mobile crypto
wallet. The backend plans wallet actions, produces unsigned transactions, and
registers broadcast hashes; it never holds private keys, seed phrases, or
signers. A Next.js demo (`demo-next/`) is the only UI and does the wallet
interaction in the browser.

## Setup and run

- Python `>=3.11`. Dependencies use `uv`: run `uv sync` after changing
  `pyproject.toml`.
- Copy `.env.example` to `.env` before running anything that loads `Settings`.
  A missing `.env` makes `./scripts/start_local.sh` exit early.
- Start the API locally: `./scripts/start_local.sh` (add `--reload` for hot
  reload). It imports `wallet_agent.main:app` with `PYTHONPATH=src`.
- Full stack: `docker compose up -d --build`; redeploy with
  `./scripts/redeploy.sh` (add `--no-cache` to rebuild images).
- Smoke check a running API: `python scripts/smoke_test.py --base-url http://localhost:8000`.

## Verification

Run these before finishing a change (from the repo root):

```bash
.venv/bin/pytest -q              # full offline test suite
.venv/bin/ruff check src tests   # lint (E, F, I; line-length 100)
python3 -m compileall src        # import/compile safety
.venv/bin/python -m evals.wallet_agent_evals        # deterministic 15-case suite
.venv/bin/python -m evals.wallet_app_evals          # REST/SSE + LangGraph lifecycle
```

- Tests are offline by default: chain transports, providers, and models are
  injectable fakes. Never add tests that sign, broadcast, or hit real RPC/HTTP
  unless marked `integration` (see `pyproject.toml` markers).
- Browser tests need Playwright Chromium and are opt-in:
  `.venv/bin/pytest -q tests/browser -m browser`.
- `pytest` runs in `asyncio_mode = "auto"`; write async tests without a manual
  event loop fixture.

## Layout

- `src/wallet_agent/api/` — FastAPI app, auth, dependencies. HTTP routes live
  under `/v1`.
- `src/wallet_agent/graph/` — LangGraph state machine (`build.py`, `nodes.py`,
  `routes.py`, `state.py`, `read_only.py`). A router splits conversation vs.
  lifecycle graphs that share one checkpointer.
- `src/wallet_agent/chains/` — per-family transports (EVM, TRON, Solana) behind
  a registry. Execution-critical RPC checks only.
- `src/wallet_agent/providers/` — swap providers (Bridgers, OmniBridge) and the
  HTTP transport.
- `src/wallet_agent/okx/` — signed OKX Onchain OS read-only integration.
- `src/wallet_agent/domain/`, `models/`, `persistence/`, `prices/` — models,
  contracts, checkpoint/run stores, price providers.
- `src/evals/` — evaluation harnesses (shipped as an installed package).
- `demo-next/` — Next.js 12 demo UI.
- `docs/superpowers/` — design specs and reports; check these for intent behind
  a subsystem before large changes.

## Conventions

- Python style is enforced by `ruff` (`E`, `F`, `I`), line length 100. Add
  `from __future__ import annotations` and type hints to new modules, matching
  the existing files.
- Prefer the existing injectable-provider pattern over direct network or model
  calls in library code so tests stay offline.
- Behavior changes to the agent loop should keep write capabilities
  (signing/transfer/swap/broadcast) out of model tool sets; they stay
  deterministic graph nodes behind wallet confirmation.
- Use `rg` for searching; `git log`/`git blame` for history.

## Security boundary

Do not place private keys, seed phrases, signer objects, wallet clients, signed
transactions, or provider credentials into requests, prompts, graph state,
checkpoints, logs, demo debug output, or issue reports. Broadcast endpoints
accept only a transaction hash plus chain.
