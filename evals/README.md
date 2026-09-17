# Wallet Agent Evals

This suite measures the three core wallet capabilities against the real
LangGraph workflow:

- balance lookup and portfolio valuation;
- native-token transfer preparation, compact input, and precise missing-recipient clarification;
- swap quote generation, multi-turn slot retention, corrections, cancellation, temporary balance
  queries, and symbol typo normalization.

The 13 cases use structural assertions. Clarification cases must keep every slot
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

The current acceptance result is 13/13 in deterministic mode and 13/13 with
the configured `deepseek-chat` model (recorded 2026-09-17).
