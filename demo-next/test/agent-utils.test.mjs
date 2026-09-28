import assert from "node:assert/strict";
import test from "node:test";
import {
  classifyAgentTest,
  extractConversationArtifacts,
  formatResponseSummary,
  formatProcessingDuration,
  latestProgressText,
  parseSseFrames,
  providerPriority,
  responseMessage,
  restoreConversationMessages,
  sanitizeDebug,
  transactionChainId,
} from "../lib/agent-utils.ts";

test("formats processing duration for an active request", () => {
  assert.equal(formatProcessingDuration(0), "0 ms");
  assert.equal(formatProcessingDuration(438), "438 ms");
  assert.equal(formatProcessingDuration(1542), "1.5 s");
});

test("uses the last visible progress entry as the current stage", () => {
  assert.equal(latestProgressText([], "等待请求"), "等待请求");
  assert.equal(
    latestProgressText([{ text: "请求类型识别完成。" }, { text: "Bridgers 报价请求失败。" }], "等待请求"),
    "Bridgers 报价请求失败。",
  );
});

test("parses complete SSE frames and preserves partial remainder", () => {
  assert.deepEqual(parseSseFrames("event: update\ndata: {}\n\nevent: complete\ndata:"), {
    frames: ["event: update\ndata: {}"],
    remainder: "event: complete\ndata:",
  });
});

test("maps status responses to user-facing text", () => {
  assert.equal(responseMessage({ status: { status: "pending" } }), "交易已经提交，正在等待链上确认。");
  assert.equal(responseMessage({ message: "custom" }), "custom");
  assert.equal(responseMessage({ kind: "error", errors: [{ code: "BAD_REQUEST", message: "参数不完整" }] }), "参数不完整");
  assert.equal(responseMessage({ kind: "clarification", errors: [{ code: "MISSING_PRICE_PARAMETERS", message: "请补充价格查询参数" }] }), "请补充价格查询参数");
  assert.equal(responseMessage({ kind: "swap_status", provider_status: "not_propagated" }), "交易哈希暂时还没有在源链上出现，Provider 轮询尚未开始。");
  assert.equal(responseMessage({ kind: "confirmation_required" }), "请确认是否继续这笔兑换。");
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
