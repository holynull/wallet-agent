# Task 2: Normalized OKX Wallet and Pre-Transaction Data

Implemented the provider-neutral OKX wallet adapter and pre-transaction
normalization layer.

## Delivered

- Added normalized `WalletTotalValue`, `TokenMarketDetails`,
  `TransactionContext`, `GasLimitEstimate`, and `SimulationResult` models.
- Extended `TokenBalance` with OKX source attribution, observation time, risk
  classification, and optional market details.
- Added a `WalletProvider` protocol for total value, token balances, gas-limit
  estimation, and simulation.
- Added `OkxWalletAdapter` for the documented `/api/v6` balance and
  pre-transaction endpoints.
- Added explicit chain-name/index mapping, native-asset construction, Decimal
  conversion, raw-balance normalization, and risk-token handling.
- Added secret-safe `OkxWalletError` failures for unsupported chains,
  malformed provider data, and upstream client failures.
- Kept raw OKX response fields inside the adapter; callers receive only domain
  models.

## Review Follow-Up

- Balance requests now use the documented `chains` query parameter. The total
  value endpoint sends its boolean `excludeRiskToken` value, while the token
  balance endpoint uses its documented `0`/`1` encoding.
- Token rows resolve `chainIndex` from each token entry rather than assuming an
  outer chain group.
- Pre-transaction calldata is sent under `extJson.inputData`.
- Simulation success is derived from the documented `failReason`; non-empty
  reasons become sanitized failure evidence.
- ERC-20 rows must provide explicit decimals and raw balance data. Symbol-based
  defaults are not used; only native assets may use configured native decimals
  to derive a missing raw amount.

## Verification

```text
UV_CACHE_DIR=/private/tmp/wallet-agent-uv-cache uv run pytest \
  tests/okx/test_models.py tests/okx/test_wallet.py \
  tests/domain/test_models.py tests/domain/test_contracts.py -q
27 passed

UV_CACHE_DIR=/private/tmp/wallet-agent-uv-cache uv run ruff check \
  src/wallet_agent/okx src/wallet_agent/domain/models.py \
  src/wallet_agent/domain/providers.py tests/okx/test_models.py \
  tests/okx/test_wallet.py
All checks passed!

git diff --check
clean
```
