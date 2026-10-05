/** ToolTimeline：Agent 执行轨迹折叠面板（意图/思考/工具调用/结果） */
import { useState } from 'react';
import { CaretDownOutlined, CaretRightOutlined, LoadingOutlined } from '@ant-design/icons';
import type { ChatEvent } from '../../types';
import type { ToolTimelineProps } from './types';
import styles from './index.module.css';

function stepIcon(ev: ChatEvent): string {
  switch (ev.type) {
    case 'intent':
      return '🎯';
    case 'agent_start':
      return '🤖';
    case 'phase':
      return '📌';
    case 'tool_call':
      return '🔧';
    case 'tool_result':
      return '📄';
    case 'thinking':
      return '💭';
    case 'error':
      return '❌';
    case 'permission_request':
      return '🔐';
    case 'permission_granted':
      return '✅';
    case 'aborted':
      return '⏹️';
    case 'compacted':
      return '🗜️';
    case 'model_fallback':
      return '🔀';
    case 'subagent_start':
      return '🧑‍🚀';
    case 'subagent_done':
      return '🏁';
    case 'plan_update':
      return '📋';
    case 'continued':
      return '⏭️';
    case 'verify_warning':
      return '🔎';
    default:
      return '·';
  }
}

/** 任务清单的状态标记：与后端 plan_digest() 的 [x]/[~]/[ ] 保持同一套语义 */
const PLAN_MARK: Record<string, string> = {
  done: '✓',
  in_progress: '▸',
  pending: '○',
};

function stepText(ev: ChatEvent): JSX.Element | null {
  switch (ev.type) {
    case 'intent':
      return (
        <span>
          意图识别：<span className={styles.toolName}>{ev.intent}</span>
          <span className={styles.argsText}>（{ev.router === 'llm' ? 'LLM 路由' : '关键词路由'}）</span>
        </span>
      );
    case 'agent_start':
      return <span>启动 <span className={styles.toolName}>{ev.agent}</span></span>;
    case 'phase':
      return <span>阶段：<span className={styles.toolName}>{ev.phase}</span> — {ev.task}</span>;
    case 'thinking':
      return <span className={styles.thinking}>{(ev.text ?? '').slice(0, 200)}</span>;
    case 'tool_call':
      return (
        <span>
          调用 <span className={styles.toolName}>{ev.tool}</span>{' '}
          <span className={styles.argsText}>{JSON.stringify(ev.args)}</span>
        </span>
      );
    case 'tool_result':
      return (
        <span className={styles.resultText}>
          {JSON.stringify(ev.result).slice(0, 300)}
          {typeof ev.duration_ms === 'number' && (
            <span className={styles.argsText}> · {ev.duration_ms}ms</span>
          )}
        </span>
      );
    case 'permission_request':
      return (
        <span className={styles.permission}>
          等待确认：{ev.summary}
          {ev.is_destructive && <span className={styles.dangerTag}>变更集群</span>}
        </span>
      );
    case 'permission_granted':
      return (
        <span className={styles.granted}>
          已批准 <span className={styles.toolName}>{ev.tool}</span>
          <span className={styles.argsText}>（用户批准）</span>
        </span>
      );
    case 'aborted':
      return <span className={styles.aborted}>已中断：{ev.reason}</span>;
    case 'compacted':
      return (
        <span className={styles.notice}>
          {ev.reason}
          {typeof ev.freed_chars === 'number' && (
            <span className={styles.argsText}>（释放 {ev.freed_chars} 字符）</span>
          )}
        </span>
      );
    case 'model_fallback':
      return <span className={styles.notice}>{ev.text ?? `已切换到 ${ev.to}`}</span>;
    case 'subagent_start':
      return (
        <span>
          派发子 Agent <span className={styles.toolName}>{ev.agent}</span>
          <span className={styles.argsText}>（{ev.description}）</span>
        </span>
      );
    case 'subagent_done':
      return (
        <span className={styles.granted}>
          <span className={styles.toolName}>{ev.agent}</span> 完成
          <span className={styles.argsText}>
            （{ev.tool_calls} 次工具调用 · {(ev.tokens ?? 0).toLocaleString()} tokens）
          </span>
        </span>
      );
    case 'plan_update': {
      const steps = ev.plan ?? [];
      const done = steps.filter((s) => s.status === 'done').length;
      return (
        <span>
          任务清单
          <span className={styles.argsText}>
            （{done}/{steps.length} 完成）
          </span>
          <span className={styles.planList}>
            {steps.map((s, i) => (
              <span key={i} className={`${styles.planStep} ${styles[s.status]}`}>
                {PLAN_MARK[s.status] ?? '○'} {s.title}
              </span>
            ))}
          </span>
        </span>
      );
    }
    case 'continued':
      return (
        <span className={styles.notice}>
          {ev.text}
          {typeof ev.freed_chars === 'number' && (
            <span className={styles.argsText}>（上下文释放 {ev.freed_chars} 字符）</span>
          )}
        </span>
      );
    case 'verify_warning':
      return (
        <span className={styles.notice}>
          事实核对：{ev.text}
          {ev.corrected && <span className={styles.argsText}>（已自纠正一次）</span>}
        </span>
      );
    case 'error':
      return <span>{ev.text}</span>;
    default:
      return null;
  }
}

const VISIBLE_TYPES = new Set([
  'intent',
  'agent_start',
  'phase',
  'thinking',
  'tool_call',
  'tool_result',
  'error',
  'permission_request',
  'permission_granted',
  'aborted',
  'compacted',
  'model_fallback',
  'subagent_start',
  'subagent_done',
  'plan_update',
  'continued',
  'verify_warning',
]);

export default function ToolTimeline({ events, streaming }: ToolTimelineProps): JSX.Element | null {
  const [open, setOpen] = useState<boolean>(true);
  const steps = events.filter((e) => VISIBLE_TYPES.has(e.type));
  if (steps.length === 0) return null;
  const toolCalls = steps.filter((e) => e.type === 'tool_call').length;
  // 分段续跑过就把段数标在头部：12 步内做完和跑了 3 段，对读者是完全不同的信息
  const segments = steps.filter((e) => e.type === 'continued').length + 1;
  return (
    <div className={styles.timeline}>
      <div className={styles.header} onClick={() => setOpen(!open)}>
        {open ? <CaretDownOutlined /> : <CaretRightOutlined />}
        <span>
          Agent 执行轨迹（{toolCalls} 次工具调用
          {segments > 1 ? ` · ${segments} 段` : ''}）
        </span>
        {streaming && <LoadingOutlined />}
      </div>
      {open && (
        <div className={styles.body}>
          {steps.map((ev, i) => (
            <div
              className={`${styles.step} ${ev.subagent ? styles.stepNested : ''}`}
              key={i}
            >
              <span className={styles.stepIcon}>{stepIcon(ev)}</span>
              <span className={styles.stepBody}>
                {ev.subagent && (
                  <span className={styles.subagentTag}>{ev.subagent}</span>
                )}
                {stepText(ev)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
