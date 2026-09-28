export function parseSseFrames(buffer: string): { frames: string[]; remainder: string } {
  const frames = buffer.split("\n\n");
  return { frames: frames.slice(0, -1), remainder: frames.at(-1) || "" };
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

export function latestProgressText(entries: Array<{ text?: string }>, fallback: string): string {
  return entries.at(-1)?.text || fallback;
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
    swap_ready: "兑换交易已准备好，请在钱包中确认并广播。",
  };
  const status = response.provider_status || response.confirmation_status || response.stage || (typeof response.status === "object" ? response.status?.status || response.status?.state : response.status);
  return messages[status] || messages[response.kind] || (status ? `交易状态：${status}` : "");
}

export type ConversationArtifacts = {
  response: any;
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
  gas?: any;
  assets?: any;
  status?: any;
  broadcast_hash?: string;
};

export function extractConversationArtifacts(state: any): ConversationArtifacts | null {
  if (!state || typeof state !== "object") return null;
  const response = state.response?.response || state.response || (state.kind ? state : null);
  if (!response || typeof response !== "object") return null;
  const kind = response.kind;
  const stage = response.stage || (!kind ? state.authorization_stage : undefined);
  const artifacts: ConversationArtifacts = { response };

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
  if (kind === "price_query") artifacts.price = response;
  if (kind === "gas_check") artifacts.gas = response;
  if (kind === "asset_discovery") artifacts.assets = response;
  if (kind === "swap_status" || kind === "transaction_status") artifacts.status = response;
  if (kind === "error" && (response.preflight || state.preflight)) artifacts.preflight = response.preflight || state.preflight;
  if (state.broadcast_tx_hash || response.tx_hash) artifacts.broadcast_hash = state.broadcast_tx_hash || response.tx_hash;

  return Object.keys(artifacts).length > 1 ? artifacts : null;
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
    return `你在 ${portfolio.chain || "当前网络"} 上共有 ${portfolio.assets?.length || 0} 项资产，USD 估值为 ${portfolio.total_usd_value ?? "暂不可用"}。`;
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
