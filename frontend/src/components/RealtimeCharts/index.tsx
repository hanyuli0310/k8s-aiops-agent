/** RealtimeCharts（U14）：实时水位 / 容量 / 带宽折线图（@ant-design/plots，5s 轮询） */
import { useEffect, useMemo, useState } from 'react';
import { Empty, Select, Spin } from 'antd';
import { Line } from '@ant-design/plots';
import { fetchRealtime } from '../../services/api';
import type { RealtimeData } from '../../types';
import type { RealtimeChartsProps } from './types';
import styles from './index.module.css';

/** 图表分组：标题 + 该组包含的指标 + 值单位 */
const CHART_GROUPS: { title: string; metrics: string[]; unit: string }[] = [
  { title: '水位 CPU / 内存 / 连接 (%)', metrics: ['cpu_pct', 'mem_pct', 'conn_pct'], unit: '%' },
  { title: '容量使用率 (%)', metrics: ['capacity_pct'], unit: '%' },
  { title: '带宽 进 / 出 (Mbps)', metrics: ['bandwidth_in_mbps', 'bandwidth_out_mbps'], unit: 'Mbps' },
];

interface ChartRow {
  time: string;
  value: number;
  series: string;
}

export default function RealtimeCharts({ pollMs = 5000 }: RealtimeChartsProps): JSX.Element {
  const [data, setData] = useState<RealtimeData | null>(null);
  const [service, setService] = useState<string | undefined>(undefined);
  const [loading, setLoading] = useState<boolean>(true);

  useEffect(() => {
    let alive = true;
    const load = async (): Promise<void> => {
      try {
        const d = await fetchRealtime(service, 10);
        if (alive) setData(d);
      } catch {
        // 保持上次数据，避免闪断
      } finally {
        if (alive) setLoading(false);
      }
    };
    void load();
    const timer = setInterval(() => void load(), pollMs);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [service, pollMs]);

  const chartRows = useMemo((): ChartRow[][] => {
    if (!data?.series) return CHART_GROUPS.map(() => []);
    return CHART_GROUPS.map((group) => {
      const rows: ChartRow[] = [];
      for (const s of data.series) {
        if (!group.metrics.includes(s.metric)) continue;
        for (const [ts, v] of s.points) {
          rows.push({
            time: new Date(ts).toLocaleTimeString('zh-CN', { hour12: false }),
            value: Math.round(v * 100) / 100,
            series: `${s.instance.slice(0, 24)}.${s.metric}`,
          });
        }
      }
      return rows;
    });
  }, [data]);

  return (
    <div className={styles.wrap}>
      <div className={styles.header}>
        <span className={styles.title}>📈 实时指标（最近 10 分钟，10s 采集 / 5s 刷新）</span>
        <Select
          allowClear
          placeholder="全部服务"
          size="small"
          style={{ minWidth: 200 }}
          value={service}
          options={(data?.services ?? []).map((s) => ({ label: s, value: s }))}
          onChange={(v: string | undefined) => setService(v)}
        />
        {loading && <Spin size="small" />}
      </div>
      {data && data.row_count > 0 ? (
        <div className={styles.chartGrid}>
          {CHART_GROUPS.map((g, i) => (
            <div key={g.title} className={styles.chartCard}>
              <div className={styles.chartTitle}>{g.title}</div>
              <Line
                data={chartRows[i]}
                xField="time"
                yField="value"
                seriesField="series"
                colorField="series"
                height={220}
                legend={false}
                animate={false}
                axis={{ x: { labelAutoHide: true, tickCount: 6 } }}
              />
            </div>
          ))}
        </div>
      ) : (
        <Empty description="暂无实时数据（请确认 mock_server 与 data_collector 已启动，DATA_SOURCE=live）" />
      )}
    </div>
  );
}
