# Progressive HMAC, anonymous identity, and email verification design

## Context

The current API supports two independent controls:

- `AUTH_REQUIRED` with `AUTH_TOKENS`: static Bearer-token identity mapping.
- Optional `ADMIN_TOKEN`: operator-only access to debug endpoints.

With authentication disabled, the API trusts the `user_id` supplied by the App.
Conversation and swap ownership are separated by that value, but a client can
claim another user's `user_id`.

CatWallet currently uses a shared client `SecretKey` and HMAC request
signatures. The shared key proves that a request came from a CatWallet client,
but it does not prove which user sent the request.

## Goals

1. Protect every App-facing `/v1` route with a unified request signature.
2. Allow test and local environments to disable signature enforcement.
3. Keep `AUTH_REQUIRED` independent from request signing.
4. Support an anonymous tier where `user_id` remains the logical storage key.
5. Prepare a later account upgrade at a token-usage threshold using verified
   email and dynamic access/refresh tokens.
6. Keep debug endpoints operator-only through `ADMIN_TOKEN`.

## Non-goals

- Proving wallet-address ownership.
- Putting private keys, seed phrases, signers, or wallet clients in the backend.
- Treating a shared App HMAC key as a per-user security boundary.

## Request signature

### Headers

```http
X-Timestamp: <Unix timestamp in milliseconds>
X-Nonce: <random per-request value>
X-Signature: <Base64 HMAC-SHA256>
X-Key-Id: <platform/version key id when multiple keys are configured>
X-App-Version: <optional App version>
X-Platform: <optional ios|android>
```

### Canonical string

```text
UPPERCASE_METHOD + "\n"
raw_path + "\n"
raw_query_string + "\n"
X-Timestamp + "\n"
X-Nonce + "\n"
sha256(raw_request_body_bytes)
```

- The raw path and raw query string are used exactly as received.
- The body hash uses the exact request bytes. Empty bodies hash `b""`.
- The signature excludes the `X-Signature` header itself.
- `X-Signature` is `Base64(HMAC-SHA256(SecretKey, canonical_string))`.
- Timestamp skew and nonce replay are both rejected.
- Comparison uses constant-time comparison.

### Modes

```dotenv
REQUEST_SIGNATURE_MODE=off|warn|enforce
REQUEST_SIGNATURE_SECRET=<shared App SecretKey>
REQUEST_SIGNATURE_KEYS={"ios-v1":"...","android-v1":"..."}
REQUEST_SIGNATURE_TTL_SECONDS=300
```

- `off`: ignore signature headers.
- `warn`: validate signatures but continue processing when invalid; record the
  result for rollout diagnostics.
- `enforce`: reject missing, invalid, stale, replayed, or unverifiable requests.

Exempt routes:

- `/health`
- `/ready`
- `/docs`, `/openapi.json`, `/redoc`
- `/debug/*` (operator token required instead)

Nonce claims are stored in Redis when available, with an in-process fallback for
tests and single-process local runs.

When `REQUEST_SIGNATURE_KEYS` is configured, App requests select the platform
key through `X-Key-Id`. This supports distinct iOS and Android keys and enables
per-key rotation. The single `REQUEST_SIGNATURE_SECRET` remains supported for
backward compatibility.

### Error codes

- `REQUEST_SIGNATURE_REQUIRED`
- `INVALID_REQUEST_SIGNATURE`
- `STALE_REQUEST_TIMESTAMP`
- `REPLAYED_REQUEST_NONCE`
- `REQUEST_SIGNATURE_UNAVAILABLE`

## Auth modes

`AUTH_REQUIRED` remains independent:

| Mode | User identity | Request protection |
| --- | --- | --- |
| No auth, signature off | client `user_id` | TLS only |
| No auth, signature enforce | client `user_id` | App-shared HMAC |
| Static Bearer auth | token-mapped `user_id` | optional HMAC |
| Hybrid/dynamic auth | Bearer token, fallback anonymous identity | HMAC + token |

Production must not describe a shared HMAC key as per-user authentication.

## Anonymous tier

Until the account-upgrade threshold is reached:

- App supplies a stable `user_id`.
- Backend records token usage against that `user_id`.
- Requests are protected by HMAC in production.
- The accepted risk is that a malicious App client can claim another `user_id`.

If cross-user isolation becomes a security requirement, anonymous `user_id`
must become server-issued or otherwise bound to a trusted identity.

## Account upgrade trigger

Suggested policy:

```dotenv
EMAIL_VERIFICATION_REQUIRED_AFTER_TOKENS=50000000
```

When a user crosses the threshold, protected expensive endpoints return:

```json
{
  "code": "EMAIL_VERIFICATION_REQUIRED",
  "message": "继续使用需要完成邮箱验证"
}
```

The value and scope of the threshold are product configuration; they must be
validated server-side.

## Email verification and dynamic tokens

Later phases add:

```http
POST /v1/auth/email/send-code
POST /v1/auth/email/verify
POST /v1/auth/refresh
POST /v1/auth/logout
```

The verified email is bound to the existing `user_id`, avoiding conversation
and session migration where possible.

Dynamic tokens are opaque random tokens stored as SHA-256 hashes in Redis:

```text
wallet-agent:auth:email-code:<email-or-user>
wallet-agent:auth:access:<sha256(access-token)>
wallet-agent:auth:refresh:<sha256(refresh-token)>
```

Access tokens are short-lived; refresh tokens rotate. Logout revokes the token
family. The token verifier becomes asynchronous and is injected alongside the
existing static verifier.

`AUTH_MODE=hybrid` allows:

- no Bearer token: anonymous/HMAC behavior while the user remains below the
  threshold;
- Bearer token: verified dynamic identity, which is authoritative for ownership.

## Security constraints

- Never trust a client-supplied `user_id` as proof of a verified account.
- Never expose email codes or tokens in logs.
- Limit email-code attempts and challenge attempts.
- Use HTTPS everywhere.
- Do not expose `/debug/*` publicly.
- Do not add a public key-generation endpoint; generate secrets with an offline
  CLI or `openssl rand -hex 32`.

## Rollout

1. Implement unified HMAC signing with `off` as the default.
2. Keep test environments at `off` while the App integrates.
3. Validate with `warn`, inspect signature failures, then switch test to
   `enforce`.
4. Deploy production in `warn`, then move to `enforce`.
5. Add usage accounting and the email-upgrade flow.
6. Add dynamic access/refresh tokens and `AUTH_MODE=hybrid`.
7. Require Bearer identity for users above the threshold.

## Testing

- Canonical-string and HMAC known vectors.
- Missing/invalid/stale/replayed request rejection.
- Same request accepted once in `enforce` mode.
- `warn` mode records but does not reject.
- `off` mode ignores signature headers.
- Exempt route coverage.
- Body and query signing.
- Redis and in-memory nonce-store behavior.
- Later phases: email-code expiry/attempt limits, token rotation, logout,
  owner isolation, and hybrid identity precedence.
