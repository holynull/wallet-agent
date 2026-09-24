"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

type Message = { role: "user" | "assistant" | "system"; content: string };
type Conversation = { conversation_id: string; session_id?: string; summary: string; status: string; updated_at: string };
type Provider = { request: (args: { method: string; params?: unknown[] }) => Promise<any>; on?: (event: string, callback: (...args: any[]) => void) => void };
type Quote = { provider?: string; provider_reference?: string; input_amount?: string; expected_output?: string; source_asset?: { symbol?: string }; destination_asset?: { symbol?: string } };

const API_BASE = process.env.NEXT_PUBLIC_AGENT_API_URL ?? "http://localhost:8000";

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
  const [debug, setDebug] = useState<string[]>([]);
  const [wallet, setWallet] = useState<{ provider: Provider; address: string; chainId: string; chain: string } | null>(null);
  const [quotes, setQuotes] = useState<Quote[]>([]);
  const [selectedQuote, setSelectedQuote] = useState<string | null>(null);
  const [pendingTransaction, setPendingTransaction] = useState<any>(null);
  const [confirmation, setConfirmation] = useState<any>(null);
  const [approval, setApproval] = useState<any>(null);
  const [preflight, setPreflight] = useState<any>(null);
  const [status, setStatus] = useState<any>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const messagesRef = useRef<HTMLDivElement>(null);

  function log(label: string, value: unknown) {
    setDebug((current) => [...current, `[${new Date().toISOString()}] ${label}\n${JSON.stringify(value, null, 2)}`].slice(-100));
  }
  const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  async function loadHistory() {
    const body = await requestApi("/v1/agent/conversations?user_id=browser-demo", { headers });
    setConversations(body.conversations ?? []); log("CONVERSATIONS", body);
  }
  async function loadConversation(id: string) {
    const body = await requestApi(`/v1/agent/conversations/${encodeURIComponent(id)}?user_id=browser-demo`, { headers });
    setConversationId(body.conversation_id); setSessionId(body.session_id ?? null); setMessages(body.messages ?? []); setHistoryOpen(false); log("CONVERSATION", body);
  }
  async function removeConversation(id: string) {
    if (!window.confirm("删除这条对话及其历史记录？")) return;
    await requestApi(`/v1/agent/conversations/${encodeURIComponent(id)}?user_id=browser-demo`, { method: "DELETE", headers });
    if (id === conversationId) startNew();
    await loadHistory();
  }
  function startNew() { setConversationId(null); setSessionId(null); setMessages([{ role: "assistant", content: "你好，我可以帮你查询余额、比较兑换报价，并准备未签名交易。告诉我你想做什么。" }]); }
  function chainName(id: string) { return ({ "0x1": "ETH", "0x38": "BSC", "0x89": "POLYGON", "0xa": "OPTIMISM", "0x2105": "BASE", "0xa4b1": "ARBITRUM" } as Record<string, string>)[id.toLowerCase()] ?? `EVM(${id})`; }
  function txValue(value: unknown) { if (value == null || value === "") return "0x0"; const text = String(value); return text.startsWith("0x") ? text : `0x${BigInt(text).toString(16)}`; }
  async function connectWallet() {
    const provider = (window as any).ethereum as Provider | undefined;
    if (!provider) { setMessages((current) => [...current, { role: "assistant", content: "没有检测到浏览器钱包插件。" }]); return; }
    const accounts = await provider.request({ method: "eth_requestAccounts" }); const chainId = await provider.request({ method: "eth_chainId" });
    setWallet({ provider, address: accounts[0], chainId, chain: chainName(chainId) });
    provider.on?.("accountsChanged", (next: string[]) => setWallet((current) => current && next[0] ? { ...current, address: next[0] } : null));
    provider.on?.("chainChanged", (next: string) => setWallet((current) => current && { ...current, chainId: next, chain: chainName(next) }));
  }
  async function sendTransaction(transaction: any) {
    if (!wallet) throw new Error("请先连接浏览器钱包");
    const active = await wallet.provider.request({ method: "eth_chainId" });
    if (transaction.chain_id && Number(active) !== Number(transaction.chain_id)) await wallet.provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: `0x${Number(transaction.chain_id).toString(16)}` }] });
    return wallet.provider.request({ method: "eth_sendTransaction", params: [{ from: wallet.address, to: transaction.to, data: transaction.data || "0x", value: txValue(transaction.value), ...(transaction.gas_limit ? { gas: txValue(transaction.gas_limit) } : {}), ...(transaction.max_fee_per_gas ? { maxFeePerGas: txValue(transaction.max_fee_per_gas) } : {}), ...(transaction.max_priority_fee_per_gas ? { maxPriorityFeePerGas: txValue(transaction.max_priority_fee_per_gas) } : {}) }] });
  }
  async function selectQuote(reference: string) {
    if (!sessionId) return; const body = await requestApi(`/v1/swap/${sessionId}/select-quote`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", provider_reference: reference }) });
    setSelectedQuote(reference); applyState(body); setMessages((current) => [...current, { role: "system", content: "报价已选择，正在检查余额和 Allowance。" }]);
  }
  function applyState(value: any) { const current = value?.state || value?.data?.state || value?.data || value; if (current?.quote_candidates) setQuotes(current.quote_candidates); if (current?.selected_provider_reference) setSelectedQuote(current.selected_provider_reference); if (current?.pending_transaction) setPendingTransaction(current.pending_transaction); if (current?.approval_transaction) setApproval(current.approval_transaction); if (current?.confirmation_state) setConfirmation(current.confirmation_state); if (current?.preflight) setPreflight(current.preflight); if (current?.response?.status) setStatus(current.response); if (current?.response?.message) setMessages((previous) => [...previous, { role: "assistant", content: current.response.message }]); }
  async function send(event?: FormEvent) {
    event?.preventDefault();
    const text = message.trim(); if (!text || busy) return;
    setMessages((current) => [...current, { role: "user", content: text }]); setMessage(""); setBusy(true);
    const body = await requestApi("/v1/agent/turn", { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", conversation_id: conversationId ?? undefined, session_id: sessionId ?? undefined, message: text, address: wallet?.address, chain: wallet?.chain, metadata: wallet ? { wallet_chain_id: wallet.chainId } : {} }) });
    setConversationId(body.conversation_id); setSessionId(body.session_id ?? sessionId); log("API POST /v1/agent/turn", body);
    const response = await fetch(`${API_BASE}/v1/agent/stream/${body.run_id}`, { headers });
    if (!response.body) throw new Error("SSE stream unavailable");
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ""; let assistant = "";
    while (true) {
      const item = await reader.read(); if (item.done) break; buffer += decoder.decode(item.value, { stream: true });
      const chunks = buffer.split("\n\n"); buffer = chunks.pop() ?? "";
      for (const chunk of chunks) {
        const data = chunk.split("\n").find((line) => line.startsWith("data:")); if (!data) continue;
        try { const payload = JSON.parse(data.slice(5).trim()); log("SSE", payload); applyState(payload); const content = payload?.state?.response?.message ?? payload?.response?.message; if (content && content !== assistant) assistant = content; } catch { /* ignore keepalive */ }
      }
    }
    if (assistant) setMessages((current) => [...current, { role: "assistant", content: assistant }]);
    setBusy(false);
  }
  useEffect(() => { if (messagesRef.current) messagesRef.current.scrollTop = messagesRef.current.scrollHeight; }, [messages]);
  return <main>
    <header><h1>Wallet Agent Demo</h1><p>通过自然语言查询余额、兑换报价并准备交易。私钥不会发送到服务端。</p><div className="wallet-bar"><span className="wallet-status">{wallet ? `已连接：${wallet.chain} ${wallet.address.slice(0, 6)}…${wallet.address.slice(-4)}` : "未连接浏览器钱包"}</span><input className="token" type="password" placeholder="Bearer token（可选）" value={token} onChange={(event) => setToken(event.target.value)} /><button onClick={() => void connectWallet()}>{wallet ? "重新连接" : "连接钱包"}</button><button className="secondary" onClick={() => { setHistoryOpen(!historyOpen); if (!historyOpen) void loadHistory(); }}>历史</button></div></header>
    {historyOpen && <section className="panel"><div className="toolbar"><button className="secondary" onClick={() => void loadHistory()}>重新加载</button><button className="secondary" onClick={startNew}>新建对话</button></div><div className="list">{conversations.length === 0 ? <p className="muted">暂无已保存的对话。</p> : conversations.map((item) => <div key={item.conversation_id} className="conversation"><button onClick={() => void loadConversation(item.conversation_id)}><strong>{item.summary}</strong><span>{new Date(item.updated_at).toLocaleString()} · {item.status}</span></button><button className="danger delete" onClick={() => void removeConversation(item.conversation_id)}>删除</button></div>)}</div></section>}
    <div ref={messagesRef} id="messages">
      {messages.map((item, index) => <div className={`message ${item.role}`} key={`${index}-${item.content}`}><div className="bubble">{item.content}</div></div>)}
      {quotes.length > 0 && <section className="card"><h3>报价列表</h3>{quotes.map((quote) => <div className="quote" key={quote.provider_reference}><strong>{quote.provider} · {quote.provider_reference}</strong><span>{quote.input_amount} {quote.source_asset?.symbol} → {quote.expected_output} {quote.destination_asset?.symbol}</span><button className="secondary" disabled={!quote.provider_reference} onClick={() => void selectQuote(quote.provider_reference!)}>{selectedQuote === quote.provider_reference ? "已选择" : "选择此报价"}</button></div>)}</section>}
      {confirmation?.status === "requested" && <section className="card"><h3>请确认兑换</h3><p>{confirmation.summary?.input_amount} {confirmation.summary?.source_asset?.symbol} → {confirmation.summary?.expected_output} {confirmation.summary?.destination_asset?.symbol}</p><div className="card-actions"><button onClick={async () => { const body = await requestApi(`/v1/swap/${sessionId}/confirm`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", approved: true }) }); applyState(body); }}>确认兑换</button><button className="secondary" onClick={async () => { const body = await requestApi(`/v1/swap/${sessionId}/confirm`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", approved: false }) }); applyState(body); }}>取消</button></div></section>}
      {preflight && <section className="card"><h3>{preflight.ok ? "交易预检查通过" : "交易预检查未通过"}</h3><p>{(preflight.checks || []).map((item: any) => `${item.status === "passed" ? "✓" : "×"} ${item.message}`).join("\n")}</p></section>}
      {approval && <section className="card"><h3>需要 Approve 授权</h3><pre className="debug">{JSON.stringify(approval, null, 2)}</pre><button onClick={async () => { const hash = await sendTransaction(approval); const body = await requestApi(`/v1/swap/${sessionId}/approve-broadcast`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", chain: approval.chain, approve_tx_hash: hash }) }); applyState(body); }}>钱包签名并广播 Approve</button></section>}
      {pendingTransaction && <section className="card"><h3>交易已准备好</h3><pre className="debug">{JSON.stringify(pendingTransaction, null, 2)}</pre><button onClick={async () => { const hash = await sendTransaction(pendingTransaction); const body = await requestApi(`/v1/swap/${sessionId}/broadcast`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", chain: pendingTransaction.chain, tx_hash: hash }) }); applyState(body); }}>钱包签名并广播</button></section>}
      {status && <section className="card"><h3>订单状态</h3><p>{status.message || status.status?.status || "处理中"}</p></section>}
    </div>
    <details className="panel"><summary>后端调试数据（{debug.length} 条）</summary><div className="toolbar"><button className="secondary" onClick={() => navigator.clipboard?.writeText(debug.join("\n\n"))}>复制调试数据</button><button className="secondary" onClick={() => setDebug([])}>清空</button></div><pre className="debug">{debug.join("\n\n") || "等待后端响应…"}</pre></details>
    <form className="composer" onSubmit={send}><div className="composer-row"><textarea value={message} onChange={(event) => setMessage(event.target.value)} placeholder="例如：把 1 USDC 从 Base 换成 BSC 上的 USDT" disabled={busy} /><button disabled={busy}>{busy ? "处理中…" : "发送"}</button></div><div className="meta"><span>conversation: {conversationId ?? "新会话"}</span><span>session: {sessionId ?? "未创建"}</span></div></form>
  </main>;
}
