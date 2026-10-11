import assert from "node:assert/strict";
import test from "node:test";
test("App context isolates address families and deduplicates BTC addresses", () => {
  assert.deepEqual(buildWalletAppContext({ ...DEFAULT_APP_WALLET, networks: ["ETH", "BASE", "BTC", "TRON"], btcAddresses: "btc-a\nbtc-b btc-a", tronAddress: "tron-a" }, "0xevm"), {
    supported_networks: ["ETH", "BASE", "BTC", "TRON"],
    wallet_accounts: [{ chain: "ETH", address: "0xevm" }, { chain: "BASE", address: "0xevm" }, { chain: "BTC", address: "btc-a" }, { chain: "BTC", address: "btc-b" }, { chain: "TRON", address: "tron-a" }],
  });
  assert.deepEqual(buildWalletAppContext({ ...DEFAULT_APP_WALLET, networks: [] }, "0xevm"), { supported_networks: [], wallet_accounts: [] });
});

test("BTC accounts accept optional signing material per line", () => {
  const context = buildWalletAppContext(
    {
      ...DEFAULT_APP_WALLET,
      networks: ["BTC"],
      btcAddresses: "bc1qexample,02ab,84'/0'/0'/0/0,a1b2c3d4\n3FGAexample,,,\nbc1qexample,02ab",
    },
    ""
  );
  assert.deepEqual(context.wallet_accounts, [
    {
      chain: "BTC",
      address: "bc1qexample",
      pubkey: "02ab",
      derivation_path: "84'/0'/0'/0/0",
      master_fingerprint: "a1b2c3d4",
    },
    { chain: "BTC", address: "3FGAexample" },
  ]);
});

test("default demo wallet carries the three BTC addresses", () => {
  const btc = buildWalletAppContext(DEFAULT_APP_WALLET, "").wallet_accounts.filter((item) => item.chain === "BTC");
  assert.deepEqual(btc, [
    { chain: "BTC", address: "bc1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d5" },
    { chain: "BTC", address: "bc1p47u7v2k5cpfqt0gxftkat3ade7ucs588g8lr0znzr9le06taty6q6x9gut" },
    { chain: "BTC", address: "3FGALr2UNebKnBTErhePUk3CuzmDd9FJux" },
  ]);
});

test("default demo wallet carries the TRON address", () => {
  const tron = buildWalletAppContext(DEFAULT_APP_WALLET, "").wallet_accounts.filter((item) => item.chain === "TRON");
  assert.deepEqual(tron, [{ chain: "TRON", address: "TNwXHkvCwj3nayWPbFbxD7rBPncBsyBS27" }]);
});

test("nonzero dust remains visible and portfolio cards restore with chat history", () => {
  assert.equal(formatBalanceUsd("0.0000001"), "<0.01");
  assert.equal(formatBalanceUsd(null), "价格未知");
  const portfolio = { assets: [{ asset: { chain: "BTC", symbol: "BTC" }, amount: "0.001" }], complete: false };
  const response = { kind: "react", message: "部分余额", portfolio };
  const artifacts = extractConversationArtifacts({ response });
  const history = restoreConversationMessages([{ role: "assistant", content: "部分余额", artifacts }]);
  assert.deepEqual(history[0].artifacts.portfolio, portfolio);
});
import {
  classifyAgentTest,
  apiErrorMessage,
  buildReadOnlyTimeline,
  extractConversationArtifacts,
  extractReadOnlyTrace,
  formatResponseSummary,
  formatProcessingDuration,
  buildHistoryPath,
  clientActionLabel,
  mergeHistoryPage,
  sortHistoryRows,
  formatTokenAmount,
  historyAmountSign,
  historyDirectionLabel,
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
  buildWalletAppContext,
  DEFAULT_APP_WALLET,
  formatBalanceUsd,
  isBitcoinSigningTransaction,
  bitcoinSigningFields,
  formatSigningDecimal,
  satsToBtc,
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
        { tool: "get_swap_quote", result: { ok: true, quotes: [{ provider_reference: "q1", input_amount: "1", expected_output: "2" }] } },
      ],
    },
    model_trace: [{ role: "react_agent", model_id: "deepseek-chat" }],
  });

  assert.deepEqual(artifacts?.reactResults, [
    { tool: "price_lookup", result: { ok: true, usd_price: "1800" } },
    { tool: "get_swap_quote", result: { ok: true, quotes: [{ provider_reference: "q1", input_amount: "1", expected_output: "2" }] } },
  ]);
  assert.deepEqual(artifacts?.quotes, [{ provider_reference: "q1", input_amount: "1", expected_output: "2" }]);
  assert.equal(formatResponseSummary({ kind: "react", message: "ETH 当前价格约为 1800 美元。" }), "ETH 当前价格约为 1800 美元。");
});

test("projects the receive-address card from a successful ReAct tool result", () => {
  const artifacts = extractConversationArtifacts({
    response: {
      kind: "react",
      message: "你的收款地址：0x3f84… 👆",
      react_results: [
        {
          tool: "receive_address",
          result: {
            ok: true,
            kind: "receive_address",
            address: "0x3f84",
            groups: [{ group: "EVM", address: "0x3f84", chains: ["ETH", "BSC"] }],
            supported_chains: ["ETH", "BSC", "BTC"],
          },
        },
      ],
    },
  });

  assert.equal(artifacts?.receive?.address, "0x3f84");
  assert.deepEqual(artifacts?.receive?.supported_chains, ["ETH", "BSC", "BTC"]);
});

test("ignores an unsuccessful receive-address result", () => {
  const artifacts = extractConversationArtifacts({
    response: {
      kind: "react",
      message: "App 还没有提供任何公共地址。",
      react_results: [
        { tool: "receive_address", result: { ok: false, code: "RECEIVE_ADDRESS_UNAVAILABLE" } },
      ],
    },
  });

  assert.equal(artifacts?.receive, undefined);
});

test("formats raw history amounts with token decimals", () => {
  assert.equal(formatTokenAmount("120300000000000000", 18, "BNB"), "0.1203 BNB");
  assert.equal(formatTokenAmount("42020000", 8, "BTC"), "0.4202 BTC");
  assert.equal(formatTokenAmount("1000000000000000000", 18, "ETH"), "1 ETH");
  assert.equal(formatTokenAmount("", 18, "ETH"), "ETH");
  assert.equal(formatTokenAmount("120300000000000000", null, "BNB"), "120300000000000000 BNB");
});

test("labels history rows with direction and sign", () => {
  assert.equal(historyDirectionLabel("receive"), "接收来自");
  assert.equal(historyDirectionLabel("send"), "发送至");
  assert.equal(historyDirectionLabel("swap"), "兑换");
  assert.equal(historyDirectionLabel(undefined), "交易");
  assert.equal(historyAmountSign("receive"), "+");
  assert.equal(historyAmountSign("send"), "-");
  assert.equal(historyAmountSign("swap"), "");
});

test("projects the transaction-history card from a successful ReAct tool result", () => {
  const history = {
    ok: true,
    kind: "transaction_history",
    title: "最近交易记录",
    total: 1,
    summary: "1 笔转出",
    items: [
      {
        chain: "BSC",
        tx_hash: "0xaa",
        status: "confirmed",
        direction: "send",
        symbol: "BNB",
        decimals: 18,
        amount_raw: "120300000000000000",
        counterparty: "0x6356beba8025334f1b7250fa06fcb1e47ff46ed1",
      },
    ],
  };
  const artifacts = extractConversationArtifacts({
    response: { kind: "react", message: "最近 1 笔记录拉出来了 👆", react_results: [{ tool: "transaction_history", result: history }] },
  });

  assert.deepEqual(artifacts?.history, history);
});

test("ignores an unsuccessful transaction-history result", () => {
  const artifacts = extractConversationArtifacts({
    response: {
      kind: "react",
      message: "无法查询。",
      react_results: [{ tool: "transaction_history", result: { ok: false, code: "HISTORY_ADDRESS_UNAVAILABLE" } }],
    },
  });

  assert.equal(artifacts?.history, undefined);
});

test("quick-reply chips survive a reload with their type and value", () => {
  const suggestions = [
    { label: "使用当前钱包地址", message: "使用当前连接钱包地址作为收款地址。", type: "address", value: "0x3f84", data: { intent: "transfer", recipient: "0x3f84" } },
    { label: "转给联系人", message: "我想从联系人里选一个收款地址。", type: "action", value: "pick_contact", data: { intent: "transfer" } },
  ];
  const artifacts = extractConversationArtifacts({
    response: { kind: "clarification", message: "收款地址是？", missing_fields: ["transfer_recipient"], suggestions },
  });

  assert.deepEqual(artifacts?.suggestions, suggestions);

  const restored = restoreConversationMessages([
    { role: "assistant", content: "收款地址是？", artifacts },
  ]);
  assert.equal(restored[0].suggestions.length, 2);
  assert.equal(restored[0].suggestions[0].type, "address");
  assert.equal(restored[0].suggestions[1].value, "pick_contact");
});

test("a clarification without suggestions restores an empty chip row", () => {
  const artifacts = extractConversationArtifacts({
    response: { kind: "clarification", message: "请补充金额。", missing_fields: ["input_amount"] },
  });

  assert.equal(artifacts?.suggestions, undefined);
  const restored = restoreConversationMessages([{ role: "assistant", content: "请补充金额。", artifacts }]);
  assert.ok(!restored[0].suggestions || restored[0].suggestions.length === 0);
});

test("projects the client action the agent asked the App to open", () => {
  const client_action = { type: "open_contacts", reason: "transfer_recipient", slots: { chain: "BTC" } };
  const artifacts = extractConversationArtifacts({
    response: { kind: "react", message: "请从通讯录里选一个联系人 👇", client_action },
  });

  assert.deepEqual(artifacts?.clientAction, client_action);
  assert.equal(clientActionLabel("open_contacts"), "打开通讯录，选择一个转账联系人");
  assert.equal(clientActionLabel("scan_qr"), "打开扫一扫，读取收款码");
  assert.equal(clientActionLabel(undefined), "需要 App 弹出自己的界面");
});

test("a plain react answer carries no client action", () => {
  const artifacts = extractConversationArtifacts({
    response: { kind: "react", message: "你的余额是 1 ETH。" },
  });

  assert.equal(artifacts?.clientAction, undefined);
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

test("Bitcoin signing payload is detected by chain or PSBT format", () => {
  assert.equal(isBitcoinSigningTransaction({ chain: "BTC", to: "bc1p", data: "psbt" }), true);
  assert.equal(isBitcoinSigningTransaction({ chain: "bitcoin", display: {} }), true);
  assert.equal(isBitcoinSigningTransaction({ chain: "ETH", display: { format: "psbt" } }), true);
  assert.equal(isBitcoinSigningTransaction({ chain: "BASE", display: { format: "calldata" } }), false);
  assert.equal(isBitcoinSigningTransaction(null), false);
});

test("sats convert to a trimmed BTC amount", () => {
  assert.equal(satsToBtc("120000"), "0.0012");
  assert.equal(satsToBtc(100000000), "1");
  assert.equal(satsToBtc(1), "0.00000001");
  assert.equal(satsToBtc("nope"), "");
});

test("bitcoin signing fields expose the data an app must show before signing", () => {
  const transaction = {
    chain: "BTC",
    to: "bc1p47u...",
    value: "120000",
    data: "cHNidP8B...",
    provider: "bridgers",
    display: {
      action: "provider_deposit",
      format: "psbt",
      deposit_address: "bc1p47u...",
      fee_raw: "459",
      fee_rate_sat_vb: "3.0",
      change_raw: "79541",
      input_count: "1",
      provider_order_id: "order-btc",
      unsigned_tx_hex: "0200000001aa",
    },
  };
  const fields = bitcoinSigningFields(transaction);
  const byLabel = Object.fromEntries(fields.map((field) => [field.label, field]));
  assert.equal(byLabel["目标地址"].value, "bc1p47u...");
  assert.equal(byLabel["金额 (sats)"].value, "120000");
  assert.equal(byLabel["金额 (BTC)"].value, "0.0012");
  assert.equal(byLabel["手续费 (sats)"].value, "459");
  assert.equal(byLabel["找零 (sats)"].value, "79541");
  assert.equal(byLabel["订单号"].value, "order-btc");
  assert.equal(byLabel["PSBT (base64)"].value, "cHNidP8B...");
  assert.equal(byLabel["PSBT (base64)"].copy, true);
  assert.equal(byLabel["未签名交易 (hex)"].copy, true);
  assert.equal(byLabel["操作"].value, "兑换充值（转入 Provider 充值地址）");
});

test("bitcoin signing fields omit empty optional values and label plain transfers", () => {
  const fields = bitcoinSigningFields({ chain: "BTC", to: "bc1q", value: "5000", data: "psbt" });
  assert.equal(fields.find((field) => field.label === "操作").value, "BTC 转账");
  assert.equal(fields.some((field) => field.label === "手续费 (sats)"), false);
  assert.equal(fields.some((field) => field.label === "订单号"), false);
});

test("Bitcoin prepare responses get PSBT-specific wording", () => {
  const btc = { chain: "BTC", data: "cHNidP8", display: { format: "psbt" } };
  assert.equal(
    responseMessage({ kind: "transfer_prepare", pending_transaction: btc }),
    "Bitcoin 转账交易已准备好，请把 PSBT 导入比特币钱包签名并广播。",
  );
  assert.equal(
    responseMessage({ kind: "swap_prepare", transaction: btc }),
    "Bitcoin 兑换交易已准备好，请把 PSBT 导入比特币钱包签名并广播。",
  );
  assert.equal(responseMessage({ kind: "transfer_prepare", pending_transaction: { chain: "BASE" } }), "转账交易已准备好，请在钱包中确认并广播。");
  assert.equal(responseMessage({ kind: "swap_status", pending_transaction: btc }), "");
});

test("signing decimals are trimmed for display", () => {
  assert.equal(formatSigningDecimal("0.6970000000000001"), "0.697");
  assert.equal(formatSigningDecimal(3), "3");
  assert.equal(formatSigningDecimal(""), "");
  assert.equal(formatSigningDecimal(undefined), "");
  const fields = bitcoinSigningFields({ chain: "BTC", to: "bc1q", value: "5000", data: "psbt", display: { fee_rate_sat_vb: "0.6970000000000001" } });
  assert.equal(fields.find((field) => field.label === "费率 (sat/vB)").value, "0.697");
});


test("history list path carries user, search and cursor", () => {
  assert.equal(buildHistoryPath({ userId: "u1" }), "/v1/agent/conversations?user_id=u1");
  assert.equal(
    buildHistoryPath({ userId: "u1", limit: 20 }),
    "/v1/agent/conversations?user_id=u1&limit=20",
  );
  const path = buildHistoryPath({ userId: "u1", query: " 流水 ", cursor: "2026-10-10T06:15:51Z", limit: 20 });
  const params = new URLSearchParams(path.split("?")[1]);
  assert.equal(params.get("user_id"), "u1");
  assert.equal(params.get("q"), "流水");
  assert.equal(params.get("cursor"), "2026-10-10T06:15:51Z");
  assert.equal(params.get("limit"), "20");
  // An empty search must not be sent at all.
  assert.equal(new URLSearchParams(buildHistoryPath({ userId: "u1", query: "   " }).split("?")[1]).has("q"), false);
});

test("history pages append without duplicating rows", () => {
  const first = mergeHistoryPage([], {
    conversations: [{ conversation_id: "a" }, { conversation_id: "b" }],
    next_cursor: "cursor-1",
  });
  assert.deepEqual(first.conversations.map((item) => item.conversation_id), ["a", "b"]);
  assert.equal(first.nextCursor, "cursor-1");

  const second = mergeHistoryPage(first.conversations, {
    conversations: [{ conversation_id: "b" }, { conversation_id: "c" }],
    next_cursor: null,
  });
  assert.deepEqual(second.conversations.map((item) => item.conversation_id), ["a", "b", "c"]);
  assert.equal(second.nextCursor, null);
});

test("pinned rows float to the top of the local ordering", () => {
  const rows = [
    { conversation_id: "old", updated_at: "2026-10-01T00:00:00Z", pinned: false },
    { conversation_id: "new", updated_at: "2026-10-10T00:00:00Z", pinned: false },
    { conversation_id: "pinned-old", updated_at: "2026-09-01T00:00:00Z", pinned: true },
  ];
  assert.deepEqual(
    sortHistoryRows(rows).map((item) => item.conversation_id),
    ["pinned-old", "new", "old"],
  );
});
