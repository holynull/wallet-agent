# Wallet Agent Evals

This suite measures the three core wallet capabilities against the real
LangGraph workflow:

- balance lookup and portfolio valuation;
- native-token transfer preparation and precise missing-recipient clarification;
- swap quote generation, multi-turn slot retention, and precise missing-amount clarification.

Clarification cases also assert that the agent keeps every slot the user already
provided. A generic clarification that discards known amounts, assets, or chains
does not pass.

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
