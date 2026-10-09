"use client";

import { FormEvent, ReactNode, useEffect, useRef, useState } from "react";
import { apiErrorMessage, buildReadOnlyTimeline, classifyAgentTest, ConversationArtifacts, extractConversationArtifacts, extractReadOnlyTrace, formatModelTraceEntry, formatProcessingDuration, formatResponseSummary, latestProgressText, mergeModelTrace, parseSseFrame, parseSseFrames, providerPriority as getProviderPriority, ReadOnlyTrace, responseMessage, restoreConversationMessages, sanitizeDebug, transactionChainId } from "../lib/agent-utils";

type Message = { id?: string; role: "user" | "assistant" | "system"; content: string; fullContent?: string; typing?: boolean; suggestions?: any[]; artifacts?: ConversationArtifacts; responseKey?: string };
type ProgressEntry = { text: string; elapsed?: number; model?: ModelTraceEntry };
type ModelTraceEntry = { call_id?: string; role?: string; model_id?: string; task_kind?: string; stage?: string; timestamp?: string; elapsed_ms?: number; tool_call_ids?: string[]; prompt_tokens?: number; completion_tokens?: number; total_tokens?: number; cached_tokens?: number; finish_reason?: string };
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

function renderInlineMarkdown(text: string): ReactNode[] {
  const result: ReactNode[] = [];
  const pattern = /(\[[^\]]+\]\((https?:\/\/[^\s)]+)\)|`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|_[^_\n]+_)/g;
  let cursor = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  const appendText = (value: string) => {
    value.split("\n").forEach((line, index) => {
      if (index > 0) result.push(<br key={`br-${key++}`} />);
      if (line) result.push(line);
    });
  };
  while ((match = pattern.exec(text)) !== null) {
    appendText(text.slice(cursor, match.index));
    const token = match[0];
    if (token.startsWith("[")) {
      const link = token.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/);
      if (link) result.push(<a key={`link-${key++}`} href={link[2]} target="_blank" rel="noreferrer">{link[1]}</a>);
    } else if (token.startsWith("`")) {
      result.push(<code key={`code-${key++}`}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith("**") || token.startsWith("__")) {
      result.push(<strong key={`strong-${key++}`}>{token.slice(2, -2)}</strong>);
    } else {
      result.push(<em key={`em-${key++}`}>{token.slice(1, -1)}</em>);
    }
    cursor = match.index + token.length;
  }
  appendText(text.slice(cursor));
  return result;
}

function markdownCells(line: string): string[] {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function isMarkdownTableSeparator(line: string): boolean {
  const cells = markdownCells(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function renderMarkdown(content: string): JSX.Element {
  const lines = String(content || "").replace(/\r\n?/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let index = 0;
  let key = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (/^\s*```/.test(line)) {
      const language = line.trim().slice(3).trim();
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !/^\s*```/.test(lines[index])) code.push(lines[index++]);
      if (index < lines.length) index += 1;
      blocks.push(<pre className="markdown-code" key={`code-block-${key++}`}><code data-language={language || undefined}>{code.join("\n")}</code></pre>);
      continue;
    }
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const heading = line.match(/^\s*(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      const Heading = `h${heading[1].length}` as keyof JSX.IntrinsicElements;
      blocks.push(<Heading key={`heading-${key++}`}>{renderInlineMarkdown(heading[2])}</Heading>);
      index += 1;
      continue;
    }
    if (index + 1 < lines.length && line.includes("|") && isMarkdownTableSeparator(lines[index + 1])) {
      const headers = markdownCells(line);
      const rows: string[][] = [];
      index += 2;
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        rows.push(markdownCells(lines[index++]));
      }
      blocks.push(<div className="markdown-table-wrap" key={`table-${key++}`}><table><thead><tr>{headers.map((cell, cellIndex) => <th key={`th-${cellIndex}`}>{renderInlineMarkdown(cell)}</th>)}</tr></thead><tbody>{rows.map((row, rowIndex) => <tr key={`tr-${rowIndex}`}>{headers.map((_header, cellIndex) => <td key={`td-${rowIndex}-${cellIndex}`}>{renderInlineMarkdown(row[cellIndex] || "")}</td>)}</tr>)}</tbody></table></div>);
      continue;
    }
    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    if (unordered) {
      const items: string[] = [];
      while (index < lines.length) {
        const item = lines[index].match(/^\s*[-*+]\s+(.+)$/);
        if (!item) break;
        items.push(item[1]);
        index += 1;
      }
      blocks.push(<ul key={`ul-${key++}`}>{items.map((item, itemIndex) => <li key={`li-${itemIndex}`}>{renderInlineMarkdown(item)}</li>)}</ul>);
      continue;
    }
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (ordered) {
      const items: string[] = [];
      while (index < lines.length) {
        const item = lines[index].match(/^\s*\d+[.)]\s+(.+)$/);
        if (!item) break;
        items.push(item[1]);
        index += 1;
      }
      blocks.push(<ol key={`ol-${key++}`}>{items.map((item, itemIndex) => <li key={`li-${itemIndex}`}>{renderInlineMarkdown(item)}</li>)}</ol>);
      continue;
    }
    const paragraph: string[] = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !/^\s*(#{1,6})\s+/.test(lines[index]) && !/^\s*```/.test(lines[index]) && !/^\s*[-*+]\s+/.test(lines[index]) && !/^\s*\d+[.)]\s+/.test(lines[index])) paragraph.push(lines[index++]);
    blocks.push(<p key={`p-${key++}`}>{renderInlineMarkdown(paragraph.join("\n"))}</p>);
  }
  return <div className="markdown">{blocks}</div>;
}

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
  if (!response.ok) {
    const error = new Error(apiErrorMessage(body));
    (error as Error & { apiBody?: unknown }).apiBody = body;
    throw error;
  }
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
  const cancellingRef = useRef(false);
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
  const [readOnlyTrace, setReadOnlyTrace] = useState<ReadOnlyTrace | null>(null);
  const [backendDuration, setBackendDuration] = useState<number | null>(null);
  const [backendStage, setBackendStage] = useState("等待请求");
  const [modelTrace, setModelTrace] = useState<ModelTraceEntry[]>([]);
  const [agentTests, setAgentTests] = useState<Record<string, string>>({});
  const [historyOpen, setHistoryOpen] = useState(false);
  const messagesRef = useRef<HTMLDivElement>(null);
  const responseKeysRef = useRef<Set<string>>(new Set());

  function log(label: string, value: unknown) {
    setDebug((current) => [...current, `[${new Date().toISOString()}] ${label}\n${JSON.stringify(sanitizeDebug(value), null, 2)}`].slice(-100));
  }
  const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  async function loadHistory() {
    try {
      const body = await requestApi("/v1/agent/conversations?user_id=browser-demo", { headers });
      const history = Array.isArray(body.conversations) ? body.conversations : [];
      history.sort((left: Conversation, right: Conversation) => {
        const rightTime = Date.parse(right.updated_at);
        const leftTime = Date.parse(left.updated_at);
        return (Number.isFinite(rightTime) ? rightTime : 0) - (Number.isFinite(leftTime) ? leftTime : 0);
      });
      setConversations(history);
      log("CONVERSATIONS", { ...body, conversations: history });
    }
    catch (error) { setMessages((current) => [...current, { role: "assistant", content: `读取历史失败：${error instanceof Error ? error.message : String(error)}` }]); }
  }
  async function loadConversation(id: string) {
    try {
      clearConversationState();
      const body = await requestApi(`/v1/agent/conversations/${encodeURIComponent(id)}?user_id=browser-demo`, { headers });
      const latestArtifacts = body.latest_artifacts || extractConversationArtifacts({ ...body.session, model_trace: body.model_trace });
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
  function clearConversationState() { abortRef.current?.abort(); abortRef.current = null; setBusy(false); setActionLoading(null); endProcessing(); setConversationId(null); setSessionId(null); setQuotes([]); setSelectedQuote(null); setPendingTransaction(null); setPendingKind("swap"); setBroadcastHash(null); setConfirmation(null); setApproval(null); setPreflight(null); setStatus(null); setWalletData(null); setPortfolioData(null); setPriceData(null); setGasData(null); setAssetsData(null); setSuggestions([]); setTokenCandidates([]); setProgress([]); setReadOnlyTrace(null); setModelTrace([]); setBackendDuration(null); setBackendStage("等待请求"); responseKeysRef.current.clear(); }
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
    setReadOnlyTrace(artifacts?.readOnly || null);
    setModelTrace((items) => mergeModelTrace(items, artifacts?.model_trace));
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
      content,
      fullContent: content,
      typing: content.length > 1,
      suggestions,
      artifacts: artifacts || undefined,
      responseKey,
    }]);
  }
  function confirmationKey(value: any) {
    return value?.payload_hash || value?.provider_reference || value?.quote?.provider_reference || [value?.task_id, value?.task_revision, value?.summary?.provider_reference].filter(Boolean).join(":") || JSON.stringify({
      provider: value?.summary?.provider,
      input_amount: value?.summary?.input_amount,
      source: value?.summary?.source_asset?.symbol,
      destination: value?.summary?.destination_asset?.symbol,
      expected_output: value?.summary?.expected_output,
    });
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
    setReadOnlyTrace(null);
    // Keep the conversation-wide timeline; only the progress text is turn-scoped.
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
        const streamedModels = payload?.data?.model_trace || payload?.model_trace;
        if (Array.isArray(streamedModels)) {
          const newModels = streamedModels as ModelTraceEntry[];
          setModelTrace((items) => mergeModelTrace(items, newModels));
          if (render) setProgress((items) => {
            const known = new Set(items.map((item) => item.model?.call_id).filter(Boolean));
            const calls = newModels.filter((item) => item.model_id && !known.has(item.call_id));
            return [...items, ...calls.map((model) => ({
              text: model.stage || model.task_kind || model.role || "模型调用",
              model,
            }))];
          });
        }
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
    if (render) {
      log("SSE STREAM SUMMARY", {
        run_id: runId,
        event_count: events.length,
        last_event_id: lastEventId || null,
        terminal_received: terminalReceived,
        response_kind: finalResponse?.kind || null,
        assistant_text_seen: Boolean(assistant),
        duration_ms: Math.round(performance.now() - requestStartedAt),
      });
    }
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
      try {
        const body = await requestApi(`/v1/swap/${sessionId}/confirm`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", approved }) });
        log("CONFIRM RESPONSE", body);
        applyState(body);
        const response = body?.response?.response || body?.response || body;
        const kind = response?.kind || body?.stage || body?.status;
        if (kind === "approval_required" || body?.stage === "approval_required") {
          setMessages((current) => [...current, { role: "system", content: "兑换已确认，需要先完成 Token Approve 授权。" }]);
        } else if (kind === "swap_prepare" || body?.pending_transaction) {
          setMessages((current) => [...current, { role: "system", content: "兑换已确认，交易已准备好，请在钱包中签名。" }]);
        } else if (response?.message) {
          setMessages((current) => [...current, { role: "system", content: String(response.message) }]);
        }
      }
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
    const stateModels = current?.model_trace;
    if (Array.isArray(stateModels)) setModelTrace((items) => mergeModelTrace(items, stateModels));
    const trace = extractReadOnlyTrace(current);
    if (trace) setReadOnlyTrace(trace);
    if (eventName === "progress" || current?.progress) {
      const progressValue = current?.progress || current?.data || current;
      const progressText = progressValue?.message || progressValue?.stage;
      const elapsed = Number(progressValue?.elapsed_ms);
      if (Number.isFinite(elapsed)) setBackendDuration(elapsed);
      if (progressText) {
        const text = String(progressText);
        const line = `${text}${Number.isFinite(elapsed) ? ` · ${elapsed} ms` : ""}`;
        setBackendStage(text);
        setProgress((items) => items.some((item) => item.text === text && !item.model) ? items : [...items, { text, elapsed: Number.isFinite(elapsed) ? elapsed : undefined }].slice(-40));
      }
    }
    const eventModels = value?.data?.model_trace || value?.model_trace;
    if (Array.isArray(eventModels)) setModelTrace((items) => mergeModelTrace(items, eventModels));
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
    const artifacts = response ? extractConversationArtifacts({ ...current, response, model_trace: mergeModelTrace(modelTrace, current?.model_trace) }) : null;
    if (response) applyActiveArtifacts(artifacts);
    const summary = formatResponseSummary(response);
    const finalMessage = response?.message || summary;
    log("SSE RESPONSE RESOLUTION", {
      event: eventName || "snapshot",
      response_kind: response?.kind || null,
      response_message_present: Boolean(response?.message),
      summary_present: Boolean(summary),
      final_message_present: Boolean(finalMessage),
      artifact_types: artifacts ? Object.keys(artifacts).filter((key) => key !== "response") : [],
      current_keys: current && typeof current === "object" ? Object.keys(current).slice(0, 30) : [],
    });
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
    if (cancellingRef.current) return;
    cancellingRef.current = true;
    const activeSessionId = sessionId;
    abortRef.current?.abort();
    abortRef.current = null;
    setBusy(false);
    endProcessing();
    setMessages((current) => [...current, { role: "system", content: activeSessionId ? "已取消当前兑换和请求。" : "已取消本次请求。" }]);
    try {
      if (activeSessionId) {
        await requestApi(`/v1/swap/${activeSessionId}/cancel`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo" }) });
      }
    } catch (error) {
      setMessages((current) => [...current, { role: "system", content: `本地请求已停止，但后端取消同步失败：${error instanceof Error ? error.message : String(error)}` }]);
    } finally {
      cancellingRef.current = false;
    }
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
  const activeConfirmationKey = confirmation?.status === "requested" ? confirmationKey(confirmation) : null;
  const confirmationIndexes = new Map<string, number>();
  messages.forEach((messageItem, messageIndex) => {
    const candidate = messageItem.role === "assistant" ? messageItem.artifacts?.confirmation : null;
    if (candidate?.status === "requested") confirmationIndexes.set(confirmationKey(candidate), messageIndex);
  });
  function renderConversationCards(artifacts: ConversationArtifacts, interactive: boolean, showConfirmation = true) {
    const cardQuotes = artifacts.quotes || [];
    const cardConfirmation = artifacts.confirmation;
    const cardPreflight = artifacts.preflight;
    const cardApproval = artifacts.approval;
    const cardTransaction = artifacts.pending_transaction;
    const cardPendingKind = artifacts.pending_kind || "swap";
    const cardTokenCandidates = artifacts.token_candidates || [];
    const cardStatus = artifacts.status;
    const reactResults = artifacts.reactResults || [];
    const readOnlyTimeline = artifacts.readOnly
      ? buildReadOnlyTimeline(artifacts.model_trace || [], artifacts.readOnly.toolCalls)
      : [];
    const timelineMeta = (item: ReturnType<typeof buildReadOnlyTimeline>[number]) => [
      item.model_id,
      item.call_id ? `call_id ${item.call_id}` : null,
      item.status,
      item.elapsed_ms != null ? `${item.elapsed_ms} ms` : null,
    ].filter(Boolean).join(" · ");
    const timelineTitle = (item: ReturnType<typeof buildReadOnlyTimeline>[number]) => {
      if (item.kind === "model") return item.tool_call_ids?.length ? "模型决定调用工具" : item.label || "模型调用";
      if (item.kind === "tool") return `执行 ${item.name || "只读工具"}`;
      return `ToolMessage 已返回${item.name ? ` · ${item.name}` : ""}`;
    };
    return <>
      {cardQuotes.length > 0 && <section className="card" data-card-kind="quotes"><h3>报价列表</h3>{cardQuotes.map((quote: Quote) => <div className="quote" key={quote.provider_reference}><strong>{quote.provider} · {quote.provider_reference}</strong><span>{quote.input_amount} {quote.source_asset?.symbol} → {quote.expected_output} {quote.destination_asset?.symbol}</span>{interactive && <button className="secondary" disabled={!quote.provider_reference || Boolean(actionLoading) || busy} onClick={() => void selectQuote(quote.provider_reference!)}>{actionLoading === `quote:${quote.provider_reference}` ? <><i className="loading-spinner" />处理中…</> : selectedQuote === quote.provider_reference ? "已选择" : "选择此报价"}</button>}</div>)}</section>}
      {showConfirmation && cardConfirmation?.status === "requested" && <section className="card" data-card-kind="confirmation" data-confirmation-key={confirmationKey(cardConfirmation)}><h3>请确认兑换</h3><p>{cardConfirmation.summary?.input_amount} {cardConfirmation.summary?.source_asset?.symbol} → {cardConfirmation.summary?.expected_output} {cardConfirmation.summary?.destination_asset?.symbol}{cardConfirmation.summary?.provider ? `\nProvider：${cardConfirmation.summary.provider}` : ""}{cardConfirmation.expires_at ? `\n有效期至：${cardConfirmation.expires_at}` : ""}</p>{interactive && <div className="card-actions"><button disabled={Boolean(actionLoading) || busy} onClick={() => void confirmSwap(true)}>{actionLoading === "confirm" ? <><i className="loading-spinner" />处理中…</> : "确认兑换"}</button><button className="secondary" disabled={Boolean(actionLoading) || busy} onClick={() => void confirmSwap(false)}>{actionLoading === "cancel-confirmation" ? <><i className="loading-spinner" />处理中…</> : "取消"}</button></div>}</section>}
      {cardPreflight && <section className="card" data-card-kind="preflight"><h3>{cardPreflight.ok ? "交易预检查通过" : "交易预检查未通过"}</h3><p>{cardPreflight.gas_sources ? `Gas 来源：${Object.entries(cardPreflight.gas_sources).map(([name, used]) => `${name}=${used === null || used === undefined ? "未返回" : used === "unavailable" ? "不可用" : used === "failed" ? "失败" : "成功"}`).join("、")}\n` : ""}{cardPreflight.simulation?.success === false ? `模拟警告：${cardPreflight.simulation.error || cardPreflight.simulation.failure_reason || cardPreflight.simulation.fail_reason || "simulation failed"}\n` : ""}{(cardPreflight.checks || []).map((item: any) => `${item.status === "passed" ? "✓" : item.status === "warning" ? "!" : "×"} ${item.message}`).join("\n")}</p></section>}
      {cardApproval && <section className="card" data-card-kind="approval"><h3>需要 Approve 授权</h3><p>请确认授权额度，浏览器钱包会在本地弹窗签名。</p><pre className="debug">{JSON.stringify(cardApproval, null, 2)}</pre>{interactive && <button disabled={Boolean(actionLoading) || busy} onClick={() => void approveSwap()}>{actionLoading === "approve" ? <><i className="loading-spinner" />处理中…</> : "钱包签名并广播 Approve"}</button>}</section>}
      {cardTransaction && <section className="card" data-card-kind="transaction"><h3>{cardPendingKind === "transfer" ? "转账交易已准备好" : "兑换交易已准备好"}</h3><p>{cardPendingKind === "transfer" ? "请确认收款地址和金额，钱包会在当前转账链本地签名并广播。" : "请在钱包中确认未签名交易。"}</p><pre className="debug">{JSON.stringify(cardTransaction, null, 2)}</pre>{interactive && <div className="card-actions"><button disabled={Boolean(actionLoading) || busy} onClick={() => void broadcastPendingTransaction()}>{actionLoading === "broadcast" ? <><i className="loading-spinner" />处理中…</> : `钱包签名并广播${cardPendingKind === "transfer" ? "转账" : " Swap"}`}</button>{cardPendingKind === "transfer" && broadcastHash && <button className="secondary" disabled={Boolean(actionLoading) || busy} onClick={() => void queryPendingTransaction()}>{actionLoading === "transaction-status" ? <><i className="loading-spinner" />处理中…</> : "查询交易状态"}</button>}</div>}</section>}
      {cardTokenCandidates.length > 0 && <section className="card" data-card-kind="tokens"><h3>请选择 Token</h3><div className="card-actions">{cardTokenCandidates.map((item, index) => interactive ? <button className="secondary" key={`${item.address}-${index}`} onClick={() => void chooseToken(item)}>{index + 1}. {item.symbol} · {item.chain} · {String(item.address).slice(0, 8)}…</button> : <span key={`${item.address}-${index}`}>{index + 1}. {item.symbol} · {item.chain}</span>)}</div></section>}
      {artifacts.wallet && <section className="card" data-card-kind="wallet"><h3>钱包余额</h3><pre className="debug">{JSON.stringify(artifacts.wallet, null, 2)}</pre></section>}
      {artifacts.portfolio && <section className="card" data-card-kind="portfolio"><h3>资产组合</h3><pre className="debug">{JSON.stringify(artifacts.portfolio, null, 2)}</pre></section>}
      {artifacts.price && <section className="card" data-card-kind="price"><h3>价格与市场数据</h3><pre className="debug">{JSON.stringify(artifacts.price, null, 2)}</pre></section>}
      {artifacts.assetValue && <section className="card" data-card-kind="asset-value"><h3>资产估值</h3><p>{responseMessage(artifacts.assetValue)}</p><pre className="debug">{JSON.stringify(artifacts.assetValue, null, 2)}</pre></section>}
      {artifacts.gas && <section className="card" data-card-kind="gas"><h3>Gas 检查</h3><pre className="debug">{JSON.stringify(artifacts.gas, null, 2)}</pre></section>}
      {artifacts.assets && <section className="card" data-card-kind="assets"><h3>Token 资产发现</h3><pre className="debug">{JSON.stringify(artifacts.assets, null, 2)}</pre></section>}
      {cardStatus && <section className="card" data-card-kind="status"><h3>订单状态</h3><p>{cardStatus.message || responseMessage(cardStatus) || cardStatus.status?.status || "处理中"}</p><pre className="debug">{JSON.stringify(cardStatus, null, 2)}</pre></section>}
      {reactResults.length > 0 && <section className="card" data-card-kind="react-results"><div className="progress-heading"><h3>本轮工具结果</h3><span>{reactResults.length} 项</span></div><ol className="react-results-list">{reactResults.map((item, index) => <li key={`${item.tool || "tool"}-${index}`}><strong>{item.tool || "工具"}</strong><pre className="debug">{JSON.stringify(sanitizeDebug(item.result), null, 2)}</pre></li>)}</ol></section>}
      {(artifacts.route || artifacts.reactToolTimings?.length) && <section className="card react-observability" data-card-kind="react-observability"><div className="progress-heading"><h3>本轮处理信息</h3><span>{artifacts.route?.route_mode || "agent"}</span></div>{artifacts.route && <p className="muted">路由：{artifacts.route.graph_role || "conversation"} · {artifacts.route.route_reason || "model_answer"}</p>}{artifacts.reactToolTimings?.length ? <ol className="react-results-list">{artifacts.reactToolTimings.map((item, index) => <li key={`${item.call_id || item.tool || "tool"}-${index}`}><strong>{item.tool || "工具"}</strong><span>{item.status || "unknown"}{item.elapsed_ms != null ? ` · ${item.elapsed_ms} ms` : ""}{item.cache_status ? ` · cache ${item.cache_status}` : ""}{item.error_code ? ` · ${item.error_code}` : ""}</span></li>)}</ol> : null}</section>}
      {artifacts.model_trace?.length ? <section className="model-trace" data-testid="model-trace"><span>本轮模型</span><div>{artifacts.model_trace.map((item, index) => <code key={item.call_id || `${item.timestamp}-${item.role}-${index}`}>{formatModelTraceEntry({ ...item, stage: item.task_kind ? `${item.stage || item.role}:${item.task_kind}` : item.stage || item.role })}</code>)}</div></section> : null}
      {readOnlyTimeline.length > 0 && <section className="model-trace-panel" data-testid="tool-timeline"><div className="progress-heading"><h3>模型调用过程</h3><span>{readOnlyTimeline.filter((item) => item.kind === "tool").length} 个工具调用</span></div><ol className="timeline-list">{readOnlyTimeline.map((item) => <li className={`timeline-item ${item.kind}`} key={item.key}><i /><div><strong>{timelineTitle(item)}</strong><span>{timelineMeta(item)}</span>{item.kind === "tool" && item.args && <details><summary>查看参数</summary><pre className="debug">{JSON.stringify(sanitizeDebug(item.args), null, 2)}</pre></details>}{item.error && <p className="timeline-error">{item.error.message || item.error.code || String(item.error)}</p>}</div></li>)}</ol></section>}
    </>;
  }
  return <main>
    <header className="app-header"><div className="brand"><span className="brand-mark">W</span><div><h1>Wallet Agent</h1><p>智能钱包操作台 · 安全地查询、报价与准备交易</p></div></div><div className="wallet-bar"><span className={`wallet-status ${wallet ? "connected" : ""}`}><i />{wallet ? `${wallet.chain} · ${wallet.address.slice(0, 6)}…${wallet.address.slice(-4)}` : "未连接钱包"}</span><input className="token" type="password" placeholder="Bearer token（可选）" value={token} onChange={(event) => setToken(event.target.value)} /><button onClick={() => void connectWallet()}>{wallet ? "重新连接" : "连接钱包"}</button><button className="secondary" onClick={() => void refreshSession()}>刷新</button><button className="secondary" onClick={() => { setHistoryOpen(!historyOpen); if (!historyOpen) void loadHistory(); }}>历史</button></div></header>
    <div className="workspace">
      <section className="main-column">
        {historyOpen && <section className="panel"><div className="toolbar"><button className="secondary" onClick={() => void loadHistory()}>重新加载</button><button className="secondary" onClick={startNew}>新建对话</button></div><div className="list">{conversations.length === 0 ? <p className="muted">暂无已保存的对话。</p> : conversations.map((item) => <div key={item.conversation_id} className="conversation"><button onClick={() => void loadConversation(item.conversation_id)}><strong>{item.summary}</strong><span>{new Date(item.updated_at).toLocaleString()} · {item.status}</span></button><button className="danger delete" onClick={() => void removeConversation(item.conversation_id)}>删除</button></div>)}</div></section>}
        <section className="action-strip"><span>工具</span><button className="secondary" onClick={() => void runDebugSmoke()}>联调检查</button><button className="secondary" onClick={() => void runAgentTests()}>测试全部 Agent 功能</button></section>
        <div ref={messagesRef} id="messages">
      {messages.map((item, index) => {
        const itemConfirmation = item.role === "assistant" ? item.artifacts?.confirmation : null;
        const itemConfirmationKey = itemConfirmation?.status === "requested" ? confirmationKey(itemConfirmation) : null;
        const showConfirmation = Boolean(itemConfirmationKey && activeConfirmationKey && itemConfirmationKey === activeConfirmationKey && confirmationIndexes.get(itemConfirmationKey) === index);
        return <div className="message-group" key={item.id || `${index}-${item.content}`}><div className={`message ${item.role}`}><div className="message-stack">{item.role !== "system" && <div className="bubble">{renderMarkdown(item.content)}</div>}{item.suggestions?.length ? <div className="inline-suggestions"><span>建议操作</span><div className="card-actions">{item.suggestions.map((suggestion, suggestionIndex) => <button className="secondary" disabled={busy || Boolean(actionLoading)} key={`${suggestion.label || suggestion.message}-${suggestionIndex}`} onClick={() => void chooseSuggestion(suggestion.message || suggestion.label || "", suggestion.data)}>{busy || actionLoading ? <><i className="loading-spinner" />处理中…</> : suggestion.label || suggestion.message}</button>)}</div></div> : null}{item.role === "assistant" && item.artifacts && renderConversationCards(item.artifacts, index === lastAssistantIndex, showConfirmation)}</div></div></div>;
      })}
        </div>
        <form className="composer" onSubmit={send}><div className="composer-row"><textarea onKeyDown={handleComposerKeyDown} value={message} onChange={(event) => setMessage(event.target.value)} placeholder="输入消息，Enter 发送，Shift+Enter 换行" disabled={busy || Boolean(actionLoading)} /><button disabled={busy || Boolean(actionLoading)}>{busy ? <><i className="loading-spinner" />处理中…</> : "发送"}</button></div><div className="toolbar"><span className="muted">{processing ? `${processing.label} · ${formatProcessingDuration(processingElapsed)}` : "就绪"}</span>{busy && <button type="button" className="secondary" onClick={cancelRequest}>取消请求</button>}{!busy && !actionLoading && lastMessage && <button type="button" className="secondary" onClick={() => void retryLastMessage()}>重试</button>}</div><div className="meta"><span>conversation: {conversationId ?? "新会话"}</span><span>session: {sessionId ?? "未创建"}</span></div></form>
      </section>
      <aside className="side-column"><div className="side-card"><div className="side-title"><span>运行状态</span><b className={processing ? "busy" : "ready"}>{processing ? "处理中" : "就绪"}</b></div><div className="stat"><span>网络</span><strong>{wallet?.chain || "未连接"}</strong></div><div className="stat"><span>会话</span><strong>{sessionId ? "已建立" : "新会话"}</strong></div><div className="stat"><span>请求耗时</span><strong>{processing ? formatProcessingDuration(processingElapsed) : "--"}</strong></div><div className="stat"><span>后端耗时</span><strong>{backendDuration == null ? "--" : `${backendDuration} ms`}</strong></div><div className="stat"><span>当前阶段</span><strong className="stage-value">{processing?.label || latestProgressText(progress, backendStage)}</strong></div><div className="stat"><span>当前模型</span><strong className="stage-value" data-testid="current-model">{modelTrace.length ? modelTrace[modelTrace.length - 1]?.model_id : "等待模型调用"}</strong></div></div>{progress.length > 0 && <section className="side-card progress-card"><div className="progress-heading"><h3>处理过程</h3><span>{backendDuration == null ? "进行中" : `${backendDuration} ms`}</span></div><ol className="progress-list">{progress.map((item, index) => <li key={`${item.text}-${index}`}><i /> <span>{item.text}{item.model?.model_id ? ` · ${item.model.model_id}` : ""}{item.elapsed != null ? ` · ${item.elapsed} ms` : ""}</span></li>)}</ol></section>}{readOnlyTrace && <section className="side-card read-only-card" data-testid="read-only-trace"><div className="progress-heading"><h3>只读查询</h3><span>{readOnlyTrace.loopCount}/2 轮</span></div>{readOnlyTrace.reference && <p className="trace-reference">引用：{readOnlyTrace.reference.id || readOnlyTrace.reference.kind || "已解析"}</p>}<ol className="progress-list">{readOnlyTrace.toolCalls.map((call, index) => <li key={`${call.call_id || call.name || "tool"}-${index}`}><i className={call.status === "failed" || call.status === "rejected" ? "failed" : ""} /><span><strong>{call.name || "只读工具"}</strong>{call.status ? ` · ${call.status}` : " · 处理中"}{call.call_id ? ` · call_id ${call.call_id}` : ""}{call.elapsed_ms != null ? ` · ${call.elapsed_ms} ms` : ""}{call.args && <details><summary>查看参数</summary><pre className="debug">{JSON.stringify(sanitizeDebug(call.args), null, 2)}</pre></details>}{call.error && <em className="timeline-error">{call.error.message || call.error.code || String(call.error)}</em>}</span></li>)}</ol>{Object.keys(readOnlyTrace.facts).length > 0 && <details><summary>验证事实</summary><pre className="debug">{JSON.stringify(sanitizeDebug(readOnlyTrace.facts), null, 2)}</pre></details>}</section>}<details className="panel debug-panel"><summary>后端调试数据（{debug.length} 条）</summary><div className="toolbar"><button className="secondary" onClick={() => navigator.clipboard?.writeText(debug.join("\n\n"))}>复制</button><button className="secondary" onClick={() => setDebug([])}>清空</button></div><pre className="debug">{debug.join("\n\n") || "等待后端响应…"}</pre></details>{Object.keys(agentTests).length > 0 && <div className="side-card test-card"><div className="side-title"><span>Agent 测试</span><b>{Object.values(agentTests).filter((value) => value.startsWith("通过")).length}/{AGENT_TESTS.length}</b></div><div className="test-list">{AGENT_TESTS.map((item) => <div key={item.id}><span>{item.name}</span><em>{agentTests[item.id] || "未运行"}</em></div>)}</div></div>}</aside>
    </div>
  </main>;
}
