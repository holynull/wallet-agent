# Task 1 Report

Implemented the domain contract slice for EVM wallet actions.

What changed:
- Added `TransferRequest`, `AllowanceRequirement`, `ApprovalTransaction`, `TokenPrice`, and `SwapAuthorizationState`.
- Extended `NormalizedQuote` with `usd_input_value`, `usd_expected_output`, `price_snapshots`, and `allowance_requirement`.
- Added `TokenPriceProvider` to `src/wallet_agent/domain/providers.py`.
- Tightened domain redaction so declared model fields like `token` are preserved while nested arbitrary mappings still redact secrets.

Verification:
- `PYTHONPATH=src pytest tests/domain/test_models.py -q`
- `PYTHONPATH=src pytest tests/domain/test_models.py tests/domain/test_contracts.py -q`

Notes:
- The worktree venv resolves the installed package from the main checkout, so verification used `PYTHONPATH=src` to force the worktree source.
