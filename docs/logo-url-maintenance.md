# Logo URL 维护与防失效约定

## 背景

Trust Wallet 的 logo URL 出现过两次线上失效，都是“URL 能拼出来，但 404”：

1. **EIP-55 大小写**：Trust Wallet 的 token 目录用 EIP-55 checksum 地址命名，
   存的小写地址拼出的 URL 404（commit `f995f2f`）。
2. **BSC 目录改名**：BSC 的 token 目录从 `blockchains/binance/assets` 迁移到
   `blockchains/smartchain/assets`，旧 slug 拼出的 URL 404（commit `ebe4e99`）。

## 当前防线

### 1. Provider logo 优先

资产 logo 优先使用数据源（OKX 等）返回的 `logo_url`；只有 Provider 没给时才走
Trust Wallet。

### 2. 只下发 Trust Wallet 已发布的 URL（防“猜 URL”）

不再手工拼接 `blockchains/<slug>/assets/<address>/logo.png`。改为读取
`src/wallet_agent/domain/trustwallet_token_logos.json`：

- 该文件由 `scripts/sync_trustwallet_logos.py` 从 Trust Wallet 各链的
  `tokenlist.json` 生成，只包含 Trust Wallet **实际发布**的 token。
- `asset_logo_url(chain, address)` 只返回注册表里存在的 URL；不存在的返回
  `None`，App 渲染占位图而不是坏图。
- 覆盖链：ETH / BSC / BASE / ARBITRUM / OPTIMISM / POLYGON / TRON / SOLANA /
  AVALANCHE。

### 3. 回归测试

`tests/domain/test_chains.py` 固定断言常见 token 的 logo URL：

- ETH USDC / DAI（EIP-55 checksum）
- BSC USDT（smartchain slug）
- TRON BUSD（base58 原样透传）
- 不在 Trust Wallet 列表里的地址返回 `None`，不产生 404 URL

### 4. 一键同步脚本

Trust Wallet 改名 / 新增 token 后，重新生成注册表：

```bash
PYTHONPATH=src .venv/bin/python scripts/sync_trustwallet_logos.py
# 生成 src/wallet_agent/domain/trustwallet_token_logos.json
# 检查 git diff 后提交
```

建议：

- Trust Wallet 结构变更后跑一次；
- 或挂到定时任务 / CI（网络任务，标 `integration`）。

## App 侧建议（兜底）

即使后端保证 URL 可访问，App 端仍建议：

1. `asset.logo_url` 为空 → 显示占位图（不要用默认链 logo 冒充代币）。
2. 图片加载失败（onError）→ 回退到占位图，并上报 URL 给后端，便于我们核对
   Trust Wallet 是否又改了结构。
3. 不要缓存坏图：失败后下一次请求仍尝试加载，避免 Trust Wallet 修复后客户端
   一直显示旧占位图。

## 变更流程（后端）

1. 先改/重跑 `scripts/sync_trustwallet_logos.py`，确认新 URL 真实可访问
   （`curl -L -o /dev/null -w "%{http_code}" <url>` 返回 200）。
2. 更新 `tests/domain/test_chains.py` 里的固定断言。
3. 跑全量 `pytest` / `ruff` / evals 后提交部署。
