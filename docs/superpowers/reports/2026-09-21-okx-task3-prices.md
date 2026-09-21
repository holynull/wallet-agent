# Task 3: OKX Market Price Providers

Implemented the normalized OKX market-price adapter and per-asset composite
fallback provider.

## Delivered

- Added `OkxPriceProvider` for current prices, detailed market metrics,
  historical index prices, and OHLC candles.
- Added normalized `HistoricalPricePoint`, `HistoricalPricePage`, and
  `PriceCandle` domain models.
- Extended `TokenPrice` with explicit provider attribution and CoinGecko now
  reports `provider="coingecko"`.
- Normalized Decimal monetary values and timezone-aware UTC timestamps.
- Added documented period and limit validation, cursor preservation, native
  asset handling, and lowercase EVM contract addresses.
- Added short-TTL request caching and request coalescing for OKX price calls.
- Added `CompositePriceProvider`, which prefers OKX per asset and asks
  CoinGecko only for missing assets; prices are never averaged.
- Provider failures remain sanitized through structured `AgentError` metadata.

## Verification

```text
UV_CACHE_DIR=/private/tmp/wallet-agent-uv-cache uv run pytest \
  tests/prices/test_okx.py tests/prices/test_composite.py \
  tests/prices/test_coingecko.py tests/graph/test_prices.py \
  tests/api/test_price_injection.py -q
10 passed

UV_CACHE_DIR=/private/tmp/wallet-agent-uv-cache uv run ruff check \
  src/wallet_agent/prices src/wallet_agent/domain/models.py \
  src/wallet_agent/domain/providers.py tests/prices/test_okx.py \
  tests/prices/test_composite.py tests/prices/test_coingecko.py
All checks passed!

git diff --check
clean
```
