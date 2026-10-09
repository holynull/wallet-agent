export function parseSseFrames(buffer: string): { frames: string[]; remainder: string } {
  const frames = buffer.split("\n\n");
  return { frames: frames.slice(0, -1), remainder: frames[frames.length - 1] || "" };
}

export function parseSseFrame(frame: string): { id: string; event: string; data: string } {
  const lines = frame.split("\n");
  return {
    id: lines.find((line) => line.startsWith("id:"))?.slice(3).trim() || "",
    event: lines.find((line) => line.startsWith("event:"))?.slice(6).trim() || "message",
    data: lines
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n"),
  };
}

export function formatProcessingDuration(milliseconds: number): string {
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return "0 ms";
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  return `${(milliseconds / 1000).toFixed(1)} s`;
}

export function apiErrorMessage(body: any, fallback = "请求失败，请稍后重试。"): string {
  const detail = body?.detail && typeof body.detail === "object" ? body.detail : body;
  const code = detail?.code;
  const messages: Record<string, string> = {
    CONFIRMATION_EXPIRED: "这份兑换报价已过期，请重新获取报价。",
    CONFIRMATION_NOT_PENDING: "当前兑换确认状态已变化，正在刷新状态，请稍后重试。",
    CONFIRMATION_RESUME_NOOP: "确认请求没有推进，兑换状态可能已更新，请刷新后重试。",
  };
  return messages[code] || detail?.message || body?.message || fallback;
}

export function latestProgressText(entries: Array<{ text?: string }>, fallback: string): string {
  return (entries.length ? entries[entries.length - 1]?.text : undefined) || fallback;
}

export type ReadOnlyTrace = {
  enabled: boolean;
  loopCount: number;
  toolCalls: Array<Record<string, any>>;
  facts: Record<string, any>;
  reference: Record<string, any> | null;
};

export type ModelTraceEntry = {
  call_id?: string;
  role?: string;
  model_id?: string;
  task_kind?: string;
  stage?: string;
  timestamp?: string;
  elapsed_ms?: number;
  tool_call_ids?: string[];
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  cached_tokens?: number;
  finish_reason?: string;
};

export function formatModelTraceEntry(item: ModelTraceEntry | Record<string, any>): string {
  const parts = [
    item.stage || item.role || "模型调用",
    item.model_id,
    item.call_id ? `call_id ${item.call_id}` : null,
    item.elapsed_ms != null ? `${item.elapsed_ms} ms` : null,
    item.prompt_tokens != null ? `${item.prompt_tokens} prompt` : null,
    item.completion_tokens != null ? `${item.completion_tokens} completion` : null,
    item.total_tokens != null ? `${item.total_tokens} total` : null,
    item.cached_tokens != null ? `${item.cached_tokens} cached` : null,
    item.finish_reason ? `finish:${item.finish_reason}` : null,
  ];
  return parts.filter(Boolean).join(" · ");
}

export type ReadOnlyTimelineItem = {
  kind: "model" | "tool" | "result";
  key: string;
  call_id?: string;
  name?: string;
  label?: string;
  model_id?: string;
  status?: string;
  args?: Record<string, any>;
  error?: any;
  elapsed_ms?: number;
  tool_call_ids?: string[];
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  cached_tokens?: number;
  finish_reason?: string;
};

function optionalField(target: Record<string, any>, key: string, value: any): void {
  if (value !== undefined && value !== null && value !== "") target[key] = value;
}

export function buildReadOnlyTimeline(
  modelTrace: Array<Record<string, any>> = [],
  toolCalls: Array<Record<string, any>> = [],
): ReadOnlyTimelineItem[] {
  const timeline: ReadOnlyTimelineItem[] = [];
  const calls = toolCalls.filter((item) => item && typeof item === "object");
  const callsById = new Map(calls.map((item, index) => [
    String(item.call_id || item.id || `anonymous-${index}`),
    item,
  ]));
  const consumed = new Set<Record<string, any>>();

  const appendTool = (call: Record<string, any>, index: number) => {
    if (consumed.has(call)) return;
    consumed.add(call);
    const callId = String(call.call_id || call.id || `anonymous-${index}`);
    const name = String(call.name || "只读工具");
    const tool: ReadOnlyTimelineItem = {
      kind: "tool",
      key: `tool:${callId}`,
      call_id: callId,
      name,
    };
    optionalField(tool, "args", call.args || call.arguments);
    optionalField(tool, "status", call.status || "处理中");
    optionalField(tool, "elapsed_ms", call.elapsed_ms);
    optionalField(tool, "error", call.error);
    timeline.push(tool);

    const result: ReadOnlyTimelineItem = {
      kind: "result",
      key: `result:${callId}`,
      call_id: callId,
      name,
    };
    optionalField(result, "status", call.status || "处理中");
    optionalField(result, "elapsed_ms", call.elapsed_ms);
    optionalField(result, "error", call.error);
    timeline.push(result);
  };

  for (const [index, item] of modelTrace.filter((entry) => entry && typeof entry === "object").entries()) {
    const model: ReadOnlyTimelineItem = {
      kind: "model",
      key: `model:${String(item.call_id || `${item.timestamp || "unknown"}-${index}`)}`,
      label: String(item.stage || item.role || "模型调用"),
    };
    optionalField(model, "call_id", item.call_id);
    optionalField(model, "model_id", item.model_id);
    optionalField(model, "elapsed_ms", item.elapsed_ms);
    optionalField(model, "prompt_tokens", item.prompt_tokens);
    optionalField(model, "completion_tokens", item.completion_tokens);
    optionalField(model, "total_tokens", item.total_tokens);
    optionalField(model, "cached_tokens", item.cached_tokens);
    optionalField(model, "finish_reason", item.finish_reason);
    const toolCallIds = Array.isArray(item.tool_call_ids)
      ? item.tool_call_ids.map((value: any) => String(value)).filter(Boolean)
      : [];
    if (toolCallIds.length) model.tool_call_ids = toolCallIds;
    timeline.push(model);
    toolCallIds.forEach((callId) => {
      const call = callsById.get(callId);
      if (call) appendTool(call, calls.indexOf(call));
    });
  }

  calls.forEach((call, index) => appendTool(call, index));
  return timeline;
}

export function extractReadOnlyTrace(state: any): ReadOnlyTrace | null {
  if (!state || typeof state !== "object") return null;
  const source = state.read_only && typeof state.read_only === "object" ? state.read_only : state;
  const toolCallsValue = source.read_tool_calls ?? source.tool_calls;
  const toolCalls = Array.isArray(toolCallsValue)
    ? toolCallsValue.filter((item: any) => item && typeof item === "object").map((item: any) => ({ ...item }))
    : [];
  const loopValue = source.read_loop_count ?? source.loop_count;
  const loopCount = Number.isFinite(Number(loopValue))
    ? Number(loopValue)
    : 0;
  const factsValue = source.read_facts ?? source.facts;
  const facts = factsValue && typeof factsValue === "object" ? { ...factsValue } : {};
  const referenceValue = source.read_reference ?? source.reference;
  const reference = referenceValue && typeof referenceValue === "object"
    ? { ...referenceValue }
    : null;
  const enabled = source.read_only_enabled === true || source.enabled === true || loopCount > 0 || toolCalls.length > 0 || reference !== null;
  return enabled ? { enabled, loopCount, toolCalls, facts, reference } : null;
}

export function responseMessage(response: any): string {
  if (!response || typeof response !== "object") return "";
  if (Array.isArray(response.errors)) {
    const errorText = response.errors.map((item: any) => item?.message || item?.code).filter(Boolean).join("\n");
    if (errorText) return errorText;
  }
  if (response.message) return String(response.message);
  if (response.kind === "unsupported") return "抱歉，我目前只能处理钱包余额、资产、价格、转账和兑换相关请求。";
  const messages: Record<string, string> = {
    pending: "交易已经提交，正在等待链上确认。",
    confirmed: "交易已确认，资产状态应该很快同步到钱包。",
    completed: "兑换已完成，可以到目标链查看到账资产。",
    failed: "交易执行失败或已回滚，请检查交易详情和余额。",
    timed_out: "暂时还没有等到 Provider 的最终结果，可以稍后再次查询。",
    broadcast_pending: "交易已被节点看到，正在等待链上确认。",
    not_propagated: "交易哈希暂时还没有在源链上出现，Provider 轮询尚未开始。",
    dropped_or_replaced: "交易可能已被丢弃或被同 nonce 的新交易替换。",
    unknown: "暂时无法确定交易状态，建议稍后重试查询。",
    quote_selection_required: "请选择一个兑换报价后继续。",
    confirmation_required: "请确认是否继续这笔兑换。",
    approval_required: "这笔兑换需要先授权 Token 额度，请在钱包中确认 Approve。",
    transfer_prepare: "转账交易已准备好，请在钱包中确认并广播。",
    swap_ready: "兑换交易已准备好，请在钱包中确认并广播。",
  };
  const status = response.provider_status || response.confirmation_status || response.stage || (typeof response.status === "object" ? response.status?.status || response.status?.state : response.status);
  return messages[status] || messages[response.kind] || (status ? `交易状态：${status}` : "");
}

export type ConversationArtifacts = {
  response: any;
  route?: { graph_role?: string; route_mode?: string; route_reason?: string };
  reactToolTimings?: Array<Record<string, any>>;
  model_trace?: ModelTraceEntry[];
  quotes?: any[];
  confirmation?: any;
  preflight?: any;
  approval?: any;
  pending_transaction?: any;
  pending_kind?: "swap" | "transfer";
  token_candidates?: any[];
  wallet?: any;
  portfolio?: any;
  price?: any;
  assetValue?: any;
  gas?: any;
  assets?: any;
  status?: any;
  broadcast_hash?: string;
  reactResults?: Array<{ tool?: string; result?: any }>;
  readOnly?: ReadOnlyTrace;
};

export const APP_NETWORKS = ["ETH", "BSC", "BASE", "POLYGON", "ARBITRUM", "OPTIMISM", "BTC", "TRON", "SOLANA"];
export type AppWalletConfig = { networks: string[]; evmAddress: string; btcAddresses: string; tronAddress: string; solanaAddress: string };
// Default SegWit / Taproot / nested SegWit BTC addresses plus a TRON address,
// used to exercise the multi-chain balance query in the demo. Public
// addresses only; the wallet extension does not expose BTC or TRON accounts.
export const DEFAULT_APP_WALLET: AppWalletConfig = { networks: [...APP_NETWORKS], evmAddress: "", btcAddresses: ["bc1qdqahz8x62rsyfmlkv0m5e5rc234zr7vyl690d5", "bc1p47u7v2k5cpfqt0gxftkat3ade7ucs588g8lr0znzr9le06taty6q6x9gut", "3FGALr2UNebKnBTErhePUk3CuzmDd9FJux"].join("\n"), tronAddress: "TNwXHkvCwj3nayWPbFbxD7rBPncBsyBS27", solanaAddress: "" };

export function buildWalletAppContext(config: AppWalletConfig, connectedAddress = "") {
  const supported_networks = [...new Set(config.networks.filter((chain) => APP_NETWORKS.includes(chain)))];
  const wallet_accounts: Array<{ chain: string; address: string }> = [];
  for (const chain of supported_networks) {
    const addresses = chain === "BTC" ? config.btcAddresses.split(/[\s,;]+/) : [
      chain === "TRON" ? config.tronAddress : chain === "SOLANA" ? config.solanaAddress : config.evmAddress.trim() || connectedAddress,
    ];
    for (const address of new Set(addresses.map((item) => item.trim()).filter(Boolean))) wallet_accounts.push({ chain, address });
  }
  return { supported_networks, wallet_accounts };
}

export function formatBalanceUsd(value: unknown): string {
  if (value === null || value === undefined || value === "") return "价格未知";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "价格未知";
  if (amount > 0 && amount < 0.01) return "<0.01";
  return amount.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function extractConversationArtifacts(state: any): ConversationArtifacts | null {
  if (!state || typeof state !== "object") return null;
  const response = state.response?.response || state.response || (state.kind ? state : null);
  if (!response || typeof response !== "object") return null;
  const kind = response.kind;
  const stage = response.stage || (!kind ? state.authorization_stage : undefined);
  const artifacts: ConversationArtifacts = { response };
  const routeValue = state.route || response.route;
  if (routeValue && typeof routeValue === "object") {
    artifacts.route = {
      graph_role: routeValue.graph_role,
      route_mode: routeValue.route_mode,
      route_reason: routeValue.route_reason,
    };
  } else if (state.route_mode || response.route_mode) {
    artifacts.route = {
      graph_role: state.graph_role,
      route_mode: state.route_mode || response.route_mode,
      route_reason: state.route_reason || response.route_reason,
    };
  }
  const reactToolTimings = state.react_tool_timings || response.react_tool_timings;
  if (Array.isArray(reactToolTimings) && reactToolTimings.length) {
    artifacts.reactToolTimings = reactToolTimings.map((item: any) => ({ ...item }));
  }
  const modelTrace = state.model_trace || state.artifacts?.model_trace;
  if (Array.isArray(modelTrace) && modelTrace.length) artifacts.model_trace = modelTrace;

  if (kind === "swap_quote") artifacts.quotes = response.quotes || state.quote_candidates || [];
  if (kind === "confirmation_required") artifacts.confirmation = response.confirmation || state.confirmation_state;
  if (kind === "approval_required" || stage === "approval_required") artifacts.approval = response.approval_transaction || state.approval_transaction;
  if (["transfer_prepare", "swap_prepare"].includes(kind) || stage === "swap_ready") {
    artifacts.preflight = response.preflight || state.preflight;
    artifacts.pending_transaction = response.pending_transaction || response.transaction || state.pending_transaction;
    artifacts.pending_kind = kind === "transfer_prepare" ? "transfer" : "swap";
  }
  if (kind === "clarification" && (response.token_candidates || state.token_candidates)?.length) {
    artifacts.token_candidates = response.token_candidates || state.token_candidates;
  }
  if (kind === "wallet_query") artifacts.wallet = response.wallet || response;
  if (kind === "portfolio_query") artifacts.portfolio = response.portfolio || response;
  if (response.portfolio) artifacts.portfolio = response.portfolio;
  if (kind === "price_query") artifacts.price = response;
  if (kind === "asset_value") artifacts.assetValue = response;
  if (kind === "gas_check") artifacts.gas = response;
  if (kind === "asset_discovery") artifacts.assets = response;
  if (kind === "swap_status" || kind === "transaction_status") artifacts.status = response;
  if (kind === "react" && Array.isArray(response.react_results) && response.react_results.length) {
    artifacts.reactResults = response.react_results;
    const portfolioResult = [...response.react_results].reverse().find((item: any) => item?.result?.portfolio);
    if (!artifacts.portfolio && portfolioResult) artifacts.portfolio = portfolioResult.result.portfolio;
    const quoteResults = response.react_results.filter((item: any) =>
      item?.tool === "get_swap_quote" && item?.result?.ok === true && Array.isArray(item.result.quotes),
    );
    const usableQuotes = quoteResults.flatMap((item: any) => item.result.quotes).filter((quote: any) => {
      const input = Number(quote?.input_amount);
      const output = Number(quote?.expected_output);
      return Boolean(quote?.provider_reference) && Number.isFinite(input) && input > 0 && Number.isFinite(output) && output > 0;
    });
    if (usableQuotes.length) {
      const unique = new Map<string, any>();
      usableQuotes.forEach((quote: any) => unique.set(String(quote.provider_reference), quote));
      artifacts.quotes = [...unique.values()];
    }
    const transferResult = [...response.react_results].reverse().find((item: any) =>
      item?.tool === "prepare_transfer" && item?.result?.ok === true && item?.result?.pending_transaction,
    );
    if (transferResult) {
      artifacts.preflight = transferResult.result.preflight;
      artifacts.pending_transaction = transferResult.result.pending_transaction;
      artifacts.pending_kind = "transfer";
    }
  }
  if (kind === "error" && (response.preflight || state.preflight)) artifacts.preflight = response.preflight || state.preflight;
  if (state.broadcast_tx_hash || response.tx_hash) artifacts.broadcast_hash = state.broadcast_tx_hash || response.tx_hash;
  const readOnly = extractReadOnlyTrace(state.read_only || state);
  if (readOnly) artifacts.readOnly = readOnly;

  return Object.keys(artifacts).length > 1 ? artifacts : null;
}

export function mergeModelTrace(
  ...sources: Array<Array<Record<string, any>> | undefined | null>
): Array<Record<string, any>> {
  const merged: Array<Record<string, any>> = [];
  const positions = new Map<string, number>();
  for (const source of sources) {
    for (const item of source || []) {
      if (!item || typeof item !== "object") continue;
      const key = String(item.call_id || `${item.timestamp || ""}:${item.role || ""}:${item.task_kind || ""}:${item.model_id || ""}`);
      const position = positions.get(key);
      if (position !== undefined) {
        merged[position] = { ...merged[position], ...item };
      } else {
        positions.set(key, merged.length);
        merged.push({ ...item });
      }
    }
  }
  return merged.sort((a, b) => String(a.timestamp || "").localeCompare(String(b.timestamp || "")));
}

export function restoreConversationMessages(messages: any[], latestArtifacts?: ConversationArtifacts | null): any[] {
  const restored = Array.isArray(messages) ? messages.map((item) => {
    if (!item?.artifacts) return { ...item };
    const artifacts = extractConversationArtifacts({ ...item.artifacts, response: item.artifacts.response });
    return artifacts ? { ...item, artifacts } : Object.fromEntries(Object.entries(item).filter(([key]) => key !== "artifacts"));
  }) : [];
  if (!latestArtifacts) return restored;
  for (let index = restored.length - 1; index >= 0; index -= 1) {
    if (restored[index]?.role === "assistant") {
      if (!restored[index].artifacts) restored[index].artifacts = latestArtifacts;
      break;
    }
  }
  return restored;
}

const chainIds: Record<string, number> = {
  ETH: 1, BSC: 56, POLYGON: 137, OPTIMISM: 10, BASE: 8453, ARBITRUM: 42161,
};

export function transactionChainId(transaction: any): number | string | undefined {
  if (transaction?.chain_id != null) return transaction.chain_id;
  const chain = String(transaction?.chain || "").toUpperCase();
  return chainIds[chain];
}

function providerText(entry: any): string {
  return [
    entry?.source,
    entry?.info?.name,
    entry?.info?.rdns,
    entry?.info?.uuid,
    entry?.provider?.name,
    entry?.provider?.providerName,
    entry?.provider?.walletName,
    entry?.provider?._name,
    entry?.provider?._isMetaMask,
    entry?.provider?._isCatWallet,
  ].filter(Boolean).join(" ").toLowerCase();
}

export function providerPriority(entry: any): number {
  const text = providerText(entry);
  const provider = entry?.provider || {};
  if (provider.isMetaMask === true || provider._isMetaMask === true || text.includes("metamask") || text.includes("io.metamask")) return 0;
  if (provider.isCatWallet === true || provider.isCatwallet === true || provider._isCatWallet === true || text.includes("catwallet") || text.includes("cat wallet")) return 1;
  if (entry?.source === "window.ethereum") return 2;
  if (entry?.source === "window.ethereum.providers") return 3;
  if (entry?.source === "eip6963") return 4;
  return 5;
}

export function formatResponseSummary(response: any): string {
  if (!response || typeof response !== "object") return "";
  if (response.kind === "swap_quote") {
    const count = response.quotes?.length || 0;
    let message = count ? `我找到了 ${count} 个兑换报价，请比较后选择一个 Provider。` : "暂时没有找到可用的兑换报价。";
    const failed = (response.provider_errors || []).map((item: any) => item?.details?.provider || "未知 Provider");
    if (failed.length) message += `另有 ${[...new Set(failed)].join(", ")} 报价失败，已跳过。`;
    return message;
  }
  if (response.kind === "wallet_query") {
    const wallet = response.wallet || response;
    const native = wallet.native_balance;
    return native
      ? `当前 ${wallet.chain || "钱包"} 的原生资产余额是 ${native.amount} ${native.asset?.symbol || ""}，Token 共 ${wallet.token_balances?.length || 0} 项。`
      : "我已查询到钱包信息，但暂时没有可展示的余额数据。";
  }
  if (response.kind === "portfolio_query") {
    const portfolio = response.portfolio || response;
    const legacyWallet = portfolio.wallet || {};
    const legacyAssets = [
      ...(legacyWallet.native_balance ? [legacyWallet.native_balance] : []),
      ...(legacyWallet.token_balances || []),
    ];
    const assets = portfolio.assets || legacyAssets;
    const chains = Array.isArray(portfolio.chains) && portfolio.chains.length
      ? portfolio.chains.join("、")
      : portfolio.chain || legacyWallet.chain || "当前网络";
    const total = portfolio.total_usd_value
      ?? portfolio.total_value?.total_value
      ?? "暂不可用";
    return `你在 ${chains} 上共有 ${portfolio.asset_count ?? assets.length} 项资产，USD 估值为 ${total}。`;
  }
  if (response.kind === "gas_check") {
    return response.sufficient
      ? `当前 ${response.chain || "网络"} 的 Gas 余额足够，预计手续费约 ${response.fee_estimate?.amount || "-"} ${response.fee_estimate?.asset?.symbol || ""}。`
      : `当前 Gas 余额不足，预计还缺少 ${response.shortfall_raw || "-"} 个最小单位。`;
  }
  if (response.kind === "asset_discovery") return `我找到了 ${response.assets?.length || 0} 个 Token 结果，详细合约地址和精度见下方。`;
  if (response.kind === "price_query") {
    const prices = response.prices || [];
    return prices.length
      ? `价格来源：${prices.map((item: any) => `${String(item.provider || "未知来源").toUpperCase()} ${item.asset?.symbol || ""} ${item.usd_price || "-"}`).join("；")}`
      : "暂时没有可用的价格数据。";
  }
  if (response.kind === "asset_value") {
    if (response.message) return String(response.message);
    const symbol = response.asset?.symbol || "资产";
    return response.usd_value != null
      ? `你的 ${symbol} 当前价值约为 $${response.usd_value}。`
      : `我查到你的 ${symbol} 余额，但暂时无法计算美元价值。`;
  }
  return responseMessage(response);
}

const blockedCodes = new Set([
  "CHAIN_CAPABILITY_UNAVAILABLE", "PRICE_PROVIDER_UNAVAILABLE", "ASSET_PROVIDER_UNAVAILABLE",
  "PROVIDER_UNAVAILABLE", "PROVIDER_QUOTE_FAILED", "PROVIDER_PREPARE_FAILED",
  "PROVIDER_REGISTER_FAILED", "PROVIDER_STATUS_FAILED", "WALLET_QUERY_FAILED",
  "PORTFOLIO_QUERY_FAILED", "GAS_CHECK_FAILED", "PRICE_QUERY_FAILED",
  "TRANSACTION_STATUS_FAILED", "ASSET_DISCOVERY_FAILED", "TRANSFER_PREPARE_FAILED",
]);

function collectCodes(value: any, result = new Set<string>()): Set<string> {
  if (!value || typeof value !== "object") return result;
  if (typeof value.code === "string") result.add(value.code);
  if (Array.isArray(value)) value.forEach((item) => collectCodes(item, result));
  else Object.values(value).forEach((item) => collectCodes(item, result));
  return result;
}

export function classifyAgentTest(spec: any, events: any[], error?: unknown) {
  const states = events.map((item) => item?.state).filter((item) => item && typeof item === "object");
  const codes = [...collectCodes([error, events])];
  const kinds = states.flatMap((item) => [item.kind, item.response?.kind]).filter(Boolean);
  const stages = states.flatMap((item) => [item.stage, item.response?.stage]).filter(Boolean);
  const hasQuote = states.some((item) => item.quote_candidates?.length || item.response?.quotes?.length);
  const hasPrices = states.some((item) => item.prices?.length || item.response?.prices?.length);
  const hasAssets = states.some((item) => item.assets?.length || item.response?.assets?.length);
  const expectedKind = spec.expectedKinds?.find((kind: string) => kinds.includes(kind));
  const expectedStage = spec.expectedStages?.find((stage: string) => stages.includes(stage));
  if (expectedKind && (!spec.requiresQuote || hasQuote) && (!spec.requiresPrices || hasPrices) && (!spec.requiresAssets || hasAssets)) {
    return { status: "pass", detail: `返回 ${expectedKind}${codes.length ? ` · ${codes.join(", ")}` : ""}`, codes };
  }
  if (expectedKind && spec.requiresPrices && !hasPrices && !codes.length) return { status: "blocked", detail: "到达价格查询分支，但未返回价格数据。", codes };
  if (expectedKind && spec.requiresAssets && !hasAssets && !codes.length) return { status: "blocked", detail: "到达资产发现分支，但未返回资产数据。", codes };
  if (expectedKind && spec.requiresQuote && !hasQuote && !codes.length) return { status: "blocked", detail: "到达兑换报价分支，但未返回候选报价。", codes };
  if (expectedStage) return { status: "pass", detail: `到达 ${expectedStage}`, codes };
  if (spec.acceptsPreflightFailure && codes.includes("TRANSACTION_PREFLIGHT_FAILED")) return { status: "pass", detail: "已到达签名前预检查边界，因当前余额或费用条件阻断", codes };
  const dependencyCodes = codes.filter((code) => blockedCodes.has(code) || code.endsWith("_UNAVAILABLE"));
  if (dependencyCodes.length) return { status: "blocked", detail: `依赖不可用：${dependencyCodes.join(", ")}`, codes };
  if (error) return { status: "fail", detail: error instanceof Error ? error.message : String(error), codes };
  return { status: "fail", detail: `未得到预期结果${codes.length ? ` · ${codes.join(", ")}` : ""}`, codes };
}

const sensitive = /^(authorization|api[-_]?key|access[-_]?token|client[-_]?secret|credential|mnemonic|password|passphrase|private[-_]?key|secret|seed[-_]?phrase|signature|signed[-_]?body|signer|token)$/i;

export function sanitizeDebug(value: any, key = ""): any {
  if (sensitive.test(String(key))) return "[redacted]";
  if (Array.isArray(value)) return value.map((item) => sanitizeDebug(item));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, sanitizeDebug(item, name)]));
  }
  return value;
}
