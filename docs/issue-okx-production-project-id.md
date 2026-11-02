# Issue：生产 OKX 鉴权失败导致资产/价格全部不可用

> **状态：待修复（下个版本跟进）**
>
> 记录日期：2026-11-02　环境：生产 `https://agent.catwallet.ai`
>
> 关联：Wallet Agent 群 Lif 反馈（iOS/Android 查资产为空）、speak 排查
> （`OKX_PROJECT_ID` 未替换）

## 1. 现象

- App（iOS/Android）查询「我有什么资产?」返回空资产、总价值 0，并提示
  「BTC、TRON、ETH、BSC… 这些链的余额接口都返回了 OKX 数据源错误」。
- 生产接口实测：

  | 接口 | 结果 |
  | --- | --- |
  | `GET /v1/wallet/{address}/portfolio?chain=ETH` | `502 {"code":"OKX_PROVIDER_ERROR","message":"Wallet data provider is unavailable."}`（BTC/BSC 等所有链一致） |
  | `GET /v1/prices/token?chain=ETH&symbol=ETH` | `502 PRICE_QUERY_FAILED` |
  | `GET /v1/assets?provider=bridgers&chain=ETH&search=USDT`（非 OKX） | `200` 正常 |

- 生产 `/health`、`/ready` 均正常。

## 2. 对照与证据

- **测试服（同一份代码）** `GET /v1/wallet/{address}/portfolio?chain=ETH` →
  `200`，返回真实资产。
- **生产**所有 OKX 依赖接口失败，非 OKX（bridgers）接口正常 →
  问题不在代码/服务，而在生产的 OKX 凭据/项目配置。
- speak 反馈：生产 `.env` 的 `OKX_PROJECT_ID` 没找到、没有替换（沿用旧值）。

## 3. 根因判断

OKX 四个凭据必须来自**同一个 OKX 项目**：
`OKX_API_KEY` / `OKX_SECRET_KEY` / `OKX_PASSPHRASE` / `OKX_PROJECT_ID`。

- `OKX_PROJECT_ID` 对应请求头 `OK-ACCESS-PROJECT`（`src/wallet_agent/okx/client.py`）。
- 沿用旧值 / 与新的 key-secret 不匹配 → 鉴权/项目校验失败 → 所有 OKX 调用失败。
- 后端目前把 OKX 原始错误统一归一化为 `OKX_PROVIDER_ERROR`，丢掉了原始 `code`；
  且 OKX 可能返回 **HTTP 200 + body `code != 0`**，日志看着“有正常调用”，实际鉴权失败。
- 还需同时排查：生产服务器**出口 IP 是否在 OKX 白名单**、**服务器时间是否同步**
  （OKX 签名对时间窗口敏感）。

## 4. 立即修复（运维配置，非代码）

1. 在 OKX Web3 开发者后台找到生产 API Key 所属项目的 **Project ID**
   （即 `OK-ACCESS-PROJECT`），替换 `.env` 的 `OKX_PROJECT_ID`。
2. 确认 `OKX_API_KEY` / `OKX_SECRET_KEY` / `OKX_PASSPHRASE` / `OKX_PROJECT_ID`
   四个都来自**同一个项目**。
3. 确认生产出口 IP 在 OKX IP 白名单内，且服务器时间同步（NTP/chrony）。
4. 重建 `wallet-agent` 后验证：

   ```bash
   curl "https://agent.catwallet.ai/v1/wallet/<address>/portfolio?chain=ETH"   # 期望 200 + assets
   curl "https://agent.catwallet.ai/v1/prices/token?chain=ETH&symbol=ETH"     # 期望 200
   ```

## 5. 代码改进（下个版本）

- 在 `OkxWalletError` / `OkxClientError` 边界保留原始 OKX code（例如
  `50113` 签名错误、`50111` key 无效、`50102` 时间戳错误），并在 REST 502 与
  `portfolio.network_status[].errors` 的 `details` 中下发 `okx_code`
  （不泄露 key/secret）。
- 可选：`/ready` 或 `/debug/metrics` 增加 OKX 鉴权健康检查，鉴权失败时明确标识。
- 补测试：OKX 返回非 0 `code` 时，`details.okx_code` 正确透传。

## 6. 关联信息

- 群消息 SSE complete：conversation `73d4baa0-fddc-49f4-8442-7b731ce18fa4`，
  run `05ffb19c-8d42-4603-acb2-abe29949782b`（`portfolio_snapshot` 所有链 OKX 错误）。
- speak 提问：「`OKX_PROJECT_ID=` 这个参数我没找到，没替换。有影响吗」
  —— 有影响，且大概率就是本问题根因。
- 生产部署文档 `docs/production-deployment.md` §3.1 已将 `OKX_PROJECT_ID`
  列入必须替换的 key。
