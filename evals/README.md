# Wallet Agent Evals

## Phase 1 agent evaluation

This suite measures the core wallet capabilities against the real LangGraph
workflow:

- balance lookup and portfolio valuation;
- native-token transfer preparation, compact input, and precise missing-recipient clarification;
- swap quote generation, multi-turn slot retention, corrections, cancellation, temporary balance
  queries, and symbol typo normalization.

The 15 cases use structural assertions. Clarification cases must keep every slot
the user already provided, corrections must invalidate stale quote artifacts,
and temporary queries must preserve the active task. A generic clarification
that discards known amounts, assets, or chains does not pass.

The default mode is deterministic and suitable for CI:

```bash
.venv/bin/python -m evals.wallet_agent_evals
```

The opt-in language evaluation uses the configured OpenAI-compatible model:

```bash
.venv/bin/python -m evals.wallet_agent_evals --online
```

Both modes use simulated chain, price, and swap-provider adapters. They never
sign transactions, broadcast transactions, or use real provider/RPC services.
Cases are scored with structural assertions rather than an LLM judge. A nonzero
exit code means at least one case failed (or online configuration was missing).

The deterministic acceptance result is 15/15.

## Phase 2 Wallet App lifecycle evaluation

Phase 2 drives the production-facing FastAPI REST/SSE contract and the compiled
LangGraph through deterministic EIP-1193 wallet, provider, and chain simulators.
Run the complete ten-scenario suite or reproduce one scenario by its exact ID:

```bash
.venv/bin/python -m evals.wallet_app_evals
.venv/bin/python -m evals.wallet_app_evals --scenario approval_pending_restart_resume
```

Available scenario IDs, in full-suite execution order, are:

1. `erc20_swap_without_approval`
2. `erc20_swap_with_approval`
3. `approval_pending_restart_resume`
4. `wallet_rejects_approval`
5. `wallet_rejects_swap`
6. `swap_temporarily_not_visible`
7. `swap_reverted`
8. `duplicate_and_conflicting_swap_hash`
9. `provider_register_timeout_then_retry`
10. `omnibridge_erc20_deposit_order`

The command writes exactly one JSON document to stdout. Exit code `0` means all
selected scenarios passed. Exit code `1` means one or more scenarios failed or
were blocked. Exit code `2` identifies an unknown scenario, invalid CLI usage,
or evaluator configuration/runtime error.

Top-level report fields are `schema_version`, `mode`, `summary`, `dimensions`,
and `scenarios`. Each serialized scenario includes lifecycle `steps`, sanitized
`wallet_calls` and `provider_calls`, `invariants`, and `failures`, together with
its ID, status, final stage, and dimension membership. Schema version 1 uses
mode `offline-wallet-app`; summary and dimension counts are derived from the
serialized scenario results.

The restart scenario means Wallet App client reconstruction only: the old
client instance is discarded and rebuilt from public conversation/session
identifiers while the FastAPI service and its in-memory store remain running.
It does not claim backend-process restart or durable-store recovery.

The default Phase 2 suite is fully offline. It does not read `.env`, import
production settings, generate or retain private keys, mnemonics, signatures, or
signed raw transactions, call real Provider/RPC transports, or submit an
on-chain broadcast. Anvil/local-chain execution and opt-in real Provider/RPC
smoke modes are explicitly outside this evaluation phase.
