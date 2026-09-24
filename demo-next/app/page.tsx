"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

type Message = { role: "user" | "assistant" | "system"; content: string };
type Conversation = { conversation_id: string; session_id?: string; summary: string; status: string; updated_at: string };

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
  async function send(event?: FormEvent) {
    event?.preventDefault();
    const text = message.trim(); if (!text || busy) return;
    setMessages((current) => [...current, { role: "user", content: text }]); setMessage(""); setBusy(true);
    const body = await requestApi("/v1/agent/turn", { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ user_id: "browser-demo", conversation_id: conversationId ?? undefined, session_id: sessionId ?? undefined, message: text }) });
    setConversationId(body.conversation_id); setSessionId(body.session_id ?? sessionId); log("API POST /v1/agent/turn", body);
    const response = await fetch(`${API_BASE}/v1/agent/stream/${body.run_id}`, { headers });
    if (!response.body) throw new Error("SSE stream unavailable");
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ""; let assistant = "";
    while (true) {
      const item = await reader.read(); if (item.done) break; buffer += decoder.decode(item.value, { stream: true });
      const chunks = buffer.split("\n\n"); buffer = chunks.pop() ?? "";
      for (const chunk of chunks) {
        const data = chunk.split("\n").find((line) => line.startsWith("data:")); if (!data) continue;
        try { const payload = JSON.parse(data.slice(5).trim()); log("SSE", payload); const content = payload?.state?.response?.message ?? payload?.response?.message; if (content && content !== assistant) assistant = content; } catch { /* ignore keepalive */ }
      }
    }
    if (assistant) setMessages((current) => [...current, { role: "assistant", content: assistant }]);
    setBusy(false);
  }
  useEffect(() => { if (messagesRef.current) messagesRef.current.scrollTop = messagesRef.current.scrollHeight; }, [messages]);
  return <main>
    <header><h1>Wallet Agent Demo</h1><p>通过自然语言查询余额、兑换报价并准备交易。私钥不会发送到服务端。</p><div className="wallet-bar"><span className="wallet-status">浏览器钱包连接由当前 demo 页面负责</span><input className="token" type="password" placeholder="Bearer token（可选）" value={token} onChange={(event) => setToken(event.target.value)} /><button className="secondary" onClick={() => { setHistoryOpen(!historyOpen); if (!historyOpen) void loadHistory(); }}>历史</button></div></header>
    {historyOpen && <section className="panel"><div className="toolbar"><button className="secondary" onClick={() => void loadHistory()}>重新加载</button><button className="secondary" onClick={startNew}>新建对话</button></div><div className="list">{conversations.length === 0 ? <p className="muted">暂无已保存的对话。</p> : conversations.map((item) => <div key={item.conversation_id} className="conversation"><button onClick={() => void loadConversation(item.conversation_id)}><strong>{item.summary}</strong><span>{new Date(item.updated_at).toLocaleString()} · {item.status}</span></button><button className="danger delete" onClick={() => void removeConversation(item.conversation_id)}>删除</button></div>)}</div></section>}
    <div ref={messagesRef} id="messages">{messages.map((item, index) => <div className={`message ${item.role}`} key={`${index}-${item.content}`}><div className="bubble">{item.content}</div></div>)}</div>
    <details className="panel"><summary>后端调试数据（{debug.length} 条）</summary><div className="toolbar"><button className="secondary" onClick={() => navigator.clipboard?.writeText(debug.join("\n\n"))}>复制调试数据</button><button className="secondary" onClick={() => setDebug([])}>清空</button></div><pre className="debug">{debug.join("\n\n") || "等待后端响应…"}</pre></details>
    <form className="composer" onSubmit={send}><div className="composer-row"><textarea value={message} onChange={(event) => setMessage(event.target.value)} placeholder="例如：把 1 USDC 从 Base 换成 BSC 上的 USDT" disabled={busy} /><button disabled={busy}>{busy ? "处理中…" : "发送"}</button></div><div className="meta"><span>conversation: {conversationId ?? "新会话"}</span><span>session: {sessionId ?? "未创建"}</span></div></form>
  </main>;
}
