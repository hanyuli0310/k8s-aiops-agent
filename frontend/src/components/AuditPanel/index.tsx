/** AuditPanel：工具调用审计面板（谁在什么模式下批准了什么动作、结果如何） */
import { useCallback, useEffect, useState } from 'react';
import { Button, Empty, Switch, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { ReloadOutlined } from '@ant-design/icons';
import { fetchAudit } from '../../services/api';
import type { AuditData, AuditRecord } from '../../types';
import type { AuditPanelProps } from './types';
import styles from './index.module.css';

const MODE_LABEL: Record<string, string> = {
  readonly: '只读巡检',
  confirm: '确认执行',
  auto: '无人值守',
};

/** 决策依据层 → 中文说明（对应后端 permissions.REASON_*） */
const REASON_LABEL: Record<string, string> = {
  rule: '工具固有属性',
  mode: '运行模式',
  tool: '工具自检',
  user: '用户批准',
  // 历史值：逃生阀已随确认卡片上线移除，保留映射以便正确回显旧记录
  auto_approve: '逃生阀自动批准（历史）',
};

const STATUS_META: Record<string, { color: string; text: string }> = {
  ok: { color: 'green', text: '已执行' },
  error: { color: 'orange', text: '执行出错' },
  denied: { color: 'red', text: '机制拒绝' },
  rejected: { color: 'red', text: '用户拒绝' },
  timeout: { color: 'red', text: '确认超时' },
  aborted: { color: 'default', text: '已中断' },
  invalid: { color: 'orange', text: '参数不合法' },
};

function fmtTime(ms: number): string {
  const d = new Date(ms);
  const p = (n: number): string => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export default function AuditPanel({ refreshKey = 0 }: AuditPanelProps): JSX.Element {
  const [data, setData] = useState<AuditData | null>(null);
  const [destructiveOnly, setDestructiveOnly] = useState<boolean>(true);
  const [loading, setLoading] = useState<boolean>(false);

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      setData(await fetchAudit(100, destructiveOnly));
    } catch {
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [destructiveOnly]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const columns: ColumnsType<AuditRecord> = [
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 116,
      render: (v: number) => <span className={styles.mono}>{fmtTime(v)}</span>,
    },
    {
      title: '动作',
      dataIndex: 'audit_repr',
      render: (v: string, r: AuditRecord) => (
        <span>
          {r.is_destructive === 1 && <Tag color="red">变更</Tag>}
          <span className={styles.repr}>{v}</span>
        </span>
      ),
    },
    {
      title: '模式',
      dataIndex: 'run_mode',
      width: 92,
      render: (v: string) => <Tag>{MODE_LABEL[v] ?? v}</Tag>,
    },
    {
      title: '批准来源',
      dataIndex: 'reason_type',
      width: 128,
      render: (v: string) => (
        <span className={v === 'auto_approve' ? styles.warnText : undefined}>
          {REASON_LABEL[v] ?? v}
        </span>
      ),
    },
    {
      title: '结果',
      dataIndex: 'result_status',
      width: 96,
      render: (v: string) => {
        const m = STATUS_META[v] ?? { color: 'default', text: v };
        return <Tag color={m.color}>{m.text}</Tag>;
      },
    },
    {
      title: '耗时',
      dataIndex: 'duration_ms',
      width: 76,
      render: (v: number) => <span className={styles.mono}>{v > 0 ? `${v}ms` : '-'}</span>,
    },
  ];

  const s = data?.summary;

  return (
    <div className={styles.section}>
      <div className={styles.sectionTitle}>
        <span>🧾 操作审计</span>
        {s && (
          <span className={styles.stats}>
            共 {s.total} 次调用 · 变更类 {s.destructive} · 已执行 {s.executed_ok} · 被拒{' '}
            {s.denied}
          </span>
        )}
        <span className={styles.tools}>
          <span className={styles.switchLabel}>仅看变更类</span>
          <Switch size="small" checked={destructiveOnly} onChange={setDestructiveOnly} />
          <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
            刷新
          </Button>
        </span>
      </div>

      {data && data.records.length > 0 ? (
        <Table<AuditRecord>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={data.records}
          pagination={{ pageSize: 8, size: 'small' }}
        />
      ) : (
        <Empty
          description={destructiveOnly ? '暂无变更类操作记录' : '暂无操作记录'}
          image={Empty.PRESENTED_IMAGE_SIMPLE}
        />
      )}
    </div>
  );
}
