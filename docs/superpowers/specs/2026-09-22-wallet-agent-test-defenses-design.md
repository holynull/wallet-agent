# Wallet Agent 外部接口与对话测试防线设计

## 目标

在不依赖 Demo 手工操作的前提下，尽早发现 OKX、Bridgers、OmniBridge 响应结构变化，以及 Agent 多轮状态、SSE 过程和最终回答不一致的问题。

## 背景与问题

当前项目已有适配器单测、Graph 单测、API 测试和浏览器测试，但外部接口测试主要依赖人工提供响应或少量理想化 fake。最近 OKX 余额接口返回了没有 `decimals` 的真实 `tokenAssets`，导致余额查询只在 Demo 中才暴露问题。类似问题还可能出现在空结果、`null` 列表、分页字段、金额精度、Provider 降级和多轮确认状态中。

## 设计原则

1. 默认测试必须离线、可重复，不依赖 API Key、钱包地址或外部网络。
2. 真实响应只以脱敏 fixture 形式进入测试；不得保存凭据、原始地址、完整交易数据或用户隐私。
3. 缺失或不确定的金额精度不得静默默认成 0；适配器必须补齐可信元数据，或返回稳定错误。
4. 测试同时验证数据契约、领域模型、Graph 状态和用户可见 response，不能只验证 HTTP 200。
5. 真实接口巡检是可选层，失败时生成脱敏诊断，不阻塞普通离线 CI。

## 分层方案

### 1. 外部响应契约回放

为 OKX wallet、OKX token metadata、OKX price、OKX explorer、Bridgers 和 OmniBridge 建立脱敏 fixture。每个接口至少覆盖：

- 正常数据；
- 空数组、`null`、空对象；
- 字段缺失、字段别名和类型变化；
- 分页和游标；
- 数字非有限、raw 与 human 金额不一致；
- Provider 应用错误、HTTP 错误和超时。

契约测试通过 fake transport 或 fake client 回放 fixture，并断言适配器输出领域模型或稳定错误码。

### 2. 数据不变量检查

集中提供测试辅助函数，检查：

- 代币地址、链和 symbol 映射有效；
- decimals 在 0–255 范围内；
- `amount * 10**decimals == amount_raw` 时才允许精确换算；
- 未确认的精度、地址或金额不能被静默补默认值；
- 成功结果保留正确的 provider/source 标记；
- `null` 空结果按接口契约处理，不被误判成 malformed，也不被误判成有数据。

### 3. Agent 多轮场景回放

新增离线场景表，覆盖余额、资产组合、价格、转账、报价、准备、确认、广播和状态查询。每个场景记录用户输入、fake provider 事件和预期状态，断言：

- route、intent、task stage 一致；
- response 与 conversation history 都有最终回答；
- confirmation approved 后不会再次返回未确认；
- Provider 部分失败时仍能使用成功报价；
- SSE 中间事件不会丢失最后一个可见回答；
- 重复状态询问不会重复追加同一条 assistant 消息。

### 4. 脱敏 fixture 工具

提供一次性脱敏工具或测试 helper，将真实响应转换为只含字段名、类型、必要数值和占位地址的 fixture。日志和失败报告只输出 endpoint、顶层 keys、类型、列表长度、错误码和耗时，禁止输出 API 签名、密钥、原始地址或完整 payload。

### 5. 可选真实接口巡检

新增 `integration` 标记下的 OKX/Provider smoke 测试，仅在对应环境变量完整时运行。巡检验证真实响应能被当前适配器解析，并将失败转换为脱敏报告；默认单测和 CI 不调用外部服务。

## 文件边界

- `tests/contracts/`：外部接口 fixture 与契约测试。
- `tests/evals/`：多轮 Agent 场景回放与状态断言。
- `tests/helpers/`：脱敏、响应形状和金额不变量辅助函数。
- `tests/integration/`：可选真实接口巡检。
- `docs/local-demo-debugging.md`：补充从 fixture/巡检失败回到 Demo 调试数据的排查路径。
- 现有 `src/wallet_agent/okx`、`src/wallet_agent/providers` 和 Graph 代码只在测试暴露出真实契约缺口时做最小修复。

## 验收标准

1. 不提供任何外部凭据时，离线契约和场景测试可重复通过。
2. 用本次真实 OKX 余额响应 fixture 可以在没有网络的情况下复现并捕获缺失 decimals 问题。
3. 至少覆盖一组 OKX、Bridgers、OmniBridge 的正常、空结果、malformed 和 provider error 响应。
4. 至少覆盖一组“确认后查询状态”和“Provider 部分失败”的多轮场景。
5. 集成巡检没有凭据时明确 skip，而不是失败或泄露配置。
6. `ruff`、`git diff --check`、非浏览器测试和浏览器测试保持通过。
