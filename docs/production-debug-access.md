# 生产调试接口调用指南

生产 API：

```text
https://agent.catwallet.ai
```

## 前置条件

1. 生产运行版本必须包含 request audit 和 timeline 接口（`5179787` 或更新）。
2. 生产 `.env` 必须配置独立的 `ADMIN_TOKEN`。
3. 不要把 `ADMIN_TOKEN` 发到群聊、写进仓库或 App。

未配置 `ADMIN_TOKEN` 时：

```json
{
  "code": "DEBUG_ENDPOINTS_DISABLED",
  "message": "Debug endpoints require a configured ADMIN_TOKEN."
}
```

## 本地保存 ADMIN_TOKEN

macOS 建议使用仓库自带脚本保存到 Keychain：

```bash
scripts/keychain_secret.sh store catwallet-admin-token-prod
scripts/keychain_secret.sh check catwallet-admin-token-prod
```

`store` 使用隐藏输入，不会把 token 写进 shell history。

## 直接调用生产接口

```bash
scripts/keychain_secret.sh exec catwallet-admin-token-prod ADMIN_TOKEN -- \
  curl -fsS \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  "https://agent.catwallet.ai/debug/errors?status=409&limit=50"
```

注意：`exec` 会把 token 注入子进程环境。不要在命令中直接写 token。

## 通过 SSH 隧道调用

如果公网入口没有代理 `/debug/*`：

```bash
ssh -L 8000:127.0.0.1:8000 <prod-host>
```

另一个终端：

```bash
scripts/keychain_secret.sh exec catwallet-admin-token-prod ADMIN_TOKEN -- \
  curl -fsS \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  "http://127.0.0.1:8000/debug/errors?status=409&limit=50"
```

## 常用调用

### 最近错误

```bash
scripts/keychain_secret.sh exec catwallet-admin-token-prod ADMIN_TOKEN -- \
  curl -fsS \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  "https://agent.catwallet.ai/debug/errors?limit=100"
```

### 按接口过滤

```bash
scripts/keychain_secret.sh exec catwallet-admin-token-prod ADMIN_TOKEN -- \
  curl -fsS \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  "https://agent.catwallet.ai/debug/requests?path=/v1/swap&limit=50"
```

### 按 session 查询

```bash
scripts/keychain_secret.sh exec catwallet-admin-token-prod ADMIN_TOKEN -- \
  curl -fsS \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  "https://agent.catwallet.ai/debug/requests?session_id=<session-id>&limit=50"
```

### Swap 生命周期时间线

```bash
scripts/keychain_secret.sh exec catwallet-admin-token-prod ADMIN_TOKEN -- \
  curl -fsS \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  "https://agent.catwallet.ai/v1/swap/<session-id>/timeline"
```

### 对话全部 Run

```bash
scripts/keychain_secret.sh exec catwallet-admin-token-prod ADMIN_TOKEN -- \
  curl -fsS \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  "https://agent.catwallet.ai/v1/agent/conversations/<conversation-id>/runs?limit=20"
```

### 单个 Run 详情

```bash
scripts/keychain_secret.sh exec catwallet-admin-token-prod ADMIN_TOKEN -- \
  curl -fsS \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  "https://agent.catwallet.ai/v1/agent/runs/<run-id>?event_limit=200"
```

## 返回字段

请求审计包含：

```json
{
  "request_id": "...",
  "timestamp": "...",
  "method": "POST",
  "path": "/v1/swap/.../approve-broadcast",
  "status": 409,
  "error_code": "APPROVAL_HASH_CONFLICT",
  "message": "...",
  "session_id": "...",
  "run_id": "...",
  "user_id": "...",
  "signature_status": "valid",
  "signature_key_id": null,
  "app_version": "1.6.9",
  "platform": "ios",
  "duration_ms": 12.3
}
```

不会记录 Authorization、token、私钥、签名材料或完整敏感请求体。

## 安全要求

- 只通过 HTTPS 或 SSH 隧道调用。
- 不把 `/debug/*` 暴露给普通 App 用户。
- 不在日志、截图或群聊中输出 `ADMIN_TOKEN`。
- token 泄露后立即轮换：
  ```bash
  scripts/keychain_secret.sh delete catwallet-admin-token-prod
  scripts/keychain_secret.sh store catwallet-admin-token-prod
  ```
