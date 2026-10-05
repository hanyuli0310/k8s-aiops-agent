/** FaultDrillPanel：故障演练 Tab —— 故障场景注入 + 世界状态摘要（自 ControlPanel 拆出） */
import { useState } from 'react';
import { Button, Empty, Tag, message } from 'antd';
import { ThunderboltOutlined } from '@ant-design/icons';
import { injectFault, recoverFault } from '../../services/api';
import type { FaultDrillPanelProps } from './types';
import styles from './index.module.css';

export default function FaultDrillPanel({ scenarios, world, onChanged }: FaultDrillPanelProps): JSX.Element {
  const [busy, setBusy] = useState<string>('');

  const doInject = async (scenarioId: string): Promise<void> => {
    setBusy(scenarioId);
    try {
      const r = await injectFault(scenarioId);
      if (r.error) {
        message.error(r.error);
      } else {
        message.warning(`故障已注入：${r.fault_id}（约 1~2 分钟后告警出现）`);
        onChanged();
      }
    } finally {
      setBusy('');
    }
  };

  const doRecover = async (faultId: string): Promise<void> => {
    setBusy(faultId);
    try {
      const r = await recoverFault(faultId);
      if (r.error) {
        message.error(r.error);
      } else {
        message.success(`故障恢复中：${faultId}（指标按半衰期回落）`);
        onChanged();
      }
    } finally {
      setBusy('');
    }
  };

  const activeFaults = world?.active_faults ?? [];
  const activeScenarioIds = new Set(activeFaults.map((f) => f.scenario_id));

  return (
    <div className={styles.panel}>
      <div className={styles.section}>
        <div className={styles.sectionTitle}>
          <ThunderboltOutlined /> 故障场景注入
          {activeFaults.length > 0 && <Tag color="red">{activeFaults.length} 个活动故障</Tag>}
        </div>
        {activeFaults.map((f) => (
          <div key={f.fault_id} className={styles.activeFault}>
            🔥 <b>{f.title}</b>（{f.fault_id}）影响：{f.affected.join(' → ')}
            <Button
              size="small"
              danger
              style={{ marginLeft: 12 }}
              loading={busy === f.fault_id}
              onClick={() => void doRecover(f.fault_id)}
            >
              恢复故障
            </Button>
          </div>
        ))}
        <div className={styles.scenarioGrid}>
          {scenarios.map((s) => (
            <div key={s.id} className={styles.scenarioCard}>
              <div className={styles.scenarioTitle}>{s.title}</div>
              <div className={styles.scenarioDesc}>{s.description}</div>
              <div className={styles.scenarioFooter}>
                <span>
                  {s.expected_rules.map((r) => (
                    <Tag key={r} color="orange">{r}</Tag>
                  ))}
                </span>
                <Button
                  size="small"
                  type="primary"
                  danger
                  loading={busy === s.id}
                  disabled={activeScenarioIds.has(s.id)}
                  onClick={() => void doInject(s.id)}
                >
                  {activeScenarioIds.has(s.id) ? '已注入' : '注入'}
                </Button>
              </div>
            </div>
          ))}
          {scenarios.length === 0 && <Empty description="mock_server 未连接或无场景" />}
        </div>
      </div>

      <div className={styles.section}>
        <div className={styles.sectionTitle}>🌍 世界状态</div>
        {world && !world.error ? (
          <>
            <div className={styles.worldMeta}>
              <span>tick #{world.tick}</span>
              <span>world_version {world.world_version}</span>
              <span>入口流量 {world.entry_rps} RPS</span>
              <span>
                CPU 超卖率 <b>{world.cpu_oversale_pct}%</b>
                {world.cpu_oversale_pct > 150 && <Tag color="red">超阈</Tag>}
              </span>
            </div>
            <div className={styles.svcTable}>
              {world.services.map((s) => (
                <div key={s.name} className={styles.svcItem}>
                  <span>{s.name}</span>
                  <span>
                    {s.healthy_replicas}/{s.replicas} 副本
                    {s.healthy_replicas < s.replicas && <Tag color="red">异常</Tag>}
                  </span>
                </div>
              ))}
            </div>
          </>
        ) : (
          <Empty description={world?.error ?? '加载中...'} />
        )}
      </div>
    </div>
  );
}
