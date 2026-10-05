/** GovernancePanel（U15）：扫描报告 + 治理方案生成 + check 列表确认执行 */
import { useCallback, useEffect, useState } from 'react';
import { Button, Checkbox, Empty, Spin, Tag, message } from 'antd';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { markdownComponents } from '../MermaidBlock';
import { createGovernancePlan, executeGovernancePlan, fetchScanReports } from '../../services/api';
import type { ExecuteResult, GovernancePlan, ScanReport } from '../../types';
import type { GovernancePanelProps } from './types';
import styles from './index.module.css';

const RISK_COLOR: Record<string, string> = { 低: 'green', 中: 'orange', 高: 'red' };

export default function GovernancePanel({ onGoverned }: GovernancePanelProps): JSX.Element {
  const [reports, setReports] = useState<ScanReport[]>([]);
  const [plan, setPlan] = useState<GovernancePlan | null>(null);
  const [checked, setChecked] = useState<string[]>([]);
  const [planning, setPlanning] = useState<boolean>(false);
  const [executing, setExecuting] = useState<boolean>(false);
  const [result, setResult] = useState<ExecuteResult | null>(null);

  const loadReports = useCallback(async (): Promise<void> => {
    try {
      setReports(await fetchScanReports(10));
    } catch {
      // 后端未就绪时静默
    }
  }, []);

  useEffect(() => {
    void loadReports();
    const timer = setInterval(() => void loadReports(), 10000);
    return () => clearInterval(timer);
  }, [loadReports]);

  const doPlan = async (): Promise<void> => {
    setPlanning(true);
    setResult(null);
    try {
      const p = await createGovernancePlan();
      if (p.error) {
        message.info(p.error);
        setPlan(null);
      } else {
        setPlan(p);
        setChecked(p.checklist.filter((c) => c.default_checked).map((c) => c.item_id));
      }
    } finally {
      setPlanning(false);
    }
  };

  const doExecute = async (): Promise<void> => {
    if (!plan || checked.length === 0) return;
    setExecuting(true);
    try {
      const r = await executeGovernancePlan(plan.plan_id, checked);
      if (r.error) {
        message.error(r.error);
      } else {
        setResult(r);
        message.success(`治理执行完成：${r.plan_status}，已触发复扫`);
        void loadReports();
        onGoverned?.();
      }
    } finally {
      setExecuting(false);
    }
  };

  return (
    <div className={styles.wrap}>
      <div className={styles.section}>
        <div className={styles.sectionTitle}>
          📋 历史扫描报告（60s 定时扫描）
          <Button size="small" onClick={() => void loadReports()}>刷新</Button>
        </div>
        {reports.length === 0 ? (
          <Empty description="暂无扫描报告（live 模式下每 60s 生成一条）" />
        ) : (
          reports.map((r) => (
            <div key={r.id} className={styles.reportRow}>
              <span>#{r.id}</span>
              <span>{new Date(r.scan_ts).toLocaleTimeString('zh-CN', { hour12: false })}</span>
              <Tag>{r.trigger}</Tag>
              <span>open <b>{r.total_findings}</b></span>
              {r.new_findings > 0 && <Tag color="red">+{r.new_findings} 新增</Tag>}
              {r.resolved > 0 && <Tag color="green">-{r.resolved} 已恢复</Tag>}
              {r.summary?.new?.length > 0 && (
                <span className={styles.newFinding}>{r.summary.new.join('、')}</span>
              )}
            </div>
          ))
        )}
      </div>

      <div className={styles.section}>
        <div className={styles.sectionTitle}>
          🛠️ 治理方案
          <Button type="primary" size="small" loading={planning} onClick={() => void doPlan()}>
            生成治理方案
          </Button>
          {plan && (
            <Button
              size="small"
              danger
              loading={executing}
              disabled={checked.length === 0}
              onClick={() => void doExecute()}
            >
              确认执行勾选项（{checked.length}）
            </Button>
          )}
        </div>
        {planning && <Spin tip="Agent 正在生成方案..." />}
        {plan && (
          <>
            <div className={styles.solution}>
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                {plan.solution_md}
              </ReactMarkdown>
            </div>
            <Checkbox.Group
              style={{ display: 'block' }}
              value={checked}
              onChange={(v) => setChecked(v as string[])}
            >
              {plan.checklist.map((c) => {
                const itemResult = result?.results.find((x) => x.item_id === c.item_id);
                return (
                  <div key={c.item_id} className={styles.checkItem}>
                    <Checkbox value={c.item_id} disabled={executing} />
                    <Tag color={RISK_COLOR[c.risk] ?? 'default'}>{c.risk}风险</Tag>
                    <Tag>{c.finding_rule}</Tag>
                    <span className={styles.checkDesc}>
                      {c.desc}
                      <span className={styles.muted}>（{c.tool}）</span>
                      {itemResult && (
                        <div className={styles.resultRow}>
                          {itemResult.status === 'success' ? '✅' : '❌'} {itemResult.message}
                          {itemResult.mock_feedback && (
                            <span className={styles.muted}> ｜ mock: {itemResult.mock_feedback}</span>
                          )}
                        </div>
                      )}
                    </span>
                  </div>
                );
              })}
            </Checkbox.Group>
          </>
        )}
        {!plan && !planning && (
          <Empty description="点击「生成治理方案」，Agent 将基于当前 open 风险生成方案与 check 列表" />
        )}
      </div>
    </div>
  );
}
