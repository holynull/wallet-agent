"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { classifyAgentTest, ConversationArtifacts, extractConversationArtifacts, formatProcessingDuration, formatResponseSummary, latestProgressText, parseSseFrame, parseSseFrames, providerPriority as getProviderPriority, responseMessage, restoreConversationMessages, sanitizeDebug, transactionChainId } from "../lib/agent-utils";

type Message = { id?: string; role: "user" | "assistant" | "system"; content: string; fullContent?: string; typing?: boolean; suggestions?: any[]; artifacts?: ConversationArtifacts; responseKey?: string };
type ProgressEntry = { text: string; elapsed?: number };
type Conversation = { conversation_id: string; session_id?: string; summary: string; status: string; updated_at: string };
type Provider = { request: (args: { method: string; params?: unknown[] }) => Promise<any>; on?: (event: string, callback: (...args: any[]) => void) => void; providers?: Provider[]; name?: string; providerName?: string; walletName?: string; _name?: string; isMetaMask?: boolean; isCatWallet?: boolean; isCatwallet?: boolean; _isCatWallet?: boolean };
type Quote = { provider?: string; provider_reference?: string; input_amount?: string; expected_output?: string; source_asset?: { symbol?: string }; destination_asset?: { symbol?: string } };
type AgentTest = {
  id: string;
  name: string;
  intent: string;
  message: string;
  requiresWallet?: boolean;
  expectedKinds?: string[];
  expectedStages?: string[];
  requiresQuote?: boolean;
  requiresPrices?: boolean;
  requiresAssets?: boolean;
  acceptsPreflightFailure?: boolean;
  metadata?: (wallet: typeof walletShape) => Record<string, unknown>;
};
type WalletShape = { address: string; chain: string };

const API_BASE = process.env.NEXT_PUBLIC_AGENT_API_URL ?? "http://localhost:8000";
const walletShape = {} as WalletShape;
function agentSwapMetadata(address: string) {
  return { swap_request: {
    source_asset: { chain: "BASE", chain_id: 8453, symbol: "USDC", decimals: 6, address: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913" },
    destination_asset: { chain: "BSC", chain_id: 56, symbol: "USDT", decimals: 18, address: "0x55d398326f99059fF775485246999027B3197955" },
    input_amount: "10", input_amount_raw: "10000000", sender_address: address, recipient_address: address,
  } };
}
const AGENT_TESTS: AgentTest[] = [
  { id: "clarification", name: "问候与澄清", intent: "clarification", message: "你好", expectedKinds: ["clarification"] },
  { id: "unsupported", name: "不支持的请求", intent: "unsupported", message: "测试不支持的功能分支", expectedKinds: ["unsupported"] },
  { id: "wallet_query", name: "钱包余额查询", intent: "wallet_query", message: "查询钱包余额", requiresWallet: true, expectedKinds: ["wallet_query"] },
  { id: "portfolio_query", name: "资产组合查询", intent: "portfolio_query", message: "查看我的资产组合", requiresWallet: true, expectedKinds: ["portfolio_query"], metadata: ({ address, chain }) => ({ portfolio_query: { address, chain } }) },
  { id: "gas_check", name: "Gas 检查", intent: "gas_check", message: "检查当前网络手续费", requiresWallet: true, expectedKinds: ["gas_check"], metadata: ({ address, chain }) => ({ gas_request: { address, chain } }) },
  { id: "asset_discovery", name: "Token 资产发现", intent: "asset_discovery", message: "搜索 USDC Token", expectedKinds: ["asset_discovery"], requiresAssets: true, metadata: ({ chain }) => ({ asset_query: { chain, search: "USDC" } }) },
  { id: "price_query", name: "Token 价格查询", intent: "price_query", message: "查询 ETH 价格", expectedKinds: ["price_query"], requiresPrices: true, metadata: ({ chain }) => ({ price_request: { chain, symbol: "ETH", decimals: 18 } }) },
  { id: "transaction_status", name: "交易状态查询", intent: "transaction_status", message: "查询交易状态", expectedKinds: ["transaction_status"], metadata: ({ chain }) => ({ transaction_query: { chain, tx_hash: `0x${"a".repeat(64)}` } }) },
  { id: "transfer", name: "转账预检查与准备", intent: "transfer", message: "准备一笔只读转账测试", expectedKinds: ["transfer_prepare"], acceptsPreflightFailure: true, metadata: ({ address, chain }) => ({ transfer_request: { chain, sender: address, recipient: `0x${"2".repeat(40)}`, amount: "0.000001", amount_raw: "1000000000000", token: null } }) },
  { id: "swap_quote", name: "兑换报价", intent: "swap_quote", message: "准备兑换报价测试", expectedKinds: ["swap_quote"], requiresQuote: true, metadata: ({ address }) => agentSwapMetadata(address) },
  { id: "swap_select", name: "报价选择分支", intent: "swap_select", message: "测试报价选择分支", expectedStages: ["quote_selection_required"] },
  { id: "swap_allowance", name: "Allowance 检查分支", intent: "swap_allowance", message: "测试 Allowance 检查分支", expectedStages: ["quote_selection_required"] },
  { id: "swap_prepare", name: "兑换交易准备边界", intent: "swap_prepare", message: "测试兑换交易准备边界", expectedKinds: ["clarification"], metadata: ({ address }) => agentSwapMetadata(address) },
  { id: "swap_status", name: "兑换订单状态", intent: "swap_status", message: "测试兑换订单状态分支", expectedKinds: ["swap_status"] },
];

async function requestApi(path: string, options?: RequestInit) {
  const response = await fetch(`${API_BASE}${path}`, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(JSON.stringify(body));
  return body;
}

export default function Home() {
  const [messages, setMessages] = useState<Message[]>([{ role: "assistant", content: "你好，我可以帮你查询余额、比较兑换报价，并准备未签名交易。告诉我你想做什么。" }]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [processing, setProcessing] = useState<{ label: string; startedAt: number } | null>(null);
  const [processingElapsed, setProcessingElapsed] = useState(0);
  const [lastMessage, setLastMessage] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const [debug, setDebug] = useState<string[]>([]);
  const [wallet, setWallet] = useState<{ provider: Provider; address: string; chainId: string; chain: string } | null>(null);
  const [walletProviders, setWalletProviders] = useState<Array<{ info?: any; provider: Provider; source?: string }>>([]);
  const walletProvidersRef = useRef<Array<{ info?: any; provider: Provider; source?: string }>>([]);
  const [quotes, setQuotes] = useState<Quote[]>([]);
  const [selectedQuote, setSelectedQuote] = useState<string | null>(null);
  const [pendingTransaction, setPendingTransaction] = useState<any>(null);
  const [pendingKind, setPendingKind] = useState<"swap" | "transfer">("swap");
  const [broadcastHash, setBroadcastHash] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<any>(null);
  const [approval, setApproval] = useState<any>(null);
  const [preflight, setPreflight] = useState<any>(null);
  const [status, setStatus] = useState<any>(null);
  const [walletData, setWalletData] = useState<any>(null);
  const [portfolioData, setPortfolioData] = useState<any>(null);
  const [priceData, setPriceData] = useState<any>(null);
  const [gasData, setGasData] = useState<any>(null);
  const [assetsData, setAssetsData] = useState<any>(null);
  const [suggestions, setSuggestions] = useState<any[]>([]);
  const [tokenCandidates, setTokenCandidates] = useState<any[]>([]);
  const [progress, setProgress] = useState<ProgressEntry[]>([]);
  const [backendDuration, setBackendDuration] = useState<number | null>(null);
  const [backendStage, setBackendStage] = useState("等待请求");
  const [agentTests, setAgentTests] = useState<Record<string, string>>({});
  const [historyOpen, setHistoryOpen] = useState(false);
  const messagesRef = useRef<HTMLDivElement>(null);
  const responseKeysRef = useRef<Set<string>>(new Set());

  function log(label: string, value: unknown) {
    setDebug((current) => [...current, `[${new Date().toISOString()}] ${label}\n${JSON.stringify(sanitizeDebug(value), null, 2)}`].slice(-100));
  }
  const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  async function loadHistory() {
    try { const body = await requestApi("/v1/agent/conversations?user_id=browser-demo", { headers }); setConversations(body.conversations ?? []); log("CONVERSATIONS", body); }
    catch (error) { setMessages((current) => [...current, { role: "assistant", content: `读取历史失败：${error instanceof Error ? error.message : String(error)}` }]); }
  }
  async function loadConversation(id: string) {
    try {
      clearConversationState();
      const body = await requestApi(`/v1/agent/conversations/${encodeURIComponent(id)}?user_id=browser-demo`, { headers });
      const latestArtifacts = body.latest_artifacts || extractConversationArtifacts(body.session);
      setConversationId(body.conversation_id);
      setSessionId(body.session_id ?? null);
      setMessages(restoreConversationMessages(body.messages ?? [], latestArtifacts));
      applyActiveArtifacts(latestArtifacts);
      setHistoryOpen(false);
      log("CONVERSATION", body);
    }
    catch (error) { setMessages((current) => [...current, { role: "assistant", content: `加载对话失败：${error instanceof Error ? error.message : String(error)}` }]); }
  }
  async function removeConversation(id: string) {
    if (!window.confirm("删除这条对话及其历史记录？")) return;
    try { await requestApi(`/v1/agent/conversations/${encodeURIComponent(id)}?user_id=browser-demo`, { method: "DELETE", headers }); if (id === conversationId) startNew(); await loadHistory(); }
    catch (error) { setMessages((current) => [...current, { role: "assistant", content: `删除对话失败：${error instanceof Error ? error.message : String(error)}` }]); }
  }
  function beginProcessing(label: string) { setProcessing({ label, startedAt: Date.now() }); setProcessingElapsed(0); }
  function endProcessing() { setProcessing(null); setProcessingElapsed(0); }
  async function runCardAction<T>(key: string, label: string, action: () => Promise<T>): Promise<T | undefined> {
    if (actionLoading) return undefined;
    setActionLoading(key);
    beginProcessing(label);
    try { return await action(); }
    finally { setActionLoading(null); endProcessing(); }
  }
  function clearConversationState() { abortRef.current?.abort(); abortRef.current = null; setBusy(false); setActionLoading(null); endProcessing(); setConversationId(null); setSessionId(null); setQuotes([]); setSelectedQuote(null); setPendingTransaction(null); setPendingKind("swap"); setBroadcastHash(null); setConfirmation(null); setApproval(null); setPreflight(null); setStatus(null); setWalletData(null); setPortfolioData(null); setPriceData(null); setGasData(null); setAssetsData(null); setSuggestions([]); setTokenCandidates([]); setProgress([]); setBackendDuration(null); setBackendStage("等待请求"); responseKeysRef.current.clear(); }
  function applyActiveArtifacts(artifacts?: ConversationArtifacts | null) {
    setQuotes(artifacts?.quotes || []);
    setConfirmation(artifacts?.confirmation || null);
    setPreflight(artifacts?.preflight || null);
    setApproval(artifacts?.approval || null);
    setPendingTransaction(artifacts?.pending_transaction || null);
    setPendingKind(artifacts?.pending_kind || "swap");
    setTokenCandidates(artifacts?.token_candidates || []);
    setWalletData(artifacts?.wallet || null);
    setPortfolioData(artifacts?.portfolio || null);
    setPriceData(artifacts?.price || null);
    setGasData(artifacts?.gas || null);
    setAssetsData(artifacts?.assets || null);
    setStatus(artifacts?.status || null);
    setBroadcastHash(artifacts?.broadcast_hash || null);
  }
  function appendAssistantMessage(content: string, suggestions: any[] = [], artifacts?: ConversationArtifacts | null, responseKey?: string) {
    if (responseKey && responseKeysRef.current.has(responseKey)) {
      setMessages((current) => current.map((item) => item.responseKey === responseKey ? { ...item, artifacts: artifacts || item.artifacts, suggestions: suggestions.length ? suggestions : item.suggestions } : item));
      return;
    }
    const id = `assistant-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    if (responseKey) responseKeysRef.current.add(responseKey);
    setMessages((current) => [...current, {
      id,
      role: "assistant",
      content: content.slice(0, 1),
      fullContent: content,
      typing: content.length > 1,
      suggestions,
      artifacts: artifacts || undefined,
      responseKey,
    }]);
  }
  function startNew() { clearConversationState(); setAgentTests({}); setDebug([]); setMessages([{ role: "assistant", content: "你好，我可以帮你查询余额、比较兑换报价，并准备未签名交易。告诉我你想做什么。" }]); }
  function chainName(id: string) { return ({ "0x1": "ETH", "0x38": "BSC", "0x89": "POLYGON", "0xa": "OPTIMISM", "0x2105": "BASE", "0xa4b1": "ARBITRUM" } as Record<string, string>)[id.toLowerCase()] ?? `EVM(${id})`; }
  function txValue(value: unknown) { if (value == null || value === "") return "0x0"; const text = String(value); return text.startsWith("0x") ? text : `0x${BigInt(text).toString(16)}`; }
  function providerPriority(entry: { info?: any; provider: Provider; source?: string }) {
    return getProviderPriority(entry);
  }
  useEffect(() => {
    const announce = (event: Event) => {
      const detail = (event as CustomEvent).detail;
      if (detail?.provider) setWalletProviders((items) => {
        const existing = items.find((item) => item.provider === detail.provider);
        const next = existing
          ? items.map((item) => item === existing ? { ...item, info: item.info || detail.info, source: [item.source, "eip6963"].filter(Boolean).join(" ") } : item)
          : [...items, { ...detail, source: "eip6963" }];
        walletProvidersRef.current = next;
        return next;
      });
    };
    window.addEventListener("eip6963:announceProvider", announce);
    window.dispatchEvent(new Event("eip6963:requestProvider"));
    const injectedWindow = window as any;
    const candidates: Array<{ provider: Provider; source: string }> = [];
    if (injectedWindow.catWallet) candidates.push({ provider: injectedWindow.catWallet, source: "window.catWallet" });
    if (injectedWindow.catwallet) candidates.push({ provider: injectedWindow.catwallet, source: "window.catwallet" });
    const injected = injectedWindow.ethereum as Provider | undefined;
    const injectedProviders = injected?.providers ?? [];
    if (injectedProviders.length) candidates.push(...injectedProviders.map((provider: Provider) => ({ provider, source: "window.ethereum.providers" })));
    if (injected) candidates.push({ provider: injected, source: "window.ethereum" });
    candidates.forEach((candidate) => setWalletProviders((items) => {
      const next = items.some((item) => item.provider === candidate.provider) ? items : [...items, candidate];
      walletProvidersRef.current = next;
      return next;
    }));
    return () => window.removeEventListener("eip6963:announceProvider", announce);
  }, []);
  async function connectWallet() {
    const injectedWindow = window as any;
    const rememberInjected = () => {
      const candidates: Array<{ provider: Provider; source: string }> = [];
      if (injectedWindow.catWallet) candidates.push({ provider: injectedWindow.catWallet, source: "window.catWallet" });
      if (injectedWindow.catwallet) candidates.push({ provider: injectedWindow.catwallet, source: "window.catwallet" });
      const injected = injectedWindow.ethereum as Provider | undefined;
      if (injected && Array.isArray(injected.providers)) candidates.push(...injected.providers.map((provider: Provider) => ({ provider, source: "window.ethereum.providers" })));
      if (injected) candidates.push({ provider: injected, source: "window.ethereum" });
      if (candidates.length) setWalletProviders((items) => {
        const next = candidates.reduce((all, candidate) => all.some((item) => item.provider === candidate.provider) ? all : [...all, candidate], items);
        walletProvidersRef.current = next;
        return next;
      });
    };
    rememberInjected();
    injectedWindow.dispatchEvent(new Event("eip6963:requestProvider"));
    await new Promise((resolve) => setTimeout(resolve, 300));
    rememberInjected();
    const provider = walletProvidersRef.current.slice().sort((left, right) => providerPriority(left) - providerPriority(right))[0]?.provider || injectedWindow.ethereum as Provider | undefined;
    if (!provider) { setMessages((current) => [...current, { role: "assistant", content: "没有检测到浏览器钱包插件。" }]); return; }
    try {
      const accounts = await provider.request({ method: "eth_requestAccounts" }); const chainId = await provider.request({ method: "eth_chainId" });
      if (!accounts?.[0]) throw new Error("钱包没有返回账户");
      setWallet({ provider, address: accounts[0], chainId, chain: chainName(chainId) });
      provider.on?.("accountsChanged", (next: string[]) => {
        clearConversationState();
        setWallet((current) => current && next[0] ? { ...current, address: next[0] } : null);
        setMessages((current) => [...current, { role: "system", content: next[0] ? "钱包账户已切换，已清理当前兑换状态。" : "钱包已断开。" }]);
      });
      provider.on?.("chainChanged", (next: string) => {
        setWallet((current) => current && { ...current, chainId: next, chain: chainName(next) });
        setMessages((current) => [...current, { role: "system", content: `网络已切换为 ${chainName(next)}，当前兑换会话仍然保留。` }]);
      });
    } catch (error) {
      setMessages((current) => [...current, { role: "assistant", content: `连接钱包失败：${error instanceof Error ? error.message : String(error)}` }]);
    }
  }
  async function sendTransaction(transaction: any) {
    if (!wallet) throw new Error("请先连接浏览器钱包");
    const active = await wallet.provider.request({ method: "eth_chainId" });
    const expected = transactionChainId(transaction);
    const numeric = (value: unknown) => Number.parseInt(String(value), String(value).startsWith("0x") ? 16 : 10);
    if (expected != null && numeric(active) !== numeric(expected)) {
      const chain = transaction.chain || `chain ${expected}`;
      try { await wallet.provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: `0x${numeric(expected).toString(16)}` }] }); }
      catch (error: any) {
        if (error?.code === 4001) throw new Error(`你拒绝了切换到 ${chain} 网络，请切换后重试`);
        if (error?.code === 4902) throw new Error(`钱包中未添加 ${chain} 网络，请先添加该网络后重试`);
        if (error?.code === -32601 || /not supported|unsupported/i.test(error?.message || "")) throw new Error(`当前钱包不支持自动切换网络，请手动切换到 ${chain} 后重试`);
        throw new Error(`切换到 ${chain} 失败：${error?.message || error}`);
      }
      const switched = await wallet.provider.request({ method: "eth_chainId" });
      if (numeric(switched) !== numeric(expected)) throw new Error(`钱包未切换到 ${chain}，请手动切换后重试`);
    }
    return wallet.provider.request({ method: "eth_sendTransaction", params: [{ from: wallet.address, to: transaction.to, data: transaction.data || "0x", value: txValue(transaction.value), ...(transaction.gas_limit ? { gas: txValue(transaction.gas_limit) } : {}), ...(transaction.max_fee_per_gas ? { maxFeePerGas: txValue(transaction.max_fee_per_gas) } : {}), ...(transaction.max_priority_fee_per_gas ? { maxPriorityFeePerGas: txValue(transaction.max_priority_fee_per_gas) } : {}) }] });
  }
  async function waitForApproval(maxAttempts = 60) {
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      const body = await requestApi(`/v1/swap/${sessionId}/continue`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo" }) });
      applyState(body);
      if (body?.pending_transaction || body?.stage === "swap_ready") return body;
      if (body?.stage === "approval_failed") throw new Error("Approve 交易执行失败");
      if (attempt + 1 < maxAttempts) await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    throw new Error("Approve 交易等待确认超时，请稍后重试");
  }
  async function refreshSession() {
    if (!sessionId) return;
    const body = await requestApi(`/v1/swap/${sessionId}?user_id=browser-demo`, { headers });
    applyState(body); setMessages((current) => [...current, { role: "system", content: "兑换 session 已刷新。" }]);
  }
  async function runDebugSmoke() {
    setMessages((current) => [...current, { role: "system", content: "开始联调检查：health → ready → agent turn → SSE。" }]);
    try { await requestApi("/health", { headers }); await requestApi("/ready", { headers }); await sendText("你好，联调检查", true); }
    catch (error) { setMessages((current) => [...current, { role: "assistant", content: `联调检查失败：${error instanceof Error ? error.message : String(error)}` }]); }
  }
  async function runAgentTests() {
    setAgentTests({});
    for (const spec of AGENT_TESTS) {
      setAgentTests((current) => ({ ...current, [spec.id]: "运行中" }));
      const address = wallet?.address || `0x${"1".repeat(40)}`;
      const chain = wallet?.chain || "BASE";
      if (spec.requiresWallet && !wallet) {
        setAgentTests((current) => ({ ...current, [spec.id]: "阻塞：需要连接钱包" }));
        log(`AGENT TEST ${spec.id}`, { skipped: true, reason: "wallet_not_connected" });
        continue;
      }
      const metadata = { ...(spec.metadata?.({ address, chain }) || {}), agent_test_intent: spec.intent };
      const request = {
        user_id: "browser-demo", conversation_id: `agent-test-${Date.now()}-${spec.id}`,
        message: spec.message, address: spec.requiresWallet ? address : undefined,
        chain: spec.requiresWallet ? chain : undefined, metadata,
      };
      try {
        const turn = await requestApi("/v1/agent/turn", { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify(request) });
        const events = await readStream(turn.run_id, false);
        const result = classifyAgentTest(spec, events);
        setAgentTests((current) => ({ ...current, [spec.id]: `${result.status === "pass" ? "通过" : result.status === "blocked" ? "阻塞" : "失败"}：${result.detail}` }));
        log(`AGENT TEST ${spec.id}`, { request, turn, events, result });
      } catch (error) {
        const result = classifyAgentTest(spec, [], error);
        setAgentTests((current) => ({ ...current, [spec.id]: `失败：${result.detail}` }));
        log(`AGENT TEST ${spec.id}`, { request, error: result.detail, result });
      }
    }
  }
  async function sendText(text: string, display = false, extraMetadata: Record<string, unknown> = {}) {
    // Each request owns its own progress timeline; do not mix it with the prior turn.
    setProgress([]);
    setBackendDuration(null);
    setBackendStage("正在处理请求");
    const requestStartedAt = performance.now();
    const body = await requestApi("/v1/agent/turn", { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", conversation_id: conversationId ?? undefined, session_id: sessionId ?? undefined, message: text, address: wallet?.address, chain: wallet?.chain, metadata: { ...(wallet ? { wallet_chain_id: wallet.chainId } : {}), ...extraMetadata } }), signal: abortRef.current?.signal });
    setConversationId(body.conversation_id); setSessionId(body.session_id ?? sessionId); log("API POST /v1/agent/turn", body); await readStream(body.run_id, true, requestStartedAt);
    if (display) setMessages((current) => [...current, { role: "system", content: "联调检查完成。" }]);
  }
  async function readStream(runId: string, render = true, requestStartedAt = performance.now()) {
    let lastEventId = ""; let reconnects = 0; let terminalReceived = false; let assistant = ""; let finalResponse: any = null; const events: any[] = [];
    const processFrame = (frame: string) => {
      const parsedFrame = parseSseFrame(frame);
      const eventName = parsedFrame.event;
      const data = parsedFrame.data;
      if (!data) return;
      try {
        const payload = JSON.parse(data);
        if (parsedFrame.id) lastEventId = parsedFrame.id;
        if (["complete", "error", "action_required"].includes(eventName)) terminalReceived = true;
        log(`SSE ${eventName}`, payload);
        const current = payload?.state || payload?.data?.state || payload?.data || payload;
        events.push({ event: eventName, payload, state: current });
        const streamedResponse = ["response", "complete", "action_required"].includes(eventName)
          ? current?.response?.response || current?.response || payload?.response
          : null;
        if (streamedResponse?.kind && eventName !== "progress") finalResponse = streamedResponse;
        const progressValue = current?.progress || (eventName === "progress" ? current?.data || current : null);
        const elapsed = Number(progressValue?.elapsed_ms);
        if (progressValue && Number.isFinite(elapsed)) setBackendDuration(elapsed);
        if (progressValue?.message || progressValue?.stage) setBackendStage(String(progressValue.message || progressValue.stage));
        if (render) applyState({ ...payload, event: eventName, runId });
        const content = streamedResponse?.message || current?.response?.message || payload?.response?.message;
        if (content && eventName !== "progress") assistant = content;
      } catch { /* ignore malformed keepalive */ }
    };

    while (true) {
      let buffer = "";
      try {
        const suffix = lastEventId ? `?last_event_id=${encodeURIComponent(lastEventId)}` : "";
        const response = await fetch(`${API_BASE}/v1/agent/stream/${runId}${suffix}`, { headers, signal: abortRef.current?.signal });
        if (!response.ok) { const raw = await response.text(); throw new Error(`SSE ${response.status}: ${raw || response.statusText}`); }
        if (!response.body) throw new Error("SSE stream unavailable");
        const reader = response.body.getReader(); const decoder = new TextDecoder();
        while (true) {
          const item = await reader.read(); if (item.done) break;
          buffer += decoder.decode(item.value, { stream: true });
          const parsed = parseSseFrames(buffer); buffer = parsed.remainder;
          parsed.frames.forEach(processFrame);
        }
        buffer += decoder.decode();
        if (buffer.trim()) processFrame(buffer);
        if (!terminalReceived) throw new Error("SSE stream ended before a terminal event");
        break;
      } catch (error) {
        if ((error as Error)?.name === "AbortError" || reconnects >= 3) throw error;
        reconnects += 1;
        log(`SSE RECONNECT ${runId}`, { last_event_id: lastEventId, attempt: reconnects });
        await new Promise((resolve) => setTimeout(resolve, 250 * reconnects));
      }
    }
    if (render) setBackendDuration(Math.max(0, performance.now() - requestStartedAt));
    if (render && assistant && !finalResponse && !responseKeysRef.current.has(`fallback:${assistant}`)) { responseKeysRef.current.add(`fallback:${assistant}`); setMessages((current) => [...current, { role: "assistant", content: assistant }]); }
    return events;
  }
  async function selectQuote(reference: string) {
    if (!sessionId) { setMessages((current) => [...current, { role: "assistant", content: "当前没有兑换 session。请重新发起兑换。" }]); return; }
    await runCardAction(`quote:${reference}`, "正在选择报价", async () => {
      try { const body = await requestApi(`/v1/swap/${sessionId}/select-quote`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", provider_reference: reference }) }); setSelectedQuote(reference); applyState(body); setMessages((current) => [...current, { role: "system", content: "报价已选择，正在检查余额和 Allowance。" }]); }
      catch (error) { setMessages((current) => [...current, { role: "assistant", content: `选择报价失败：${error instanceof Error ? error.message : String(error)}` }]); }
    });
  }
  async function confirmSwap(approved: boolean) {
    if (!sessionId) return;
    await runCardAction(approved ? "confirm" : "cancel-confirmation", approved ? "正在确认兑换" : "正在取消兑换", async () => {
      try { const body = await requestApi(`/v1/swap/${sessionId}/confirm`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", approved }) }); applyState(body); }
      catch (error) { setMessages((current) => [...current, { role: "assistant", content: `${approved ? "确认" : "取消"}兑换失败：${error instanceof Error ? error.message : String(error)}` }]); }
    });
  }
  async function approveSwap() {
    if (!approval) return;
    await runCardAction("approve", "正在提交 Approve", async () => {
      try { const hash = await sendTransaction(approval); const body = await requestApi(`/v1/swap/${sessionId}/approve-broadcast`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", chain: approval.chain, approve_tx_hash: hash }) }); applyState(body); await waitForApproval(); }
      catch (error) { setMessages((current) => [...current, { role: "assistant", content: `Approve 失败：${error instanceof Error ? error.message : String(error)}` }]); }
    });
  }
  async function broadcastPendingTransaction() {
    if (!pendingTransaction) return;
    await runCardAction("broadcast", pendingKind === "transfer" ? "正在广播转账" : "正在广播兑换交易", async () => {
      try { const hash = await sendTransaction(pendingTransaction); const path = pendingKind === "transfer" ? `/v1/transfer/${sessionId}/broadcast` : `/v1/swap/${sessionId}/broadcast`; const body = await requestApi(path, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", chain: pendingTransaction.chain, tx_hash: hash }) }); setBroadcastHash(hash); applyState({ ...body, tx_hash: hash }); }
      catch (error) { setMessages((current) => [...current, { role: "assistant", content: `${pendingKind === "transfer" ? "转账" : "Swap"} 失败：${error instanceof Error ? error.message : String(error)}` }]); }
    });
  }
  async function queryPendingTransaction() {
    if (!pendingTransaction || !broadcastHash) return;
    await runCardAction("transaction-status", "正在查询交易状态", async () => {
      try { const body = await requestApi(`/v1/transactions/${encodeURIComponent(pendingTransaction.chain)}/${encodeURIComponent(broadcastHash)}`, { headers }); applyState({ kind: "transaction_status", ...body }); }
      catch (error) { setMessages((current) => [...current, { role: "assistant", content: `查询交易状态失败：${error instanceof Error ? error.message : String(error)}` }]); }
    });
  }
  function applyState(value: any) {
    const eventName = value?.event;
    const current = value?.state || value?.data?.state || value?.data || value;
    if (eventName === "progress" || current?.progress) {
      const progressValue = current?.progress || current?.data || current;
      const progressText = progressValue?.message || progressValue?.stage;
      const elapsed = Number(progressValue?.elapsed_ms);
      if (Number.isFinite(elapsed)) setBackendDuration(elapsed);
      if (progressText) {
        const text = String(progressText);
        const line = `${text}${Number.isFinite(elapsed) ? ` · ${elapsed} ms` : ""}`;
        setBackendStage(text);
        setProgress((items) => items.some((item) => item.text === text) ? items : [...items, { text, elapsed: Number.isFinite(elapsed) ? elapsed : undefined }].slice(-20));
      }
    }
    if (current?.selected_provider_reference) setSelectedQuote(current.selected_provider_reference);
    let response = ["response", "complete", "action_required"].includes(eventName)
      ? current?.response?.response || current?.response || current
      : null;
    if (!eventName && current && typeof current === "object") {
      response = current.response?.response || current.response || (current.kind ? current : null);
      const stage = current.stage || current.status;
      if (!response && current.confirmation_state?.status === "requested") response = { kind: "confirmation_required", confirmation: current.confirmation_state };
      else if (!response && stage === "approval_required") response = { kind: "approval_required", stage, approval_transaction: current.approval_transaction };
      else if (!response && current.pending_transaction) response = { kind: current.intent === "transfer" ? "transfer_prepare" : "swap_prepare", stage, transaction: current.pending_transaction, preflight: current.preflight };
      else if (!response && current.broadcast_status) response = { kind: "swap_status", status: current.broadcast_status, tx_hash: current.broadcast_tx_hash, message: current.message };
    }
    if (eventName === "error") setMessages((previous) => [...previous, { role: "assistant", content: current?.error?.message || responseMessage(current?.error) || "Agent 执行失败。" }]);
    if (response?.kind === "clarification") {
      const nextSuggestions = response.suggestions || [];
      setSuggestions(nextSuggestions);
    }
    const artifacts = response ? extractConversationArtifacts({ ...current, response }) : null;
    if (response) applyActiveArtifacts(artifacts);
    const summary = formatResponseSummary(response);
    const finalMessage = response?.message || summary;
    if (finalMessage) {
      const key = eventName ? `${value?.runId || "stream"}:${response.kind || "response"}:${finalMessage}` : undefined;
      appendAssistantMessage(finalMessage, response?.kind === "clarification" ? response.suggestions || [] : [], artifacts, key);
    }
  }
  async function send(event?: FormEvent) {
    event?.preventDefault();
    const text = message.trim(); if (!text || busy) return;
    setMessages((current) => [...current, { role: "user", content: text }]); setMessage(""); setLastMessage(text); setBusy(true); beginProcessing("正在处理请求"); abortRef.current = new AbortController();
    try {
      if (/^(取消|停止|退出)(兑换|交易)?$/.test(text)) {
        if (sessionId) await requestApi(`/v1/swap/${sessionId}/cancel`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo" }) });
        setMessages((current) => [...current, { role: "system", content: sessionId ? "已取消当前兑换。你可以随时重新发起新的兑换。" : "当前没有进行中的兑换。" }]);
      } else if (/^(选|选择)(第)?一|1号|第一个/.test(text) && quotes.length) {
        await selectQuote(quotes[0].provider_reference || "");
      } else await sendText(text);
    } catch (error) { if ((error as Error).name !== "AbortError") setMessages((current) => [...current, { role: "assistant", content: `请求失败：${error instanceof Error ? error.message : String(error)}` }]); } finally { setBusy(false); endProcessing(); abortRef.current = null; }
  }
  function handleComposerKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void send(event);
    }
  }
  async function cancelRequest() {
    if (sessionId) {
      try { await requestApi(`/v1/swap/${sessionId}/cancel`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo" }) }); }
      catch (error) { setMessages((current) => [...current, { role: "assistant", content: `取消兑换失败：${error instanceof Error ? error.message : String(error)}` }]); return; }
    }
    abortRef.current?.abort(); setBusy(false); endProcessing(); setMessages((current) => [...current, { role: "system", content: sessionId ? "已取消当前兑换和请求。" : "已取消本次请求。" }]);
  }
  async function chooseSuggestion(text: string, data?: Record<string, unknown>) { if (busy || actionLoading) return; setMessage(text); setMessages((current) => [...current, { role: "user", content: text }]); setLastMessage(text); setBusy(true); beginProcessing("正在处理请求"); abortRef.current = new AbortController(); try { await sendText(text, false, data ? { suggestion_data: data } : {}); } catch (error) { if ((error as Error).name !== "AbortError") setMessages((current) => [...current, { role: "assistant", content: `请求失败：${error instanceof Error ? error.message : String(error)}` }]); } finally { setBusy(false); endProcessing(); abortRef.current = null; setMessage(""); } }
  async function chooseToken(candidate: any) {
    const side = candidate.side === "source" ? "来源" : "目标";
    await chooseSuggestion(`${side} Token 合约地址是 ${candidate.address}，精度是 ${candidate.decimals}`);
  }
  async function retryLastMessage() {
    if (!lastMessage || busy) return;
    setBusy(true); beginProcessing("正在重试请求"); abortRef.current = new AbortController();
    try { await sendText(lastMessage); }
    catch (error) { if ((error as Error).name !== "AbortError") setMessages((current) => [...current, { role: "assistant", content: `请求失败：${error instanceof Error ? error.message : String(error)}` }]); }
    finally { setBusy(false); endProcessing(); abortRef.current = null; }
  }
  useEffect(() => {
    const active = messages.find((item) => item.typing && item.fullContent && item.content.length < item.fullContent.length);
    if (!active?.id || !active.fullContent) return;
    const timer = window.setTimeout(() => {
      setMessages((current) => current.map((item) => {
        if (item.id !== active.id || !item.fullContent) return item;
        const nextLength = Math.min(item.content.length + 1, item.fullContent.length);
        return {
          ...item,
          content: item.fullContent.slice(0, nextLength),
          typing: nextLength < item.fullContent.length,
        };
      }));
    }, 18);
    return () => window.clearTimeout(timer);
  }, [messages]);
  useEffect(() => {
    if (!messagesRef.current) return;
    const firstFrame = window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        if (messagesRef.current) messagesRef.current.scrollTop = messagesRef.current.scrollHeight;
      });
    });
    return () => window.cancelAnimationFrame(firstFrame);
  }, [messages]);
  useEffect(() => {
    if (!processing) return;
    const timer = window.setInterval(() => setProcessingElapsed(Date.now() - processing.startedAt), 100);
    return () => window.clearInterval(timer);
  }, [processing]);
  const lastAssistantIndex = messages.map((item) => item.role).lastIndexOf("assistant");
  function renderConversationCards(artifacts: ConversationArtifacts, interactive: boolean) {
    const cardQuotes = artifacts.quotes || [];
    const cardConfirmation = artifacts.confirmation;
    const cardPreflight = artifacts.preflight;
    const cardApproval = artifacts.approval;
    const cardTransaction = artifacts.pending_transaction;
    const cardPendingKind = artifacts.pending_kind || "swap";
    const cardTokenCandidates = artifacts.token_candidates || [];
    const cardStatus = artifacts.status;
    return <>
      {cardQuotes.length > 0 && <section className="card" data-card-kind="quotes"><h3>报价列表</h3>{cardQuotes.map((quote: Quote) => <div className="quote" key={quote.provider_reference}><strong>{quote.provider} · {quote.provider_reference}</strong><span>{quote.input_amount} {quote.source_asset?.symbol} → {quote.expected_output} {quote.destination_asset?.symbol}</span>{interactive && <button className="secondary" disabled={!quote.provider_reference || Boolean(actionLoading) || busy} onClick={() => void selectQuote(quote.provider_reference!)}>{actionLoading === `quote:${quote.provider_reference}` ? <><i className="loading-spinner" />处理中…</> : selectedQuote === quote.provider_reference ? "已选择" : "选择此报价"}</button>}</div>)}</section>}
      {cardConfirmation?.status === "requested" && <section className="card" data-card-kind="confirmation"><h3>请确认兑换</h3><p>{cardConfirmation.summary?.input_amount} {cardConfirmation.summary?.source_asset?.symbol} → {cardConfirmation.summary?.expected_output} {cardConfirmation.summary?.destination_asset?.symbol}{cardConfirmation.summary?.provider ? `\nProvider：${cardConfirmation.summary.provider}` : ""}{cardConfirmation.expires_at ? `\n有效期至：${cardConfirmation.expires_at}` : ""}</p>{interactive && <div className="card-actions"><button disabled={Boolean(actionLoading) || busy} onClick={() => void confirmSwap(true)}>{actionLoading === "confirm" ? <><i className="loading-spinner" />处理中…</> : "确认兑换"}</button><button className="secondary" disabled={Boolean(actionLoading) || busy} onClick={() => void confirmSwap(false)}>{actionLoading === "cancel-confirmation" ? <><i className="loading-spinner" />处理中…</> : "取消"}</button></div>}</section>}
      {cardPreflight && <section className="card" data-card-kind="preflight"><h3>{cardPreflight.ok ? "交易预检查通过" : "交易预检查未通过"}</h3><p>{cardPreflight.gas_sources ? `Gas 来源：${Object.entries(cardPreflight.gas_sources).map(([name, used]) => `${name}=${used ? "已用" : "未用"}`).join("、")}\n` : ""}{cardPreflight.simulation?.success === false ? `模拟警告：${cardPreflight.simulation.error || cardPreflight.simulation.fail_reason || "simulation failed"}\n` : ""}{(cardPreflight.checks || []).map((item: any) => `${item.status === "passed" ? "✓" : item.status === "warning" ? "!" : "×"} ${item.message}`).join("\n")}</p></section>}
      {cardApproval && <section className="card" data-card-kind="approval"><h3>需要 Approve 授权</h3><p>请确认授权额度，浏览器钱包会在本地弹窗签名。</p><pre className="debug">{JSON.stringify(cardApproval, null, 2)}</pre>{interactive && <button disabled={Boolean(actionLoading) || busy} onClick={() => void approveSwap()}>{actionLoading === "approve" ? <><i className="loading-spinner" />处理中…</> : "钱包签名并广播 Approve"}</button>}</section>}
      {cardTransaction && <section className="card" data-card-kind="transaction"><h3>{cardPendingKind === "transfer" ? "转账交易已准备好" : "兑换交易已准备好"}</h3><p>{cardPendingKind === "transfer" ? "请确认收款地址和金额，钱包会在当前转账链本地签名并广播。" : "请在钱包中确认未签名交易。"}</p><pre className="debug">{JSON.stringify(cardTransaction, null, 2)}</pre>{interactive && <div className="card-actions"><button disabled={Boolean(actionLoading) || busy} onClick={() => void broadcastPendingTransaction()}>{actionLoading === "broadcast" ? <><i className="loading-spinner" />处理中…</> : `钱包签名并广播${cardPendingKind === "transfer" ? "转账" : " Swap"}`}</button>{cardPendingKind === "transfer" && broadcastHash && <button className="secondary" disabled={Boolean(actionLoading) || busy} onClick={() => void queryPendingTransaction()}>{actionLoading === "transaction-status" ? <><i className="loading-spinner" />处理中…</> : "查询交易状态"}</button>}</div>}</section>}
      {cardTokenCandidates.length > 0 && <section className="card" data-card-kind="tokens"><h3>请选择 Token</h3><div className="card-actions">{cardTokenCandidates.map((item, index) => interactive ? <button className="secondary" key={`${item.address}-${index}`} onClick={() => void chooseToken(item)}>{index + 1}. {item.symbol} · {item.chain} · {String(item.address).slice(0, 8)}…</button> : <span key={`${item.address}-${index}`}>{index + 1}. {item.symbol} · {item.chain}</span>)}</div></section>}
      {artifacts.wallet && <section className="card" data-card-kind="wallet"><h3>钱包余额</h3><pre className="debug">{JSON.stringify(artifacts.wallet, null, 2)}</pre></section>}
      {artifacts.portfolio && <section className="card" data-card-kind="portfolio"><h3>资产组合</h3><pre className="debug">{JSON.stringify(artifacts.portfolio, null, 2)}</pre></section>}
      {artifacts.price && <section className="card" data-card-kind="price"><h3>价格与市场数据</h3><pre className="debug">{JSON.stringify(artifacts.price, null, 2)}</pre></section>}
      {artifacts.gas && <section className="card" data-card-kind="gas"><h3>Gas 检查</h3><pre className="debug">{JSON.stringify(artifacts.gas, null, 2)}</pre></section>}
      {artifacts.assets && <section className="card" data-card-kind="assets"><h3>Token 资产发现</h3><pre className="debug">{JSON.stringify(artifacts.assets, null, 2)}</pre></section>}
      {cardStatus && <section className="card" data-card-kind="status"><h3>订单状态</h3><p>{cardStatus.message || responseMessage(cardStatus) || cardStatus.status?.status || "处理中"}</p><pre className="debug">{JSON.stringify(cardStatus, null, 2)}</pre></section>}
    </>;
  }
  return <main>
    <header className="app-header"><div className="brand"><span className="brand-mark">W</span><div><h1>Wallet Agent</h1><p>智能钱包操作台 · 安全地查询、报价与准备交易</p></div></div><div className="wallet-bar"><span className={`wallet-status ${wallet ? "connected" : ""}`}><i />{wallet ? `${wallet.chain} · ${wallet.address.slice(0, 6)}…${wallet.address.slice(-4)}` : "未连接钱包"}</span><input className="token" type="password" placeholder="Bearer token（可选）" value={token} onChange={(event) => setToken(event.target.value)} /><button onClick={() => void connectWallet()}>{wallet ? "重新连接" : "连接钱包"}</button><button className="secondary" onClick={() => void refreshSession()}>刷新</button><button className="secondary" onClick={() => { setHistoryOpen(!historyOpen); if (!historyOpen) void loadHistory(); }}>历史</button></div></header>
    <div className="workspace">
      <section className="main-column">
        {historyOpen && <section className="panel"><div className="toolbar"><button className="secondary" onClick={() => void loadHistory()}>重新加载</button><button className="secondary" onClick={startNew}>新建对话</button></div><div className="list">{conversations.length === 0 ? <p className="muted">暂无已保存的对话。</p> : conversations.map((item) => <div key={item.conversation_id} className="conversation"><button onClick={() => void loadConversation(item.conversation_id)}><strong>{item.summary}</strong><span>{new Date(item.updated_at).toLocaleString()} · {item.status}</span></button><button className="danger delete" onClick={() => void removeConversation(item.conversation_id)}>删除</button></div>)}</div></section>}
        <section className="action-strip"><span>工具</span><button className="secondary" onClick={() => void runDebugSmoke()}>联调检查</button><button className="secondary" onClick={() => void runAgentTests()}>测试全部 Agent 功能</button></section>
        <div ref={messagesRef} id="messages">
      {messages.map((item, index) => <div className="message-group" key={item.id || `${index}-${item.content}`}><div className={`message ${item.role}`}><div className="message-stack">{item.role !== "system" && <div className="bubble">{item.content}</div>}{item.suggestions?.length ? <div className="inline-suggestions"><span>建议操作</span><div className="card-actions">{item.suggestions.map((suggestion, suggestionIndex) => <button className="secondary" disabled={busy || Boolean(actionLoading)} key={`${suggestion.label || suggestion.message}-${suggestionIndex}`} onClick={() => void chooseSuggestion(suggestion.message || suggestion.label || "", suggestion.data)}>{busy || actionLoading ? <><i className="loading-spinner" />处理中…</> : suggestion.label || suggestion.message}</button>)}</div></div> : null}{item.role === "assistant" && item.artifacts && renderConversationCards(item.artifacts, index === lastAssistantIndex)}</div></div></div>)}
        </div>
        <form className="composer" onSubmit={send}><div className="composer-row"><textarea onKeyDown={handleComposerKeyDown} value={message} onChange={(event) => setMessage(event.target.value)} placeholder="输入消息，Enter 发送，Shift+Enter 换行" disabled={busy || Boolean(actionLoading)} /><button disabled={busy || Boolean(actionLoading)}>{busy ? <><i className="loading-spinner" />处理中…</> : "发送"}</button></div><div className="toolbar"><span className="muted">{processing ? `${processing.label} · ${formatProcessingDuration(processingElapsed)}` : "就绪"}</span>{busy && <button type="button" className="secondary" onClick={cancelRequest}>取消请求</button>}{!busy && !actionLoading && lastMessage && <button type="button" className="secondary" onClick={() => void retryLastMessage()}>重试</button>}</div><div className="meta"><span>conversation: {conversationId ?? "新会话"}</span><span>session: {sessionId ?? "未创建"}</span></div></form>
      </section>
      <aside className="side-column"><div className="side-card"><div className="side-title"><span>运行状态</span><b className={processing ? "busy" : "ready"}>{processing ? "处理中" : "就绪"}</b></div><div className="stat"><span>网络</span><strong>{wallet?.chain || "未连接"}</strong></div><div className="stat"><span>会话</span><strong>{sessionId ? "已建立" : "新会话"}</strong></div><div className="stat"><span>请求耗时</span><strong>{processing ? formatProcessingDuration(processingElapsed) : "--"}</strong></div><div className="stat"><span>后端耗时</span><strong>{backendDuration == null ? "--" : `${backendDuration} ms`}</strong></div><div className="stat"><span>当前阶段</span><strong className="stage-value">{processing?.label || latestProgressText(progress, backendStage)}</strong></div></div>{progress.length > 0 && <section className="side-card progress-card"><div className="progress-heading"><h3>处理过程</h3><span>{backendDuration == null ? "进行中" : `${backendDuration} ms`}</span></div><ol className="progress-list">{progress.map((item, index) => <li key={`${item.text}-${index}`}><i /> <span>{item.text}{item.elapsed != null ? ` · ${item.elapsed} ms` : ""}</span></li>)}</ol></section>}<details className="panel debug-panel"><summary>后端调试数据（{debug.length} 条）</summary><div className="toolbar"><button className="secondary" onClick={() => navigator.clipboard?.writeText(debug.join("\n\n"))}>复制</button><button className="secondary" onClick={() => setDebug([])}>清空</button></div><pre className="debug">{debug.join("\n\n") || "等待后端响应…"}</pre></details>{Object.keys(agentTests).length > 0 && <div className="side-card test-card"><div className="side-title"><span>Agent 测试</span><b>{Object.values(agentTests).filter((value) => value.startsWith("通过")).length}/{AGENT_TESTS.length}</b></div><div className="test-list">{AGENT_TESTS.map((item) => <div key={item.id}><span>{item.name}</span><em>{agentTests[item.id] || "未运行"}</em></div>)}</div></div>}</aside>
    </div>
  </main>;
}
