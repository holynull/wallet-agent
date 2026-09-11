# Browser Wallet Chat Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 Demo 改造成支持浏览器钱包连接和自然语言兑换参数收集的聊天式 EVM App。

**Architecture:** FastAPI 在每个 turn 中保存公开 wallet context 和可复用 session_id；LangGraph 的 intent 节点从模型输出合并兑换草稿，字段不完整时返回 clarification，完整后复用现有 quote/allowance/unsigned transaction 图。原生 HTML Demo 使用 EIP-1193 provider 连接钱包、渲染 SSE 事件和业务卡片，并在用户确认后调用 `eth_sendTransaction`。

**Tech Stack:** Python 3.11, FastAPI, LangGraph, Pydantic, vanilla HTML/CSS/JavaScript, EIP-1193.

**Spec:** `docs/superpowers/specs/2026-09-10-browser-wallet-chat-design.md`

## Global Constraints

- 只支持 EVM 浏览器钱包连接；服务端不接收私钥、助记词、signer 或 wallet client。
- 报价必须完整返回，用户必须提交原样 `provider_reference`。
- approve receipt 和 allowance 未确认前不得生成最终 swap unsigned transaction。
- 维持现有 REST/SSE API 兼容性；没有 wallet context 的旧请求继续工作。
- 每项行为先写失败测试，再写最小实现；完成后运行 pytest、Ruff、compileall 和脚本静态检查。

### Task 1: Extend graph state and extraction

**Files:**
- Modify: `src/wallet_agent/graph/state.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `src/wallet_agent/main.py`
- Test: `tests/graph/test_graph_paths.py`

**Interfaces:**
- Add `swap_draft`, `missing_fields`, and `wallet_context` state fields.
- Intent model exposes optional draft keys and the graph returns `response.kind=clarification` with `missing_fields`.

- [ ] Write failing tests for incomplete and complete extracted swap drafts.
- [ ] Run the focused graph tests and confirm the new assertions fail.
- [ ] Add typed state fields and intent output fields.
- [ ] Merge model output with checkpoint draft and wallet context; construct `SwapQuoteRequest` only when required fields are present.
- [ ] Run focused graph tests and confirm they pass.

### Task 2: Forward browser wallet context through API

**Files:**
- Modify: `src/wallet_agent/api/app.py`
- Test: `tests/api/test_api_contract.py`

**Interfaces:**
- `TurnRequest.session_id: str | None` allows a follow-up turn to reuse a swap session.
- `TurnRequest.address` and `TurnRequest.chain` are projected into `wallet_context`.

- [ ] Write failing API tests for wallet context and session reuse.
- [ ] Run focused API tests and confirm failure.
- [ ] Add session ownership validation, swap-session detection, and graph input projection.
- [ ] Run focused API tests and confirm pass.

### Task 3: Replace form demo with chat and EIP-1193 adapter

**Files:**
- Modify: `demo/index.html`
- Modify: `docs/local-demo-debugging.md`
- Modify: `README.md`

**Interfaces:**
- `connectWallet()` obtains public address and chain ID only.
- `sendMessage()` sends `conversation_id`, `session_id`, `address`, and `chain`.
- `sendUnsignedTransaction()` checks chain ID and calls `eth_sendTransaction`.

- [ ] Add chat timeline, wallet connection status, composer, quote cards, approval card, and swap transaction card.
- [ ] Add account/chain change listeners and safe error messages.
- [ ] Wire approve and swap cards to existing API endpoints.
- [ ] Document browser-wallet prerequisites and safety boundary.

### Task 4: Full verification

**Files:**
- No new production files.

- [ ] Run `pytest -q`.
- [ ] Run `ruff check src tests`.
- [ ] Run `python3 -m compileall -q src`.
- [ ] Run `bash -n scripts/start_local.sh` and `git diff --check`.
- [ ] Inspect staged diff for secrets and generated database files.
