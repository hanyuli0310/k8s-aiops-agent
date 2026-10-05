/**
 * AssetPanel：Skill / Agent 资产面板。
 *
 * 这两类资产原来只能靠读代码或 curl /api/status 才能看到，而它们恰恰决定了
 * Agent 的行为。面板的重点不是"列个清单"，而是三件排查时真正要看的事：
 *   1. 三层披露有没有起作用 —— 常驻目录占全部内容的百分比；
 *   2. 每个 Agent 手里究竟有哪些工具、其中几个是破坏性的；
 *   3. 模型实际读到的 Skill 原文（点开按需拉取，与 load_skill 同一条读取路径）。
 */
import { useCallback, useState } from 'react';
import { Collapse, Empty, Spin, Tabs, Tag, Tooltip } from 'antd';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { fetchSkillContent } from '../../services/api';
import type { AgentInfo, SkillInfo, ToolSafety } from '../../types';
import type { AssetPanelProps } from './types';
import styles from './index.module.css';

/** 内容缓存的 key：正文用 name，细则用 name/reference */
type ContentKey = string;

const TIER_META: Record<string, { color: string; text: string; tip: string }> = {
  primary: { color: 'purple', text: '主模型', tip: '决策与推理类任务走主模型' },
  fast: { color: 'cyan', text: '快模型', tip: '轻结构化任务走快模型，省时省钱' },
};

/**
 * 工具标签的颜色：按安全属性区分，一眼看出这个 Agent 能造成多大影响。
 *
 * 四类而不是三类 —— 既不只读、又没标 writes_business_data 的工具（如
 * create_risk_rule）走后端的 fail-closed 分支：它会改变后续所有扫描的行为，
 * 影响面大于一次性数据写入，所以任何模式下都要人工确认。这一类不能和"只写业务表"
 * 混在一起显示，否则面板会让人以为它在 auto 模式下会被自动放行。
 */
function toolColor(safety?: ToolSafety): string {
  if (!safety) return 'default';
  if (safety.destructive) return 'red';
  if (safety.writes_business_data) return 'orange';
  if (safety.read_only) return 'blue';
  return 'gold';
}

function toolTip(name: string, safety?: ToolSafety): string {
  if (!safety) return name;
  if (safety.destructive) return `${name}：破坏性动作，任何模式下都需用户确认`;
  if (safety.writes_business_data) return `${name}：只写本平台业务表，auto 模式自动放行`;
  if (safety.read_only) return `${name}：只读，任何模式放行`;
  return `${name}：配置变更类，一律需用户确认（不参与 auto 放行白名单）`;
}

export default function AssetPanel({
  skills,
  stats,
  agents,
  toolSafety = [],
}: AssetPanelProps): JSX.Element {
  const [contents, setContents] = useState<Record<ContentKey, string>>({});
  const [loadingKey, setLoadingKey] = useState<ContentKey | null>(null);

  const safetyOf = useCallback(
    (name: string): ToolSafety | undefined => toolSafety.find((t) => t.name === name),
    [toolSafety],
  );

  /** 按需拉取 Skill 内容。已取过的直接用缓存，避免反复展开时重复请求 */
  const loadContent = useCallback(
    async (name: string, reference?: string): Promise<void> => {
      const key = reference ? `${name}/${reference}` : name;
      if (contents[key] !== undefined) return;
      setLoadingKey(key);
      try {
        const r = await fetchSkillContent(name, reference);
        setContents((prev) => ({ ...prev, [key]: r.content }));
      } catch {
        setContents((prev) => ({ ...prev, [key]: '_读取失败，请确认后端已启动_' }));
      } finally {
        setLoadingKey(null);
      }
    },
    [contents],
  );

  const skillItems = skills.map((s: SkillInfo) => ({
    key: s.name,
    label: (
      <span className={styles.head}>
        <span className={styles.name}>{s.name}</span>
        <Tag color={s.layout === 'dir' ? 'geekblue' : 'default'}>
          {s.layout === 'dir' ? `目录 · ${s.references.length} 篇细则` : '单文件'}
        </Tag>
        <span className={styles.desc}>{s.description}</span>
        <Tooltip title="L1 常驻字符数 / L2 正文 / L3 细则合计">
          <span className={styles.meta}>
            L1 {s.catalog_chars} · L2 {s.body_chars}
            {s.refs_chars > 0 ? ` · L3 ${s.refs_chars}` : ''}
          </span>
        </Tooltip>
      </span>
    ),
    children: (
      <div className={styles.skillBody}>
        {s.when_to_use && (
          <div className={styles.when}>
            <b>何时用</b>：{s.when_to_use}
          </div>
        )}
        {s.allowed_tools.length > 0 && (
          <div className={styles.toolLine}>
            <b>配套工具</b>：
            {s.allowed_tools.map((t) => (
              <Tooltip key={t} title={toolTip(t, safetyOf(t))}>
                <Tag color={toolColor(safetyOf(t))}>{t}</Tag>
              </Tooltip>
            ))}
          </div>
        )}
        <Tabs
          size="small"
          onChange={(k) => void loadContent(s.name, k === 'body' ? undefined : k)}
          defaultActiveKey="body"
          items={[
            { key: 'body', label: '正文 (L2)' },
            ...s.references.map((r) => ({ key: r, label: `${r} (L3)` })),
          ].map((tab) => {
            const ck = tab.key === 'body' ? s.name : `${s.name}/${tab.key}`;
            return {
              key: tab.key,
              label: tab.label,
              children:
                loadingKey === ck ? (
                  <Spin size="small" />
                ) : (
                  <div className={styles.markdown}>
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {contents[ck] ?? '（展开后自动加载）'}
                    </ReactMarkdown>
                  </div>
                ),
            };
          })}
        />
      </div>
    ),
  }));

  const agentItems = agents.map((a: AgentInfo) => {
    const tier = TIER_META[a.model_tier] ?? { color: 'default', text: a.model_tier, tip: '' };
    // 用「变更类」而不是「破坏性」计数：create_risk_rule 这类非破坏性但需确认的工具
    // 也会改变系统行为，与审计面板的「仅看变更类」口径保持一致
    const mutating = a.tools.filter((t) => {
      const s = safetyOf(t);
      return s !== undefined && !s.read_only;
    }).length;
    return (
      <div key={a.key} className={styles.agentRow}>
        <div className={styles.head}>
          <span className={styles.name}>{a.name}</span>
          <Tooltip title={tier.tip}>
            <Tag color={tier.color}>
              {tier.text} · {a.model}
            </Tag>
          </Tooltip>
          {a.dispatchable ? (
            <Tooltip title="可被 dispatch_agent 作为子 Agent 派发（子 Agent 只保留只读工具）">
              <Tag color="green">可派发</Tag>
            </Tooltip>
          ) : (
            <Tooltip title="只作为顶层 Agent 由意图路由使用，不能被派发">
              <Tag>仅顶层</Tag>
            </Tooltip>
          )}
          {a.skill && (
            <Tooltip title="主 Skill：全文注入系统提示词，其余 Skill 只给目录">
              <Tag color="blue">主 Skill: {a.skill}</Tag>
            </Tooltip>
          )}
          <span className={styles.meta}>
            {a.tools.length} 个工具
            {mutating > 0 ? ` · ${mutating} 个变更类` : ' · 全只读'}
          </span>
        </div>
        <div className={styles.agentDesc}>{a.description}</div>
        <div className={styles.toolLine}>
          {a.tools.map((t) => (
            <Tooltip key={t} title={toolTip(t, safetyOf(t))}>
              <Tag color={toolColor(safetyOf(t))}>{t}</Tag>
            </Tooltip>
          ))}
        </div>
      </div>
    );
  });

  return (
    <div className={styles.section}>
      <div className={styles.sectionTitle}>
        <span>🧩 Skill / Agent 资产</span>
        {stats && (
          <Tooltip title="L1 目录常驻在每轮系统提示词里，L2 正文与 L3 细则由模型判断需要时才用 load_skill 取回。这个百分比越低，渐进式披露省下的常驻上下文越多。">
            <span className={styles.desc}>
              {stats.count} 篇 Skill（含 {stats.reference_count} 篇细则）· 全文{' '}
              {stats.total_chars.toLocaleString()} 字符 · 常驻目录仅{' '}
              {stats.catalog_chars.toLocaleString()}（{stats.catalog_pct}%）
            </span>
          </Tooltip>
        )}
      </div>

      {skills.length === 0 && agents.length === 0 ? (
        <Empty description="后端未返回资产信息" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <Tabs
          size="small"
          items={[
            {
              key: 'skills',
              label: `📚 方法论 Skill (${skills.length})`,
              children: (
                <Collapse
                  size="small"
                  items={skillItems}
                  onChange={(keys) => {
                    const last = (Array.isArray(keys) ? keys : [keys]).slice(-1)[0];
                    if (last) void loadContent(String(last));
                  }}
                />
              ),
            },
            {
              key: 'agents',
              label: `🤖 专家 Agent (${agents.length})`,
              children: <div className={styles.agentList}>{agentItems}</div>,
            },
          ]}
        />
      )}
    </div>
  );
}
