/** ControlPanel（U13）：最近治理动作展示（故障注入与世界状态已拆至 FaultDrillPanel） */
import { Tag } from 'antd';
import type { FaultPanelProps } from './types';
import styles from './index.module.css';

export default function ControlPanel({ world }: FaultPanelProps): JSX.Element {
  return (
    <div className={styles.panel}>
      <div className={styles.section}>
        <div className={styles.sectionTitle}>🔧 最近治理动作（mock 已生效）</div>
        <div className={styles.actionLog}>
          {(world?.recent_actions ?? []).slice(0, 8).map((a, i) => (
            <div key={i}>
              <Tag>{a.action_type}</Tag> {a.target} — {a.effect}
            </div>
          ))}
          {(world?.recent_actions ?? []).length === 0 && '暂无'}
        </div>
      </div>
    </div>
  );
}
