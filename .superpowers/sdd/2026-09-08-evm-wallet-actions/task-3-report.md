# Task 3 Report

Implemented the CoinGecko price provider/config slice.

What changed:
- Added `wallet_agent.prices` with `CoinGeckoPriceProvider`.
- Added CoinGecko settings for API key, base URL, token/native id maps, and cache TTL.
- Threaded an optional price provider through `GraphRuntime`, `build_graph`, `create_app`, and `build_application`.
- Extended the shared JSON transport with GET support so CoinGecko can reuse the existing retry behavior.
- Added focused tests for price normalization/cache behavior, retryable failure handling, config defaults, transport GET support, and optional graph/app injection.

Verification:
- `PYTHONPATH=src /Users/zhangleping/github.com/holynull/wallet-agent/.venv/bin/pytest tests/chains/test_transports.py tests/prices tests/test_config.py tests/graph tests/api/test_api_contract.py tests/api/test_auth.py tests/api/test_demo_page.py tests/api/test_swap_flow.py -q`
- `PYTHONPATH=src /Users/zhangleping/github.com/holynull/wallet-agent/.venv/bin/ruff check src/wallet_agent/providers/http.py src/wallet_agent/prices/coingecko.py src/wallet_agent/prices/__init__.py src/wallet_agent/config.py src/wallet_agent/graph/nodes.py src/wallet_agent/graph/build.py src/wallet_agent/api/app.py src/wallet_agent/main.py tests/chains/test_transports.py tests/prices/test_coingecko.py tests/test_config.py tests/graph/test_prices.py tests/api/test_price_injection.py tests/graph/test_graph_paths.py`

Concern:
- Test runs still emit the existing LangGraph `allowed_objects` deprecation warning from the dependency stack.

Fix evidence:
- Added a regression test showing CoinGecko reuses cached partial batches safely while expanding a later request from `ETH` to `ETH + USDC` without dropping the new asset.
- Added regression tests showing `HttpJsonTransport.get()` retries `429`/`5xx` responses and does not retry permanent `4xx` statuses.
- Verified with `./.venv/bin/pytest tests/chains/test_transports.py tests/providers/test_bridgers.py tests/providers/test_omnibridge.py tests/prices/test_coingecko.py -q`.
- Verified with `./.venv/bin/ruff check src/wallet_agent/providers/http.py src/wallet_agent/domain/models.py src/wallet_agent/prices/coingecko.py tests/providers/test_bridgers.py tests/prices/test_coingecko.py`.
