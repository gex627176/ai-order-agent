import { useEffect, useMemo, useRef, useState } from "react";
import { ApiRequestError, api } from "./api";
import type { AgentEvent, AgentSession, Customer } from "./types";

const SESSION_STORAGE_KEY = "agent-harness-session-id";

const STATUS_LABELS: Record<AgentSession["status"], string> = {
  active: "可继续对话",
  waiting_input: "等待补充信息",
  waiting_approval: "等待人工审批",
  completed: "订单已完成",
  failed: "会话执行失败",
};

const ACTION_LABELS = {
  provide_customer: "请补充客户",
  review_sku: "请人工核对商品",
  confirm_order: "请审批建单",
};

type Props = {
  customers: Customer[];
  onOpenDraft?: (draftId: string) => void;
  onOrderCreated?: () => void;
};

async function operationKey(
  kind: "message" | "resume", session: AgentSession, payload: unknown,
) {
  const material = new TextEncoder().encode(
    JSON.stringify({ kind, session_id: session.id, version: session.version, payload }),
  );
  const digest = await globalThis.crypto.subtle.digest("SHA-256", material);
  const hex = [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0")).join("");
  return `agent-ui:${kind}:${hex}`;
}

function readStoredSessionId() {
  try { return localStorage.getItem(SESSION_STORAGE_KEY); }
  catch { return null; }
}

function storeSessionId(sessionId: string) {
  try { localStorage.setItem(SESSION_STORAGE_KEY, sessionId); }
  catch { /* SQLite remains the source of truth when browser storage is unavailable. */ }
}

function clearStoredSessionId() {
  try { localStorage.removeItem(SESSION_STORAGE_KEY); }
  catch { /* Nothing else to clear. */ }
}

function eventKind(event: AgentEvent) {
  if (event.type === "user") return "用户";
  if (event.type === "assistant") return "Agent";
  if (event.type === "tool") return "工具";
  if (event.type === "delegation") return "子 Agent";
  if (event.type === "permission") return "人工审批";
  if (event.type === "model") return "模型规划";
  return "Harness";
}

function eventContent(event: AgentEvent) {
  return typeof event.payload.content === "string"
    ? event.payload.content
    : event.summary;
}

export default function AgentWorkspace({
  customers, onOpenDraft, onOrderCreated,
}: Props) {
  const [session, setSession] = useState<AgentSession | null>(null);
  const [customerId, setCustomerId] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [restoring, setRestoring] = useState(true);
  const [error, setError] = useState("");
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    const storedSessionId = readStoredSessionId();
    if (!storedSessionId) {
      setRestoring(false);
      return () => { mountedRef.current = false; };
    }
    let active = true;
    void api.agentSession(storedSessionId)
      .then((restored) => {
        if (!active) return;
        setSession(restored);
        setCustomerId(restored.customer_id ? String(restored.customer_id) : "");
      })
      .catch((reason) => {
        if (!active) return;
        if (reason instanceof ApiRequestError && reason.status === 404) {
          clearStoredSessionId();
        } else {
          setError(reason instanceof Error ? reason.message : "恢复 Agent 会话失败");
        }
      })
      .finally(() => { if (active) setRestoring(false); });
    return () => { active = false; mountedRef.current = false; };
  }, []);

  const conversationEvents = useMemo(
    () => session?.events.filter(
      (event) => event.type === "user" || event.type === "assistant",
    ) ?? [],
    [session],
  );
  const needsLatestReply = Boolean(
    session?.last_message
    && conversationEvents.length > 0
    && (
      conversationEvents.at(-1)?.type !== "assistant"
      || eventContent(conversationEvents.at(-1)!) !== session.last_message
    ),
  );
  const approvalReady = session?.status === "waiting_approval"
    && session.pending_action?.name === "confirm_order";
  const canSend = Boolean(session)
    && !busy
    && session?.status !== "waiting_approval"
    && session?.status !== "completed"
    && session?.status !== "failed";

  function remember(nextSession: AgentSession) {
    if (!mountedRef.current) return;
    setSession(nextSession);
    storeSessionId(nextSession.id);
  }

  async function reconcileConflict(reason: unknown, currentSession: AgentSession) {
    if (!(reason instanceof ApiRequestError) || reason.status !== 409) return false;
    try {
      const current = await api.agentSession(currentSession.id);
      if (!mountedRef.current) return true;
      remember(current);
      setError(`${reason.message}，会话状态已刷新，请按最新提示继续。`);
    } catch (refreshReason) {
      if (mountedRef.current) {
        setError(refreshReason instanceof Error
          ? `状态冲突且刷新失败：${refreshReason.message}`
          : "状态冲突且会话刷新失败");
      }
    }
    return true;
  }

  async function createSession() {
    setBusy(true);
    setError("");
    try {
      const created = await api.createAgentSession(customerId ? Number(customerId) : null);
      remember(created);
    } catch (reason) {
      if (mountedRef.current) {
        setError(reason instanceof Error ? reason.message : "创建 Agent 会话失败");
      }
    } finally {
      if (mountedRef.current) {
        setBusy(false);
        setRestoring(false);
      }
    }
  }

  async function sendMessage() {
    const content = message.trim();
    if (!session || !canSend || !content) return;
    setBusy(true);
    setError("");
    try {
      const idempotencyKey = await operationKey("message", session, { content });
      if (!mountedRef.current) return;
      const updated = await api.sendAgentMessage(
        session.id, content, idempotencyKey,
      );
      remember(updated);
      if (mountedRef.current) setMessage("");
    } catch (reason) {
      if (mountedRef.current && !await reconcileConflict(reason, session)) {
        setError(reason instanceof Error ? reason.message : "发送消息失败");
      }
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  }

  async function resume(approved: boolean) {
    if (!session || !approvalReady || busy) return;
    setBusy(true);
    setError("");
    try {
      const idempotencyKey = await operationKey("resume", session, { approved });
      if (!mountedRef.current) return;
      const updated = await api.resumeAgentSession(
        session.id, approved, idempotencyKey,
      );
      remember(updated);
      if (mountedRef.current && updated.status === "completed") onOrderCreated?.();
    } catch (reason) {
      if (mountedRef.current && !await reconcileConflict(reason, session)) {
        setError(reason instanceof Error ? reason.message : "审批操作失败");
      }
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  }

  function resetSession() {
    clearStoredSessionId();
    setSession(null);
    setMessage("");
    setError("");
  }

  if (restoring) {
    return <section className="card agent-workspace agent-loading" aria-live="polite">
      正在恢复 Agent 会话…
    </section>;
  }

  if (!session) {
    return <section className="card agent-start">
      <div className="agent-start-copy">
        <span className="eyebrow">PERSISTENT AGENT SESSION</span>
        <h3>让 Agent 和你一起把订单问清楚</h3>
        <p>它可以追问客户、调用受限工具并生成草稿，但正式建单仍要你单独批准。</p>
      </div>
      <div className="agent-start-form">
        <label htmlFor="agent-customer">预选客户（可选）</label>
        <select
          id="agent-customer"
          value={customerId}
          onChange={(event) => setCustomerId(event.target.value)}
          disabled={busy}
        >
          <option value="">让 Agent 在对话中询问</option>
          {customers.map((customer) => <option key={customer.id} value={customer.id}>
            {customer.name}{customer.contact ? ` · ${customer.contact}` : ""}
          </option>)}
        </select>
        <button className="primary" onClick={() => void createSession()} disabled={busy}>
          {busy ? "正在创建…" : "创建 Agent 会话"}
        </button>
        {error && <p className="agent-error" role="alert">{error}</p>}
      </div>
    </section>;
  }

  return <section className="agent-workspace">
    <header className="agent-session-bar">
      <div>
        <span>AGENT SESSION</span>
        <strong title={session.id}>{session.id.slice(0, 8)}</strong>
      </div>
      <div className={`agent-status ${session.status}`}>
        <i />{STATUS_LABELS[session.status]}
      </div>
      <button type="button" onClick={resetSession} disabled={busy}>新建会话</button>
    </header>

    {error && <div className="alert error" role="alert">{error}</div>}

    <div className="agent-grid">
      <section className="card agent-conversation">
        <div className="section-title"><span>01</span><div>
          <h3>和 Agent 沟通</h3><p>会话和事件会保存到本地 SQLite</p>
        </div></div>

        <div className="agent-messages" aria-live="polite">
          {conversationEvents.length === 0 && <div className="agent-welcome">
            <b>拾单 Agent</b>
            <p>{session.last_message || "请描述订单，缺少的信息我会继续询问。"}</p>
          </div>}
          {conversationEvents.map((event) => <article
            className={`agent-message ${event.type === "user" ? "user" : "assistant"}`}
            key={event.id}
          >
            <span>{event.type === "user" ? "你" : "Agent"}</span><p>{eventContent(event)}</p>
          </article>)}
          {needsLatestReply && <article className="agent-message assistant">
            <span>Agent</span><p>{session.last_message}</p>
          </article>}
        </div>

        {session.pending_action && <div className={`agent-action ${session.pending_action.name}`}>
          <div><span>下一步</span><strong>{ACTION_LABELS[session.pending_action.name]}</strong></div>
          {session.current_draft_id && <button
            type="button" onClick={() => onOpenDraft?.(session.current_draft_id!)}
          >查看并编辑草稿</button>}
        </div>}

        {approvalReady ? <div className="agent-approval">
          <div><strong>人工确认建单</strong><p>批准前请核对草稿。Agent 无法替你执行这个决定。</p></div>
          <div>
            <button type="button" className="reject" onClick={() => void resume(false)} disabled={busy}>拒绝建单</button>
            <button type="button" className="approve" onClick={() => void resume(true)} disabled={busy}>批准并建单</button>
          </div>
        </div> : <div className="agent-composer">
          <label htmlFor="agent-message">发送给 Agent</label>
          <textarea
            id="agent-message" rows={3} value={message}
            placeholder={session.status === "completed" ? "此会话已完成，请新建会话继续" : "例如：帮我录番茄5斤，客户是惠民餐厅"}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={(event) => {
              if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
                event.preventDefault();
                void sendMessage();
              }
            }}
            disabled={!canSend}
          />
          <div><span>Ctrl + Enter 发送</span><button
            type="button" onClick={() => void sendMessage()}
            disabled={!canSend || !message.trim()}
          >{busy ? "Agent 执行中…" : "发送消息"}</button></div>
        </div>}
      </section>

      <aside className="card agent-events">
        <div className="section-title"><span>02</span><div>
          <h3>可解释事件</h3><p>只展示步骤摘要，不展示模型隐藏推理</p>
        </div></div>
        <ol>
          {session.events.map((event) => <li key={event.id}>
            <div className={`agent-event-dot ${event.status}`} />
            <div><span>{eventKind(event)} · #{event.sequence}</span><strong>{event.summary}</strong>
              <small>{event.name} · {event.status}</small></div>
          </li>)}
        </ol>
        {session.has_more_events && <p className="agent-event-note">较早事件已折叠，可通过会话 API 分页读取。</p>}
        <dl className="agent-facts">
          <div><dt>客户</dt><dd>{customers.find((item) => item.id === session.customer_id)?.name ?? "待确认"}</dd></div>
          <div><dt>草稿</dt><dd>{session.current_draft_id?.slice(0, 8) ?? "—"}</dd></div>
          <div><dt>订单</dt><dd>{session.current_order_id ?? "—"}</dd></div>
          <div><dt>版本</dt><dd>v{session.version}</dd></div>
        </dl>
      </aside>
    </div>
  </section>;
}
