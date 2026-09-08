# Bridgers API — Status & Error Codes

> Source: https://docs-bridgers.bridgers.xyz/bridgers-api-jie-kou/status-code.md
> Distilled: 2026-08-03

---

## resCode — API-level codes

Every response carries `resCode` (string `"100"` or number `100` for success).
Match on the structured code, NOT on message text (`contains("777")` is fragile).

| resCode | Message (CN) | Category | How to handle |
|---|---|---|---|
| `100` | success | ✅ Success | proceed |
| `101` | 必填参数为空 | ClientError | surface to caller, no retry |
| `102` | 参数无效 | ClientError | surface to caller, no retry |
| `103` | 地址无效 | ClientError | surface to caller, no retry |
| `104` | 金额应为有效的数字字符串 | ClientError | surface to caller, no retry |
| `105` | 无法进行同币种兑换 | ClientError | surface to caller, no retry |
| `106` | token 不存在 | ClientError | surface to caller, no retry |
| `107` | 此 fromToken 暂不支持兑换 | ClientError | surface to caller, no retry |
| `108` | 此 toToken 暂不支持兑换 | ClientError | surface to caller, no retry |
| `109` | 目标地址不支持币种合约地址 | ClientError | surface to caller, no retry |
| `110` | 目标地址不支持合约地址 | ClientError | surface to caller, no retry |
| `411` | 账户余额不足 | ClientError | surface to caller, no retry |
| **`414`** | **已更新，请勿再次更新** | **Idempotent** | **`upload_order_hash` only: treat as success (order already recorded)** |
| `412` | 询价失败 | Retryable | backoff retry |
| `413` | 兑换失败 | Retryable | backoff retry |
| `415` | 查询失败 | Retryable | backoff retry |
| **`777`** | **频繁操作，请稍后重试** | **RateLimit/Retryable** | **backoff retry** |
| `906` | 系统异常 | Permanent | surface, no retry |
| `907` | 请求被禁止 | Permanent | surface, no retry |
| `908` | 未授权 | Permanent | surface, no retry |
| `999` | 服务内部错误 | Permanent | surface, no retry |
| `1114` | 服务不覆盖当前国家/地区 | Permanent | surface, no retry |
| `1145` | 地址或IP存在风险 | Permanent | surface, no retry |
| `1146` | XRP地址未激活代币 | ClientError | surface, no retry |

**Retryable set (API layer)**: `412`, `413`, `415`, `777`.
**Retryable set (transport layer, keep independently)**: `502`, `503`, connection error, timeout.
**No dedicated "amount below minimum" code** — deposit limit validation stays client-side (compare against `depositMin` from quote).
