# Task 1: Signed OKX Transport

Implemented the optional OKX signed transport and configuration wiring.

## Delivered

- `OkxSignedClient` canonicalizes sorted GET query parameters and compact,
  sorted POST JSON before signing and sends the exact signed bytes.
- Requests use Base64 HMAC-SHA256 with the required `OK-ACCESS-*` headers and
  optional `OK-ACCESS-PROJECT`.
- Successful responses require an OKX envelope with `code == "0"`.
- Transport failures, HTTP 429, and HTTP 5xx retry up to the configured
  attempt limit. `Retry-After` is honored; authentication and application
  errors are not retried.
- `OkxClientError` exposes stable code/status/retryable fields without raw
  upstream payloads or credential values.
- `Settings` supports optional OKX credentials as `SecretStr` values. Disabled
  mode needs no credentials; enabled mode requires API key, secret, passphrase,
  and project ID.
- `build_application()` creates and registers the client only when
  `OKX_ENABLED=true`.

## Verification

```text
UV_CACHE_DIR=/private/tmp/wallet-agent-uv-cache uv run pytest \
  tests/okx/test_client.py tests/okx/test_config.py tests/test_config.py -q
14 passed

UV_CACHE_DIR=/private/tmp/wallet-agent-uv-cache uv run ruff check \
  src/wallet_agent/okx src/wallet_agent/config.py src/wallet_agent/main.py tests/okx
All checks passed!

git diff --check
clean
```
