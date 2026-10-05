/** useChat：对话状态管理 Hook（消息列表 + SSE 事件流消费 + 权限确认 + 中断） */
import { useCallback, useRef, useState } from 'react';
import { approveTool, stopChat, streamChat } from '../services/api';
import type {
  ChatEvent,
  ChatMessageItem,
  PendingApproval,
  PermissionMode,
  UsageInfo,
} from '../types';

const SESSION_ID = `web-${Date.now()}`;

interface UseChatResult {
  messages: ChatMessageItem[];
  loading: boolean;
  /** 当前待确认的工具调用（阻塞式确认，方案 A） */
  pending: PendingApproval | null;
  /** 本轮用量（token / 费用 / 预算占比 / 耗时） */
  usage: UsageInfo | null;
  mode: PermissionMode;
  setMode: (m: PermissionMode) => void;
  send: (text: string) => Promise<void>;
  /** 中止当前运行（对应 POST /api/chat/stop） */
  stop: () => Promise<void>;
  /** 回应确认卡片 */
  respond: (approved: boolean, remember?: boolean) => Promise<void>;
}

export function useChat(): UseChatResult {
  const [messages, setMessages] = useState<ChatMessageItem[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [pending, setPending] = useState<PendingApproval | null>(null);
  const [usage, setUsage] = useState<UsageInfo | null>(null);
  const [mode, setMode] = useState<PermissionMode>('confirm');
  const abortRef = useRef<AbortController | null>(null);

  /** 把一个事件合并进最后一条 assistant 消息 */
  const appendToLast = useCallback((ev: ChatEvent): void => {
    setMessages((prev) => {
      if (prev.length === 0) return prev;
      const next = [...prev];
      const last = { ...next[next.length - 1] };
      last.events = [...last.events, ev];
      if (ev.type === 'answer') {
        last.text = ev.text ?? '';
      }
      if (ev.type === 'error') {
        last.text = last.text || `❌ ${ev.text ?? '未知错误'}`;
      }
      if (ev.type === 'aborted') {
        last.text = last.text || `⏹️ 已中断：${ev.reason ?? '用户操作'}`;
      }
      if (ev.type === 'done') {
        last.streaming = false;
      }
      next[next.length - 1] = last;
      return next;
    });
  }, []);

  const handleEvent = useCallback(
    (ev: ChatEvent): void => {
      switch (ev.type) {
        case 'permission_request':
          setPending({
            requestId: ev.request_id ?? '',
            tool: ev.tool ?? '',
            summary: ev.summary ?? ev.tool ?? '',
            args: ev.args ?? {},
            isDestructive: Boolean(ev.is_destructive),
            timeoutS: ev.timeout_s ?? 300,
            createdAt: Date.now(),
            expiring: false,
          });
          break;
        case 'permission_expiring':
          setPending((p) =>
            p && p.requestId === ev.request_id
              ? { ...p, expiring: true, secondsLeft: ev.seconds_left }
              : p,
          );
          break;
        case 'permission_granted':
          setPending((p) => (p && p.requestId === ev.request_id ? null : p));
          break;
        case 'usage':
          setUsage({
            tokensIn: ev.tokens_in ?? 0,
            tokensOut: ev.tokens_out ?? 0,
            estCostCny: ev.est_cost_cny ?? 0,
            // 预算停用时后端给 null，保持 null（?? 0 会显示成「预算 0%」）
            budgetPct: ev.budget_pct ?? null,
            elapsedS: ev.elapsed_s ?? 0,
          });
          break;
        default:
          break;
      }
      // 确认结束（无论批准/拒绝/超时）后清掉卡片，避免残留
      if (ev.type === 'tool_result' || ev.type === 'aborted' || ev.type === 'done') {
        setPending(null);
      }
      appendToLast(ev);
    },
    [appendToLast],
  );

  const send = useCallback(
    async (text: string): Promise<void> => {
      if (!text.trim()) return;
      setLoading(true);
      setUsage(null);
      setPending(null);
      setMessages((prev) => [
        ...prev,
        { role: 'user', text, events: [], streaming: false },
        { role: 'assistant', text: '', events: [], streaming: true },
      ]);
      abortRef.current = new AbortController();
      try {
        await streamChat(SESSION_ID, text, handleEvent, abortRef.current.signal, mode);
      } catch (e) {
        setMessages((prev) => {
          const next = [...prev];
          const last = { ...next[next.length - 1] };
          last.text = `❌ 请求失败：${e instanceof Error ? e.message : String(e)}`;
          last.streaming = false;
          next[next.length - 1] = last;
          return next;
        });
      } finally {
        setLoading(false);
        setPending(null);
      }
    },
    [handleEvent, mode],
  );

  /** 中止：先通知后端置位 abort（让 worker 停止烧 token），再断开本地读流 */
  const stop = useCallback(async (): Promise<void> => {
    try {
      await stopChat(SESSION_ID);
    } catch {
      // 后端不可达时仍尝试本地断流
    }
    abortRef.current?.abort();
    setPending(null);
  }, []);

  const respond = useCallback(
    async (approved: boolean, remember = false): Promise<void> => {
      const current = pending;
      if (!current) return;
      setPending(null);
      try {
        await approveTool(current.requestId, approved, remember);
      } catch {
        // 请求失败时后端会走超时兜底（按拒绝处理）
      }
    },
    [pending],
  );

  return { messages, loading, pending, usage, mode, setMode, send, stop, respond };
}
