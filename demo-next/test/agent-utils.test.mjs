import assert from "node:assert/strict";
import test from "node:test";
import {
  classifyAgentTest,
  apiErrorMessage,
  buildReadOnlyTimeline,
  extractConversationArtifacts,
  extractReadOnlyTrace,
  formatResponseSummary,
  formatProcessingDuration,
  formatModelTraceEntry,
  latestProgressText,
  mergeModelTrace,
  parseSseFrame,
  parseSseFrames,
  providerPriority,
  responseMessage,
  restoreConversationMessages,
  sanitizeDebug,
  transactionChainId,
} from "../lib/agent-utils.ts";

test("maps confirmation API errors to distinct user-facing messages", () => {
  assert.equal(apiErrorMessage({ detail: { code: "CONFIRMATION_EXPIRED", message: "expired" } }), "这份兑换报价已过期，请重新获取报价。");
  assert.equal(apiErrorMessage({ detail: { code: "CONFIRMATION_NOT_PENDING", message: "changed" } }), "当前兑换确认状态已变化，正在刷新状态，请稍后重试。");
  assert.equal(apiErrorMessage({ detail: { code: "CONFIRMATION_RESUME_NOOP", message: "noop" } }), "确认请求没有推进，兑换状态可能已更新，请刷新后重试。");
  assert.equal(apiErrorMessage({ detail: { code: "OTHER", message: "其他错误" } }), "其他错误");
});

test("formats processing duration for an active request", () => {
  assert.equal(formatProcessingDuration(0), "0 ms");
  assert.equal(formatProcessingDuration(438), "438 ms");
  assert.equal(formatProcessingDuration(1542), "1.5 s");
});

test("formats model usage and cache metadata without showing missing fields", () => {
  assert.equal(
    formatModelTraceEntry({
      stage: "只读工具调用",
      model_id: "deepseek-flash",
      call_id: "read_agent-1",
      elapsed_ms: 23,
      prompt_tokens: 120,
      completion_tokens: 9,
      cached_tokens: 80,
      finish_reason: "tool_calls",
    }),
    "只读工具调用 · deepseek-flash · call_id read_agent-1 · 23 ms · 120 prompt · 9 completion · 80 cached · finish:tool_calls",
  );
  assert.equal(formatModelTraceEntry({ model_id: "deepseek-flash" }), "模型调用 · deepseek-flash");
});

test("merges later model usage metadata for an existing call", () => {
  assert.deepEqual(mergeModelTrace(
    [{ call_id: "read_agent-1", model_id: "deepseek-flash", elapsed_ms: 20 }],
    [{ call_id: "read_agent-1", prompt_tokens: 100, cached_tokens: 80, finish_reason: "stop" }],
  ), [{
    call_id: "read_agent-1",
    model_id: "deepseek-flash",
    elapsed_ms: 20,
    prompt_tokens: 100,
    cached_tokens: 80,
    finish_reason: "stop",
  }]);
});

test("uses the last visible progress entry as the current stage", () => {
  assert.equal(latestProgressText([], "等待请求"), "等待请求");
  assert.equal(
    latestProgressText([{ text: "请求类型识别完成。" }, { text: "Bridgers 报价请求失败。" }], "等待请求"),
    "Bridgers 报价请求失败。",
  );
});

test("reads the last progress entry without requiring Array.prototype.at", () => {
  const legacyArrayLike = { 0: { text: "旧运行时" }, length: 1 };
  assert.equal(latestProgressText(legacyArrayLike, "等待请求"), "旧运行时");
});

test("extracts read-only tool trace from a streamed graph state", () => {
  assert.deepEqual(extractReadOnlyTrace({
    read_only_enabled: true,
    read_loop_count: 1,
    read_tool_calls: [{ name: "price_lookup", status: "completed", elapsed_ms: 12.4 }],
    read_facts: { price_lookup: { ok: true, prices: [{ usd_price: "1800" }] } },
    read_reference: { kind: "asset", id: "ETH:ETH", facts: { symbol: "ETH" } },
  }), {
    enabled: true,
    loopCount: 1,
    toolCalls: [{ name: "price_lookup", status: "completed", elapsed_ms: 12.4 }],
    facts: { price_lookup: { ok: true, prices: [{ usd_price: "1800" }] } },
    reference: { kind: "asset", id: "ETH:ETH", facts: { symbol: "ETH" } },
  });
});

test("builds a native tool-calling timeline with model, tool, and ToolMessage entries", () => {
  assert.deepEqual(buildReadOnlyTimeline([
    {
      call_id: "read_agent-2",
      role: "read_agent",
      model_id: "deepseek-flash",
      stage: "只读工具调用",
      elapsed_ms: 23,
      tool_call_ids: ["call-price-1"],
    },
  ], [
    {
      call_id: "call-price-1",
      name: "price_lookup",
      args: { symbol: "ETH", chain: "BASE" },
      status: "completed",
      elapsed_ms: 31,
    },
  ]), [
    {
      kind: "model",
      key: "model:read_agent-2",
      call_id: "read_agent-2",
      label: "只读工具调用",
      model_id: "deepseek-flash",
      elapsed_ms: 23,
      tool_call_ids: ["call-price-1"],
    },
    {
      kind: "tool",
      key: "tool:call-price-1",
      call_id: "call-price-1",
      name: "price_lookup",
      args: { symbol: "ETH", chain: "BASE" },
      status: "completed",
      elapsed_ms: 31,
    },
    {
      kind: "result",
      key: "result:call-price-1",
      call_id: "call-price-1",
      name: "price_lookup",
      status: "completed",
      elapsed_ms: 31,
    },
  ]);
});

test("restores read-only trace from persisted conversation artifacts", () => {
  assert.deepEqual(extractReadOnlyTrace({
    read_only: {
      enabled: true,
      loop_count: 1,
      tool_calls: [{ name: "price_lookup", status: "completed" }],
      facts: { price_lookup: { ok: true } },
      reference: { kind: "asset", id: "ETH:ETH" },
    },
  }), {
    enabled: true,
    loopCount: 1,
    toolCalls: [{ name: "price_lookup", status: "completed" }],
    facts: { price_lookup: { ok: true } },
    reference: { kind: "asset", id: "ETH:ETH" },
  });
});

test("parses complete SSE frames and preserves partial remainder", () => {
  assert.deepEqual(parseSseFrames("event: update\ndata: {}\n\nevent: complete\ndata:"), {
    frames: ["event: update\ndata: {}"],
    remainder: "event: complete\ndata:",
  });
});

test("parses SSE event IDs for resumable streams", () => {
  assert.deepEqual(parseSseFrame("id: 173-0\nevent: complete\ndata: {\"ok\":true}"), {
    id: "173-0",
    event: "complete",
    data: '{"ok":true}',
  });
});

test("maps status responses to user-facing text", () => {
  assert.equal(responseMessage({ status: { status: "pending" } }), "交易已经提交，正在等待链上确认。");
  assert.equal(responseMessage({ message: "custom" }), "custom");
  assert.equal(responseMessage({ kind: "error", errors: [{ code: "BAD_REQUEST", message: "参数不完整" }] }), "参数不完整");
  assert.equal(responseMessage({ kind: "clarification", errors: [{ code: "MISSING_PRICE_PARAMETERS", message: "请补充价格查询参数" }] }), "请补充价格查询参数");
  assert.equal(responseMessage({ kind: "swap_status", provider_status: "not_propagated" }), "交易哈希暂时还没有在源链上出现，Provider 轮询尚未开始。");
  assert.equal(responseMessage({ kind: "confirmation_required" }), "请确认是否继续这笔兑换。");
  assert.equal(responseMessage({ kind: "transfer_prepare" }), "转账交易已准备好，请在钱包中确认并广播。");
  assert.match(responseMessage({ kind: "unsupported" }), /钱包/);
});

test("extracts only cards belonging to the current response", () => {
  const historicalState = {
    quote_candidates: [{ provider_reference: "old-quote" }],
    approval_transaction: { to: "0xapprove" },
    pending_transaction: { to: "0xswap" },
    status_snapshot: { status: "pending" },
    response: { kind: "swap_status", status: { status: "pending" } },
  };

  assert.deepEqual(extractConversationArtifacts(historicalState), {
    response: { kind: "swap_status", status: { status: "pending" } },
    status: { kind: "swap_status", status: { status: "pending" } },
  });
  assert.equal(extractConversationArtifacts({
    authorization_stage: "swap_ready",
    pending_transaction: { to: "0xstale" },
    response: { kind: "clarification", errors: [{ code: "MISSING_PRICE_PARAMETERS" }] },
  }), null);
});

test("restores generic ReAct tool results without guessing a business card kind", () => {
  const artifacts = extractConversationArtifacts({
    response: {
      kind: "react",
      message: "ETH 当前价格约为 1800 美元。",
      react_results: [
        { tool: "price_lookup", result: { ok: true, usd_price: "1800" } },
        { tool: "get_swap_quote", result: { ok: true, quotes: [{ provider_reference: "q1" }] } },
      ],
    },
    model_trace: [{ role: "react_agent", model_id: "deepseek-chat" }],
  });

  assert.deepEqual(artifacts?.reactResults, [
    { tool: "price_lookup", result: { ok: true, usd_price: "1800" } },
    { tool: "get_swap_quote", result: { ok: true, quotes: [{ provider_reference: "q1" }] } },
  ]);
  assert.deepEqual(artifacts?.quotes, [{ provider_reference: "q1" }]);
  assert.equal(formatResponseSummary({ kind: "react", message: "ETH 当前价格约为 1800 美元。" }), "ETH 当前价格约为 1800 美元。");
});

test("restores ReAct route and tool timing metadata", () => {
  const artifacts = extractConversationArtifacts({
    response: { kind: "react", message: "已查询。" },
    graph_role: "conversation",
    route_mode: "react",
    route_reason: "model_answer",
    react_tool_timings: [
      { call_id: "price-1", tool: "price_lookup", elapsed_ms: 12, status: "success" },
    ],
  });

  assert.deepEqual(artifacts?.route, {
    graph_role: "conversation",
    route_mode: "react",
    route_reason: "model_answer",
  });
  assert.deepEqual(artifacts?.reactToolTimings, [
    { call_id: "price-1", tool: "price_lookup", elapsed_ms: 12, status: "success" },
  ]);
});

test("restores latest legacy card without moving persisted cards", () => {
  const persisted = { response: { kind: "swap_quote", quotes: [{ provider_reference: "q1" }] }, quotes: [{ provider_reference: "q1" }] };
  const latest = { response: { kind: "swap_status", status: { status: "pending" } }, status: { kind: "swap_status", status: { status: "pending" } } };
  const restored = restoreConversationMessages([
    { role: "assistant", content: "报价", artifacts: persisted },
    { role: "user", content: "到账了吗" },
    { role: "assistant", content: "还在确认" },
  ], latest);

  assert.deepEqual(restored[0].artifacts, persisted);
  assert.deepEqual(restored[2].artifacts, latest);
});

test("drops stale cards from an older persisted message", () => {
  const restored = restoreConversationMessages([{
    role: "assistant",
    content: "请补充参数",
    artifacts: {
      response: { kind: "clarification", errors: [{ code: "MISSING_PRICE_PARAMETERS" }] },
      preflight: { ok: true },
      pending_transaction: { to: "0xstale" },
    },
  }]);

  assert.equal(restored[0].artifacts, undefined);
});

test("derives transaction chain id from a named chain when chain_id is absent", () => {
  assert.equal(transactionChainId({ chain: "BASE" }), 8453);
  assert.equal(transactionChainId({ chain: "BSC", chain_id: 56 }), 56);
});

test("prioritizes MetaMask over CatWallet and generic injected providers", () => {
  const metamask = { info: { name: "MetaMask", rdns: "io.metamask" }, provider: { isMetaMask: true }, source: "eip6963" };
  const catwallet = { info: { name: "CatWallet" }, provider: { isCatWallet: true }, source: "window.catWallet" };
  const generic = { provider: {}, source: "window.ethereum" };
  assert.ok(providerPriority(metamask) < providerPriority(catwallet));
  assert.ok(providerPriority(metamask) < providerPriority(generic));
  assert.ok(providerPriority({ provider: { _isMetaMask: true }, source: "window.ethereum" }) < providerPriority(catwallet));
});

test("formats readable response summaries for wallet and gas results", () => {
  assert.equal(formatResponseSummary({ kind: "wallet_query", wallet: { chain: "BASE", native_balance: { amount: "1.2", asset: { symbol: "ETH" } }, token_balances: [{}, {}] } }), "当前 BASE 的原生资产余额是 1.2 ETH，Token 共 2 项。");
  assert.equal(formatResponseSummary({ kind: "gas_check", sufficient: false, shortfall_raw: "42" }), "当前 Gas 余额不足，预计还缺少 42 个最小单位。");
});

test("formats a normalized multi-chain portfolio without losing its asset count", () => {
  assert.equal(formatResponseSummary({
    kind: "portfolio_query",
    chains: ["ETH", "BASE"],
    asset_count: 2,
    assets: [{}, {}],
    total_usd_value: "305",
  }), "你在 ETH、BASE 上共有 2 项资产，USD 估值为 305。");
});

test("counts legacy portfolio native and token balances", () => {
  assert.equal(formatResponseSummary({
    kind: "portfolio_query",
    wallet: {
      chain: "BSC",
      native_balance: { amount: "0.1", asset: { symbol: "BNB" } },
      token_balances: [{ amount: "2", asset: { symbol: "USDC" } }],
    },
    total_value: { total_value: "80" },
  }), "你在 BSC 上共有 2 项资产，USD 估值为 80。");
});

test("summarizes partial quote-provider failures without changing response kind", () => {
  assert.equal(
    formatResponseSummary({
      kind: "swap_quote",
      quotes: [{ provider: "omnibridge" }],
      provider_errors: [{ details: { provider: "bridgers" } }],
    }),
    "我找到了 1 个兑换报价，请比较后选择一个 Provider。另有 bridgers 报价失败，已跳过。",
  );
});

test("redacts credentials recursively", () => {
  assert.deepEqual(sanitizeDebug({ token: "secret", nested: { private_key: "hidden" }, ok: 1 }), {
    token: "[redacted]",
    nested: { private_key: "[redacted]" },
    ok: 1,
  });
});

test("classifies agent results by expected response and dependency errors", () => {
  assert.equal(classifyAgentTest({ expectedKinds: ["price_query"] }, [
    { state: { response: { kind: "price_query", prices: [{ usd_price: "1" }] } } },
  ]).status, "pass");
  assert.equal(classifyAgentTest({ expectedKinds: ["price_query"] }, [
    { state: { response: { kind: "error", errors: [{ code: "OKX_CAPABILITY_UNAVAILABLE" }] } } },
  ]).status, "blocked");
});
