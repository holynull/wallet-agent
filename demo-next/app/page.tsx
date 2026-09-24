"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { classifyAgentTest, formatResponseSummary, parseSseFrames, responseMessage, sanitizeDebug, transactionChainId } from "../lib/agent-utils";

type Message = { role: "user" | "assistant" | "system"; content: string };
type Conversation = { conversation_id: string; session_id?: string; summary: string; status: string; updated_at: string };
type Provider = { request: (args: { method: string; params?: unknown[] }) => Promise<any>; on?: (event: string, callback: (...args: any[]) => void) => void; providers?: Provider[]; name?: string; providerName?: string; walletName?: string; isCatWallet?: boolean; isCatwallet?: boolean };
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
  const [lastMessage, setLastMessage] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const [debug, setDebug] = useState<string[]>([]);
  const [wallet, setWallet] = useState<{ provider: Provider; address: string; chainId: string; chain: string } | null>(null);
  const [walletProviders, setWalletProviders] = useState<Array<{ info?: any; provider: Provider; source?: string }>>([]);
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
  const [progress, setProgress] = useState<string[]>([]);
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
    try { clearConversationState(); const body = await requestApi(`/v1/agent/conversations/${encodeURIComponent(id)}?user_id=browser-demo`, { headers }); setConversationId(body.conversation_id); setSessionId(body.session_id ?? null); setMessages(body.messages ?? []); setHistoryOpen(false); log("CONVERSATION", body); }
    catch (error) { setMessages((current) => [...current, { role: "assistant", content: `加载对话失败：${error instanceof Error ? error.message : String(error)}` }]); }
  }
  async function removeConversation(id: string) {
    if (!window.confirm("删除这条对话及其历史记录？")) return;
    try { await requestApi(`/v1/agent/conversations/${encodeURIComponent(id)}?user_id=browser-demo`, { method: "DELETE", headers }); if (id === conversationId) startNew(); await loadHistory(); }
    catch (error) { setMessages((current) => [...current, { role: "assistant", content: `删除对话失败：${error instanceof Error ? error.message : String(error)}` }]); }
  }
  function clearConversationState() { abortRef.current?.abort(); abortRef.current = null; setBusy(false); setConversationId(null); setSessionId(null); setQuotes([]); setSelectedQuote(null); setPendingTransaction(null); setPendingKind("swap"); setBroadcastHash(null); setConfirmation(null); setApproval(null); setPreflight(null); setStatus(null); setWalletData(null); setPortfolioData(null); setPriceData(null); setGasData(null); setAssetsData(null); setSuggestions([]); setTokenCandidates([]); setProgress([]); responseKeysRef.current.clear(); }
  function startNew() { clearConversationState(); setAgentTests({}); setDebug([]); setMessages([{ role: "assistant", content: "你好，我可以帮你查询余额、比较兑换报价，并准备未签名交易。告诉我你想做什么。" }]); }
  function chainName(id: string) { return ({ "0x1": "ETH", "0x38": "BSC", "0x89": "POLYGON", "0xa": "OPTIMISM", "0x2105": "BASE", "0xa4b1": "ARBITRUM" } as Record<string, string>)[id.toLowerCase()] ?? `EVM(${id})`; }
  function txValue(value: unknown) { if (value == null || value === "") return "0x0"; const text = String(value); return text.startsWith("0x") ? text : `0x${BigInt(text).toString(16)}`; }
  function providerText(entry: { info?: any; provider: Provider; source?: string }) {
    return [entry.info?.name, entry.info?.rdns, entry.info?.uuid, entry.provider?.name, entry.provider?.providerName, entry.provider?.walletName, entry.source]
      .filter(Boolean).join(" ").toLowerCase();
  }
  function isCatWallet(entry: { info?: any; provider: Provider; source?: string }) {
    return providerText(entry).includes("catwallet") || providerText(entry).includes("cat wallet")
      || entry.provider?.isCatWallet === true || entry.provider?.isCatwallet === true;
  }
  function providerPriority(entry: { info?: any; provider: Provider; source?: string }) {
    if (isCatWallet(entry)) return 0;
    if (entry.source === "window.ethereum") return 2;
    if (entry.source === "window.ethereum.providers") return 3;
    return 4;
  }
  useEffect(() => {
    const announce = (event: Event) => {
      const detail = (event as CustomEvent).detail;
      if (detail?.provider) setWalletProviders((items) => items.some((item) => item.provider === detail.provider) ? items : [...items, { ...detail, source: "eip6963" }]);
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
    candidates.forEach((candidate) => setWalletProviders((items) => items.some((item) => item.provider === candidate.provider) ? items : [...items, candidate]));
    return () => window.removeEventListener("eip6963:announceProvider", announce);
  }, []);
  async function connectWallet() {
    const provider = walletProviders.slice().sort((left, right) => providerPriority(left) - providerPriority(right))[0]?.provider || (window as any).ethereum as Provider | undefined;
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
    const body = await requestApi("/v1/agent/turn", { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", conversation_id: conversationId ?? undefined, session_id: sessionId ?? undefined, message: text, address: wallet?.address, chain: wallet?.chain, metadata: { ...(wallet ? { wallet_chain_id: wallet.chainId } : {}), ...extraMetadata } }), signal: abortRef.current?.signal });
    setConversationId(body.conversation_id); setSessionId(body.session_id ?? sessionId); log("API POST /v1/agent/turn", body); await readStream(body.run_id, true);
    if (display) setMessages((current) => [...current, { role: "system", content: "联调检查完成。" }]);
  }
  async function readStream(runId: string, render = true) {
    const response = await fetch(`${API_BASE}/v1/agent/stream/${runId}`, { headers, signal: abortRef.current?.signal });
    if (!response.ok) { const raw = await response.text(); throw new Error(`SSE ${response.status}: ${raw || response.statusText}`); }
    if (!response.body) throw new Error("SSE stream unavailable");
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ""; let assistant = ""; const events: any[] = [];
    const processFrame = (frame: string) => {
      const eventName = frame.match(/^event:\s*(.+)$/m)?.[1] || "message";
      const data = frame.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim()).join("\n");
      if (!data) return;
      try {
        const payload = JSON.parse(data);
        log(`SSE ${eventName}`, payload);
        const current = payload?.state || payload?.data?.state || payload?.data || payload;
        events.push({ event: eventName, payload, state: current });
        if (render) applyState({ ...payload, event: eventName });
        const content = current?.response?.message ?? payload?.response?.message;
        if (content) assistant = content;
      } catch { /* ignore malformed keepalive */ }
    };
    while (true) {
      const item = await reader.read(); if (item.done) break;
      buffer += decoder.decode(item.value, { stream: true });
      const parsed = parseSseFrames(buffer); buffer = parsed.remainder;
      parsed.frames.forEach(processFrame);
    }
    buffer += decoder.decode();
    if (buffer.trim()) processFrame(buffer);
    if (render && assistant && !responseKeysRef.current.has(`fallback:${assistant}`)) { responseKeysRef.current.add(`fallback:${assistant}`); setMessages((current) => [...current, { role: "assistant", content: assistant }]); }
    return events;
  }
  async function selectQuote(reference: string) {
    if (!sessionId) { setMessages((current) => [...current, { role: "assistant", content: "当前没有兑换 session。请重新发起兑换。" }]); return; }
    try { const body = await requestApi(`/v1/swap/${sessionId}/select-quote`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", provider_reference: reference }) }); setSelectedQuote(reference); applyState(body); setMessages((current) => [...current, { role: "system", content: "报价已选择，正在检查余额和 Allowance。" }]); }
    catch (error) { setMessages((current) => [...current, { role: "assistant", content: `选择报价失败：${error instanceof Error ? error.message : String(error)}` }]); }
  }
  function applyState(value: any) {
    const eventName = value?.event;
    const current = value?.state || value?.data?.state || value?.data || value;
    if (eventName === "progress" || current?.progress) {
      const progressValue = current?.progress || current?.data || current;
      const progressText = progressValue?.message || progressValue?.stage;
      if (progressText) setProgress((items) => [...items, String(progressText)].slice(-20));
    }
    if (current?.quote_candidates) setQuotes(current.quote_candidates);
    if (current?.selected_provider_reference) setSelectedQuote(current.selected_provider_reference);
    if (current?.pending_transaction) { setPendingTransaction(current.pending_transaction); setPendingKind(current?.response?.kind === "transfer_prepare" ? "transfer" : "swap"); }
    if (current?.broadcast_tx_hash || current?.tx_hash) setBroadcastHash(current.broadcast_tx_hash || current.tx_hash);
    if (current?.approval_transaction) setApproval(current.approval_transaction);
    if (current?.confirmation_state) setConfirmation(current.confirmation_state);
    if (current?.preflight) setPreflight(current.preflight);
    const response = current?.response?.response || current?.response || current;
    if (eventName === "error") setMessages((previous) => [...previous, { role: "assistant", content: current?.error?.message || responseMessage(current?.error) || "Agent 执行失败。" }]);
    if (response?.kind === "confirmation_required" && response.confirmation) setConfirmation(response.confirmation);
    if (response?.kind === "wallet_query") setWalletData(response.wallet || response);
    if (response?.kind === "portfolio_query") setPortfolioData(response.portfolio || response);
    if (response?.kind === "price_query") setPriceData(response);
    if (response?.kind === "gas_check") setGasData(response);
    if (response?.kind === "asset_discovery") setAssetsData(response);
    if (response?.kind === "clarification") { setSuggestions(response.suggestions || []); setTokenCandidates(response.token_candidates || []); }
    if (response?.kind === "swap_status" || response?.kind === "transaction_status") setStatus(response);
    const summary = response?.kind === "swap_quote"
      ? (response.quotes?.length ? `我找到了 ${response.quotes.length} 个兑换报价，请比较后选择一个 Provider。` : "暂时没有找到可用的兑换报价。")
      : formatResponseSummary(response);
    if (summary) {
      const key = `${response.kind || "response"}:${summary}`;
      if (!responseKeysRef.current.has(key)) {
        responseKeysRef.current.add(key);
        setMessages((previous) => [...previous, { role: "assistant", content: summary }]);
      }
    }
  }
  async function send(event?: FormEvent) {
    event?.preventDefault();
    const text = message.trim(); if (!text || busy) return;
    setMessages((current) => [...current, { role: "user", content: text }]); setMessage(""); setLastMessage(text); setBusy(true); abortRef.current = new AbortController();
    try {
      if (/^(取消|停止|退出)(兑换|交易)?$/.test(text)) {
        if (sessionId) await requestApi(`/v1/swap/${sessionId}/cancel`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo" }) });
        setMessages((current) => [...current, { role: "system", content: sessionId ? "已取消当前兑换。你可以随时重新发起新的兑换。" : "当前没有进行中的兑换。" }]);
      } else if (/^(选|选择)(第)?一|1号|第一个/.test(text) && quotes.length) {
        await selectQuote(quotes[0].provider_reference || "");
      } else await sendText(text);
    } catch (error) { if ((error as Error).name !== "AbortError") setMessages((current) => [...current, { role: "assistant", content: `请求失败：${error instanceof Error ? error.message : String(error)}` }]); } finally { setBusy(false); abortRef.current = null; }
  }
  async function cancelRequest() {
    if (sessionId) {
      try { await requestApi(`/v1/swap/${sessionId}/cancel`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo" }) }); }
      catch (error) { setMessages((current) => [...current, { role: "assistant", content: `取消兑换失败：${error instanceof Error ? error.message : String(error)}` }]); return; }
    }
    abortRef.current?.abort(); setBusy(false); setMessages((current) => [...current, { role: "system", content: sessionId ? "已取消当前兑换和请求。" : "已取消本次请求。" }]);
  }
  async function chooseSuggestion(text: string, data?: Record<string, unknown>) { if (busy) return; setMessage(text); setMessages((current) => [...current, { role: "user", content: text }]); setLastMessage(text); setBusy(true); abortRef.current = new AbortController(); try { await sendText(text, false, data ? { suggestion_data: data } : {}); } catch (error) { if ((error as Error).name !== "AbortError") setMessages((current) => [...current, { role: "assistant", content: `请求失败：${error instanceof Error ? error.message : String(error)}` }]); } finally { setBusy(false); abortRef.current = null; setMessage(""); } }
  async function chooseToken(candidate: any) {
    const side = candidate.side === "source" ? "来源" : "目标";
    await chooseSuggestion(`${side} Token 合约地址是 ${candidate.address}，精度是 ${candidate.decimals}`);
  }
  async function retryLastMessage() {
    if (!lastMessage || busy) return;
    setBusy(true); abortRef.current = new AbortController();
    try { await sendText(lastMessage); }
    catch (error) { if ((error as Error).name !== "AbortError") setMessages((current) => [...current, { role: "assistant", content: `请求失败：${error instanceof Error ? error.message : String(error)}` }]); }
    finally { setBusy(false); abortRef.current = null; }
  }
  useEffect(() => {
    if (!messagesRef.current) return;
    const nearBottom = messagesRef.current.scrollHeight - messagesRef.current.scrollTop - messagesRef.current.clientHeight < 64;
    if (nearBottom) messagesRef.current.scrollTop = messagesRef.current.scrollHeight;
  }, [messages]);
  return <main>
    <header><h1>Wallet Agent Demo</h1><p>通过自然语言查询余额、兑换报价并准备交易。私钥不会发送到服务端。</p><div className="wallet-bar"><span className="wallet-status">{wallet ? `已连接：${wallet.chain} ${wallet.address.slice(0, 6)}…${wallet.address.slice(-4)}` : "未连接浏览器钱包"}</span><input className="token" type="password" placeholder="Bearer token（可选）" value={token} onChange={(event) => setToken(event.target.value)} /><button onClick={() => void connectWallet()}>{wallet ? "重新连接" : "连接钱包"}</button><button className="secondary" onClick={() => void refreshSession()}>刷新</button><button className="secondary" onClick={() => { setHistoryOpen(!historyOpen); if (!historyOpen) void loadHistory(); }}>历史</button></div></header>
    {historyOpen && <section className="panel"><div className="toolbar"><button className="secondary" onClick={() => void loadHistory()}>重新加载</button><button className="secondary" onClick={startNew}>新建对话</button></div><div className="list">{conversations.length === 0 ? <p className="muted">暂无已保存的对话。</p> : conversations.map((item) => <div key={item.conversation_id} className="conversation"><button onClick={() => void loadConversation(item.conversation_id)}><strong>{item.summary}</strong><span>{new Date(item.updated_at).toLocaleString()} · {item.status}</span></button><button className="danger delete" onClick={() => void removeConversation(item.conversation_id)}>删除</button></div>)}</div></section>}
    <section className="panel"><div className="toolbar"><button className="secondary" onClick={() => void runDebugSmoke()}>联调检查</button><button className="secondary" onClick={() => void runAgentTests()}>测试全部 Agent 功能</button></div>{Object.keys(agentTests).length > 0 && <div className="list">{AGENT_TESTS.map((item) => <div className="conversation" key={item.id}><strong>{item.name} · {item.intent}</strong><span>{agentTests[item.id] || "未运行"}</span></div>)}</div>}</section>
    <div ref={messagesRef} id="messages">
      {messages.map((item, index) => <div className={`message ${item.role}`} key={`${index}-${item.content}`}><div className="bubble">{item.content}</div></div>)}
      {quotes.length > 0 && <section className="card"><h3>报价列表</h3>{quotes.map((quote) => <div className="quote" key={quote.provider_reference}><strong>{quote.provider} · {quote.provider_reference}</strong><span>{quote.input_amount} {quote.source_asset?.symbol} → {quote.expected_output} {quote.destination_asset?.symbol}</span><button className="secondary" disabled={!quote.provider_reference} onClick={() => void selectQuote(quote.provider_reference!)}>{selectedQuote === quote.provider_reference ? "已选择" : "选择此报价"}</button></div>)}</section>}
      {confirmation?.status === "requested" && <section className="card"><h3>请确认兑换</h3><p>{confirmation.summary?.input_amount} {confirmation.summary?.source_asset?.symbol} → {confirmation.summary?.expected_output} {confirmation.summary?.destination_asset?.symbol}{confirmation.summary?.provider ? `\nProvider：${confirmation.summary.provider}` : ""}{confirmation.expires_at ? `\n有效期至：${confirmation.expires_at}` : ""}</p><div className="card-actions"><button onClick={async () => { try { const body = await requestApi(`/v1/swap/${sessionId}/confirm`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", approved: true }) }); applyState(body); } catch (error) { setMessages((current) => [...current, { role: "assistant", content: `确认兑换失败：${error instanceof Error ? error.message : String(error)}` }]); } }}>确认兑换</button><button className="secondary" onClick={async () => { try { const body = await requestApi(`/v1/swap/${sessionId}/confirm`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", approved: false }) }); applyState(body); } catch (error) { setMessages((current) => [...current, { role: "assistant", content: `取消兑换失败：${error instanceof Error ? error.message : String(error)}` }]); } }}>取消</button></div></section>}
      {preflight && <section className="card"><h3>{preflight.ok ? "交易预检查通过" : "交易预检查未通过"}</h3><p>{preflight.gas_sources ? `Gas 来源：${Object.entries(preflight.gas_sources).map(([name, used]) => `${name}=${used ? "已用" : "未用"}`).join("、")}\n` : ""}{preflight.simulation?.success === false ? `模拟警告：${preflight.simulation.error || preflight.simulation.fail_reason || "simulation failed"}\n` : ""}{(preflight.checks || []).map((item: any) => `${item.status === "passed" ? "✓" : item.status === "warning" ? "!" : "×"} ${item.message}`).join("\n")}</p></section>}
      {approval && <section className="card"><h3>需要 Approve 授权</h3><p>请确认授权额度，浏览器钱包会在本地弹窗签名。</p><pre className="debug">{JSON.stringify(approval, null, 2)}</pre><button onClick={async () => { try { const hash = await sendTransaction(approval); const body = await requestApi(`/v1/swap/${sessionId}/approve-broadcast`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", chain: approval.chain, approve_tx_hash: hash }) }); applyState(body); await waitForApproval(); } catch (error) { setMessages((current) => [...current, { role: "assistant", content: `Approve 失败：${error instanceof Error ? error.message : String(error)}` }]); } }}>钱包签名并广播 Approve</button></section>}
      {pendingTransaction && <section className="card"><h3>{pendingKind === "transfer" ? "转账交易已准备好" : "兑换交易已准备好"}</h3><p>{pendingKind === "transfer" ? "请确认收款地址和金额，钱包会在当前转账链本地签名并广播。" : "请在钱包中确认未签名交易。"}</p><pre className="debug">{JSON.stringify(pendingTransaction, null, 2)}</pre><div className="card-actions"><button onClick={async () => { try { const hash = await sendTransaction(pendingTransaction); const path = pendingKind === "transfer" ? `/v1/transfer/${sessionId}/broadcast` : `/v1/swap/${sessionId}/broadcast`; const body = await requestApi(path, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", chain: pendingTransaction.chain, tx_hash: hash }) }); setBroadcastHash(hash); applyState({ ...body, tx_hash: hash }); } catch (error) { setMessages((current) => [...current, { role: "assistant", content: `${pendingKind === "transfer" ? "转账" : "Swap"} 失败：${error instanceof Error ? error.message : String(error)}` }]); } }}>钱包签名并广播{pendingKind === "transfer" ? "转账" : " Swap"}</button>{pendingKind === "transfer" && broadcastHash && <button className="secondary" onClick={async () => { try { const body = await requestApi(`/v1/transactions/${encodeURIComponent(pendingTransaction.chain)}/${encodeURIComponent(broadcastHash)}`, { headers }); applyState({ kind: "transaction_status", ...body }); } catch (error) { setMessages((current) => [...current, { role: "assistant", content: `查询交易状态失败：${error instanceof Error ? error.message : String(error)}` }]); } }}>查询交易状态</button>}</div></section>}
      {progress.length > 0 && <section className="card"><h3>处理过程</h3><p>{progress.join("\n")}</p></section>}
      {suggestions.length > 0 && <section className="card"><h3>建议</h3><div className="card-actions">{suggestions.map((item, index) => <button className="secondary" key={`${item.label || item.message}-${index}`} onClick={() => void chooseSuggestion(item.message || item.label || "", item.data)}>{item.label || item.message}</button>)}</div></section>}
      {tokenCandidates.length > 0 && <section className="card"><h3>请选择 Token</h3><div className="card-actions">{tokenCandidates.map((item, index) => <button className="secondary" key={`${item.address}-${index}`} onClick={() => void chooseToken(item)}>{index + 1}. {item.symbol} · {item.chain} · {String(item.address).slice(0, 8)}…</button>)}</div></section>}
      {walletData && <section className="card"><h3>钱包余额</h3><pre className="debug">{JSON.stringify(walletData, null, 2)}</pre></section>}
      {portfolioData && <section className="card"><h3>资产组合</h3><pre className="debug">{JSON.stringify(portfolioData, null, 2)}</pre></section>}
      {priceData && <section className="card"><h3>价格与市场数据</h3><pre className="debug">{JSON.stringify(priceData, null, 2)}</pre></section>}
      {gasData && <section className="card"><h3>Gas 检查</h3><pre className="debug">{JSON.stringify(gasData, null, 2)}</pre></section>}
      {assetsData && <section className="card"><h3>Token 资产发现</h3><pre className="debug">{JSON.stringify(assetsData, null, 2)}</pre></section>}
      {status && <section className="card"><h3>订单状态</h3><p>{status.message || responseMessage(status) || status.status?.status || "处理中"}</p><pre className="debug">{JSON.stringify(status, null, 2)}</pre></section>}
    </div>
    <details className="panel"><summary>后端调试数据（{debug.length} 条）</summary><div className="toolbar"><button className="secondary" onClick={() => navigator.clipboard?.writeText(debug.join("\n\n"))}>复制调试数据</button><button className="secondary" onClick={() => setDebug([])}>清空</button></div><pre className="debug">{debug.join("\n\n") || "等待后端响应…"}</pre></details>
    <form className="composer" onSubmit={send}><div className="composer-row"><textarea value={message} onChange={(event) => setMessage(event.target.value)} placeholder="例如：把 1 USDC 从 Base 换成 BSC 上的 USDT" disabled={busy} /><button disabled={busy}>{busy ? "处理中…" : "发送"}</button></div><div className="toolbar"><span className="muted">{busy ? "正在处理…" : "就绪"}</span>{busy && <button type="button" className="secondary" onClick={cancelRequest}>取消请求</button>}{!busy && lastMessage && <button type="button" className="secondary" onClick={() => void retryLastMessage()}>重试</button>}</div><div className="meta"><span>conversation: {conversationId ?? "新会话"}</span><span>session: {sessionId ?? "未创建"}</span></div></form>
  </main>;
}
