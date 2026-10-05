/** TopologyGraph：服务拓扑图（AntV G6 v4，dagre 布局，异常边标红） */
import { useEffect, useRef } from 'react';
import G6, { type Graph } from '@antv/g6';
import type { TopologyEdge, TopologyNode } from '../../types';
import type { TopologyGraphProps } from './types';
import styles from './index.module.css';

const NODE_COLORS: Record<string, { fill: string; stroke: string }> = {
  service: { fill: '#e8f3ff', stroke: '#1664ff' },
  mysql: { fill: '#fff7e8', stroke: '#ff7d00' },
  redis: { fill: '#e8ffea', stroke: '#00b42a' },
};

function nodeStyle(n: TopologyNode): { fill: string; stroke: string } {
  if (n.type && NODE_COLORS[n.type]) return NODE_COLORS[n.type];
  if (n.id.includes('mysql')) return NODE_COLORS.mysql;
  if (n.id.includes('redis')) return NODE_COLORS.redis;
  return NODE_COLORS.service;
}

function edgeColor(e: TopologyEdge): string {
  return e.error_rate > 0.01 ? '#f53f3f' : '#c9cdd4';
}

export default function TopologyGraph({ data, height = 360 }: TopologyGraphProps): JSX.Element {
  const containerRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<Graph | null>(null);

  useEffect(() => {
    if (!containerRef.current || !data.edges?.length) return undefined;
    const width = containerRef.current.clientWidth || 640;
    const graph = new G6.Graph({
      container: containerRef.current,
      width,
      height,
      fitView: true,
      fitViewPadding: 24,
      layout: { type: 'dagre', rankdir: 'LR', nodesep: 18, ranksep: 48 },
      modes: { default: ['drag-canvas', 'zoom-canvas', 'drag-node'] },
      defaultNode: {
        type: 'rect',
        size: [128, 36],
        style: { radius: 8, lineWidth: 1.5 },
        labelCfg: { style: { fontSize: 11 } },
      },
      defaultEdge: {
        type: 'quadratic',
        style: { endArrow: true, lineWidth: 1.5 },
        labelCfg: { autoRotate: true, style: { fontSize: 9, fill: '#86909c' } },
      },
    });
    graph.data({
      nodes: data.nodes.map((n) => {
        const s = nodeStyle(n);
        return {
          id: n.id,
          label: n.replicas != null ? `${n.id}\n×${n.replicas}` : n.id,
          style: { fill: s.fill, stroke: s.stroke },
        };
      }),
      edges: data.edges.map((e) => ({
        source: e.source,
        target: e.target,
        label: `${e.call_count}次 | 错误率${(e.error_rate * 100).toFixed(2)}% | P99 ${e.p99_ms}ms`,
        style: {
          stroke: edgeColor(e),
          lineWidth: e.error_rate > 0.01 ? 2.5 : 1.5,
          endArrow: true,
        },
      })),
    });
    graph.render();
    graphRef.current = graph;
    return () => {
      graph.destroy();
      graphRef.current = null;
    };
  }, [data, height]);

  return (
    <div className={styles.container}>
      <div className={styles.title}>服务调用拓扑（{data.nodes?.length ?? 0} 节点 / {data.edges?.length ?? 0} 边，红色为异常边）</div>
      <div ref={containerRef} className={styles.canvas} style={{ height }} />
    </div>
  );
}
