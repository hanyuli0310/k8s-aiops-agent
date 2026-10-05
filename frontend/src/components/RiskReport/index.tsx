/** RiskReport：风险扫描报告卡片（按严重度分组 + 治理状态） */
import { Tag } from 'antd';
import type { RiskFinding } from '../../types';
import type { RiskReportProps } from './types';
import styles from './index.module.css';

const SEVERITY_COLOR: Record<string, string> = { P1: 'red', P2: 'orange', P3: 'blue' };

function FindingItem({ f, resolved }: { f: RiskFinding; resolved: boolean }): JSX.Element {
  return (
    <div className={`${styles.item} ${resolved ? styles.resolvedItem : ''}`}>
      <div className={styles.itemTitle}>
        <Tag color={resolved ? 'green' : SEVERITY_COLOR[f.severity] ?? 'default'}>
          {resolved ? '已治理' : f.severity}
        </Tag>
        <span>{f.rule_id}</span>
        <span>{f.title}</span>
      </div>
      {!resolved && <div className={styles.suggestion}>💡 {f.suggestion}</div>}
      <div className={styles.evidence}>{JSON.stringify(f.evidence)}</div>
    </div>
  );
}

export default function RiskReport({ data }: RiskReportProps): JSX.Element {
  const { summary, open_findings: open, resolved_findings: resolved } = data;
  return (
    <div className={styles.card}>
      <div className={styles.header}>
        <span className={styles.title}>风险扫描报告</span>
        <Tag color="red">P1 × {summary.P1}</Tag>
        <Tag color="orange">P2 × {summary.P2}</Tag>
        <Tag color="default">未治理 {summary.open}</Tag>
        <Tag color="green">已治理 {summary.resolved}</Tag>
      </div>
      <div className={styles.list}>
        {open.map((f) => (
          <FindingItem key={`${f.rule_id}-${f.resource_ref}`} f={f} resolved={false} />
        ))}
        {resolved.map((f) => (
          <FindingItem key={`${f.rule_id}-${f.resource_ref}`} f={f} resolved />
        ))}
      </div>
    </div>
  );
}
