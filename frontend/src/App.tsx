/** App：智能运维 Agent 主界面（智能对话 / 运维控制台 / 故障演练 三个 Tab） */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Badge, Button, Input, Segmented, Tabs, Tag, Tooltip } from 'antd';
import { SendOutlined, StopOutlined } from '@ant-design/icons';
import AssetPanel from './components/AssetPanel';
import AuditPanel from './components/AuditPanel';
import ChatMessage from './components/ChatMessage';
import ControlPanel from './components/ControlPanel';
import FaultDrillPanel from './components/FaultDrillPanel';
import GovernancePanel from './components/GovernancePanel';
import PermissionCard from './components/PermissionCard';
import PrediagnosisPanel from './components/PrediagnosisPanel';
import RealtimeCharts from './components/RealtimeCharts';
import { useChat } from './hooks/useChat';
import { fetchScenarios, fetchStatus, fetchWorld } from './services/api';
import type { FaultScenario, PermissionMode, StatusData, WorldStatus } from './types';
import styles from './App.module.css';

const QUICK_ACTIONS: string[] = [
  '采集集群可观测数据',
  '梳理服务拓扑',
  '做一次全面风险扫描',
  '用户反馈下单接口很慢，帮我定位',
  '按你给的方案执行治理，然后复扫验证',
  '帮我生成一条 Pod 重启次数的告警规则',
];

/** 三态运行模式的展示文案与说明（对应后端 harness/permissions.py） */
const MODE_OPTIONS: { value: PermissionMode; label: string; tip: string }[] = [
  {
    value: 'readonly',
    label: '🔍 只读巡检',
    tip: '可查询、可扫描、可梳理拓扑，但拒绝一切治理动作与配置变更',
  },
  {
    value: 'confirm',
    label: '✋ 确认执行',
    tip: '治理动作会弹出确认卡片，你批准后才真正执行',
  },
  {
    value: 'auto',
    label: '⚡ 无人值守',
    tip: '自动放行本平台业务表写入；变更集群的动作仍需你确认',
  },
];

export default function App(): JSX.Element {
  const { messages, loading, pending, usage, mode, setMode, send, stop, respond } = useChat();
  const [input, setInput] = useState<string>('');
  const [status, setStatus] = useState<StatusData | null>(null);
  const [scenarios, setScenarios] = useState<FaultScenario[]>([]);
  const [world, setWorld] = useState<WorldStatus | null>(null);
  const [activeTab, setActiveTab] = useState<string>('chat');
  const [auditKey, setAuditKey] = useState<number>(0);
  const bottomRef = useRef<HTMLDivElement>(null);

  const isLive = status?.data_source === 'live';

  const refreshWorld = useCallback(async (): Promise<void> => {
    try {
      setWorld(await fetchWorld());
    } catch {
      // mock 未就绪时静默
    }
  }, []);

  useEffect(() => {
    fetchStatus()
      .then((s) => {
        setStatus(s);
        if (s.permission?.default_mode) setMode(s.permission.default_mode);
      })
      .catch(() => setStatus(null));
    fetchScenarios().then((r) => setScenarios(r.scenarios ?? [])).catch(() => setScenarios([]));
  }, [setMode]);

  // 控制台/故障演练 Tab 激活时轮询世界状态（5s）：控制台需要 recent_actions，演练页需要故障/服务状态
  useEffect(() => {
    if (activeTab !== 'console' && activeTab !== 'drill') return undefined;
    void refreshWorld();
    const timer = setInterval(() => void refreshWorld(), 5000);
    return () => clearInterval(timer);
  }, [activeTab, refreshWorld]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, pending]);

  // 一轮对话结束后刷新审计面板（有新的工具调用记录）
  useEffect(() => {
    if (!loading) setAuditKey((k) => k + 1);
  }, [loading]);

  const submit = (text?: string): void => {
    const content = (text ?? input).trim();
    if (!content || loading) return;
    setInput('');
    void send(content);
  };

  const chatTab = (
    <>
      <div className={styles.chatArea}>
        {messages.length === 0 && (
          <div className={styles.welcome}>
            <div className={styles.welcomeTitle}>👋 你好，我是 K8s 集群的智能运维 Agent</div>
            <div>
              我可以：<b>采集</b> CMS/SLS 可观测数据入库 → <b>梳理</b>服务调用拓扑 →{' '}
              <b>扫描</b>高可用/容量/数据库/接口风险（支持 AI 生成告警规则）→{' '}
              <b>定位</b>故障根因并<b>执行治理</b>闭环。试试下面的快捷指令：
            </div>
            <div className={styles.quickies}>
              {QUICK_ACTIONS.map((q) => (
                <Tag key={q} color="blue" className={styles.quickTag} onClick={() => submit(q)}>
                  {q}
                </Tag>
              ))}
            </div>
          </div>
        )}
        {messages.map((m, i) => (
          <ChatMessage key={i} message={m} />
        ))}
        {pending && (
          <PermissionCard
            requestId={pending.requestId}
            tool={pending.tool}
            summary={pending.summary}
            args={pending.args}
            isDestructive={pending.isDestructive}
            timeoutS={pending.timeoutS}
            createdAt={pending.createdAt}
            expiring={pending.expiring}
            onRespond={(approved, remember) => void respond(approved, remember)}
          />
        )}
        <div ref={bottomRef} />
      </div>

      <div className={styles.toolBar}>
        <Segmented
          size="small"
          value={mode}
          onChange={(v) => setMode(v as PermissionMode)}
          disabled={loading}
          options={MODE_OPTIONS.map((o) => ({
            value: o.value,
            label: <Tooltip title={o.tip}>{o.label}</Tooltip>,
          }))}
        />
        {usage && (
          <span className={styles.usage}>
            {(usage.tokensIn + usage.tokensOut).toLocaleString()} tokens · ¥
            {usage.estCostCny.toFixed(4)}
            {usage.budgetPct !== null && ` · 预算 ${usage.budgetPct}%`} · {usage.elapsedS}s
          </span>
        )}
      </div>

      <div className={styles.inputBar}>
        <Input.TextArea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onPressEnter={(e) => {
            if (!e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder="输入运维指令，如：用户反馈下单接口很慢，帮我定位（Shift+Enter 换行）"
          autoSize={{ minRows: 1, maxRows: 4 }}
          disabled={loading}
        />
        {loading ? (
          <Button danger icon={<StopOutlined />} onClick={() => void stop()}>
            停止
          </Button>
        ) : (
          <Button type="primary" icon={<SendOutlined />} onClick={() => submit()}>
            发送
          </Button>
        )}
      </div>
    </>
  );

  const consoleTab = (
    <div className={styles.consoleArea}>
      <RealtimeCharts />
      <GovernancePanel onGoverned={() => void refreshWorld()} />
      <PrediagnosisPanel refreshKey={auditKey} />
      <AuditPanel refreshKey={auditKey} />
      <AssetPanel
        skills={status?.skills ?? []}
        stats={status?.skill_stats}
        agents={status?.agents ?? []}
        toolSafety={status?.tool_safety}
      />
      <ControlPanel world={world} />
    </div>
  );

  const drillTab = (
    <div className={styles.consoleArea}>
      <FaultDrillPanel scenarios={scenarios} world={world} onChanged={() => void refreshWorld()} />
    </div>
  );

  return (
    <div className={styles.app}>
      <div className={styles.header}>
        <span className={styles.logo}>🛰️ 全链路智能运维 Agent</span>
        <Badge
          status={status ? 'success' : 'error'}
          text={
            <span className={styles.statusText}>
              {status
                ? `后端已连接 · ${status.llm_available ? `LLM: ${status.llm_model}` : '离线降级模式'}` +
                  ` · ${isLive ? '🟢 live 动态模式' : '静态模式'} · 集群 prod-cluster-01`
                : '后端未连接'}
            </span>
          }
        />
      </div>
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        className={styles.tabs}
        items={[
          { key: 'chat', label: '💬 智能对话', children: chatTab },
          { key: 'console', label: '🎛️ 运维控制台', children: consoleTab },
          { key: 'drill', label: '🧪 故障演练', children: drillTab },
        ]}
      />
    </div>
  );
}
