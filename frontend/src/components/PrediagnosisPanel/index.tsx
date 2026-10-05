/** PrediagnosisPanel：自主预诊断结果（定时扫描发现新增 P1 时子 Agent 自动分析的根因） */
import { useCallback, useEffect, useState } from 'react';
import { Button, Collapse, Empty, Tag, Tooltip } from 'antd';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ReloadOutlined } from '@ant-design/icons';
import { fetchPrediagnosis } from '../../services/api';
import type { PrediagnosisData, PrediagnosisRecord } from '../../types';
import type { PrediagnosisPanelProps } from './types';
import styles from './index.module.css';

const STATUS_META: Record<string, { color: string; text: string }> = {
  ok: { color: 'green', text: '已完成' },
  aborted: { color: 'orange', text: '超预算中断' },
  error: { color: 'red', text: '出错' },
  failed: { color: 'red', text: '失败' },
};

function fmtTime(ms: number): string {
  const d = new Date(ms);
  const p = (n: number): string => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export default function PrediagnosisPanel({
  refreshKey = 0,
}: PrediagnosisPanelProps): JSX.Element {
  const [data, setData] = useState<PrediagnosisData | null>(null);
  const [loading, setLoading] = useState<boolean>(false);

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      setData(await fetchPrediagnosis(20));
    } catch {
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const items = (data?.records ?? []).map((r: PrediagnosisRecord) => {
    const meta = STATUS_META[r.status] ?? { color: 'default', text: r.status };
    return {
      key: String(r.id),
      label: (
        <span className={styles.head}>
          <Tag color="red">{r.severity}</Tag>
          <span className={styles.rule}>{r.rule_id}</span>
          <span className={styles.ref}>{r.resource_ref}</span>
          <Tag color={meta.color}>{meta.text}</Tag>
          <span className={styles.meta}>
            {r.tool_calls} 次调用 · {r.tokens.toLocaleString()} tokens ·{' '}
            {Math.round(r.duration_ms / 1000)}s · {fmtTime(r.created_at)}
          </span>
        </span>
      ),
      children: (
        <div className={styles.body}>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{r.conclusion}</ReactMarkdown>
        </div>
      ),
    };
  });

  return (
    <div className={styles.section}>
      <div className={styles.sectionTitle}>
        <span>🔮 自主预诊断</span>
        <Tooltip title="定时扫描发现新增 P1 风险时，自动派一个只读子 Agent 分析根因。仅 live 模式生效。">
          <span className={styles.desc}>
            {data
              ? data.enabled
                ? `已开启 · 单轮最多 ${data.max_per_scan} 个`
                : '已关闭'
              : ''}
          </span>
        </Tooltip>
        <Button
          className={styles.refresh}
          size="small"
          icon={<ReloadOutlined />}
          loading={loading}
          onClick={() => void load()}
        >
          刷新
        </Button>
      </div>

      {items.length > 0 ? (
        <Collapse size="small" items={items} />
      ) : (
        <Empty
          description="暂无预诊断记录（live 模式下扫出新增 P1 风险时自动触发）"
          image={Empty.PRESENTED_IMAGE_SIMPLE}
        />
      )}
    </div>
  );
}
