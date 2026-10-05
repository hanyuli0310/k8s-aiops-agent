/** ChatMessage：单条消息渲染（Markdown 回答 + 执行轨迹 + 拓扑/风险富卡片 + mermaid 图） */
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Alert, Spin } from 'antd';
import ToolTimeline from '../ToolTimeline';
import TopologyGraph from '../TopologyGraph';
import RiskReport from '../RiskReport';
import { markdownComponents } from '../MermaidBlock';
import type { ChatEvent, RiskReportData, TopologyData } from '../../types';
import type { ChatMessageProps } from './types';
import styles from './index.module.css';

const TOPOLOGY_TOOLS = new Set(['build_topology', 'get_topology']);
const RISK_TOOLS = new Set(['run_risk_scan', 'get_risk_report']);

function isTopologyData(v: unknown): v is TopologyData {
  return typeof v === 'object' && v !== null && Array.isArray((v as TopologyData).edges);
}

function isRiskData(v: unknown): v is RiskReportData {
  return typeof v === 'object' && v !== null && 'summary' in (v as Record<string, unknown>);
}

/** 从事件轨迹提取需要富渲染的卡片（同类取最后一次结果） */
function extractCards(events: ChatEvent[]): { topology?: TopologyData; risk?: RiskReportData } {
  let topology: TopologyData | undefined;
  let risk: RiskReportData | undefined;
  for (const ev of events) {
    if (ev.type !== 'tool_result' || !ev.tool) continue;
    if (TOPOLOGY_TOOLS.has(ev.tool) && isTopologyData(ev.result)) topology = ev.result;
    if (RISK_TOOLS.has(ev.tool) && isRiskData(ev.result)) risk = ev.result;
  }
  return { topology, risk };
}

export default function ChatMessage({ message }: ChatMessageProps): JSX.Element {
  if (message.role === 'user') {
    return (
      <div className={`${styles.row} ${styles.rowUser}`}>
        <div className={`${styles.bubble} ${styles.bubbleUser}`}>{message.text}</div>
      </div>
    );
  }
  const cards = extractCards(message.events);
  // 事实核对告警（E-4）：放在回答【上方】而不是埋进时间线 ——
  // 它是对下面这段结论可信度的限定，读者必须先看到它再看结论。
  const verify = message.events.filter((e) => e.type === 'verify_warning').slice(-1)[0];
  return (
    <div className={styles.row}>
      <div className={`${styles.bubble} ${styles.bubbleAssistant}`}>
        <ToolTimeline events={message.events} streaming={message.streaming} />
        {cards.topology && (
          <div className={styles.cards}>
            <TopologyGraph data={cards.topology} />
          </div>
        )}
        {cards.risk && (
          <div className={styles.cards}>
            <RiskReport data={cards.risk} />
          </div>
        )}
        {verify && (
          <Alert
            className={styles.verify}
            type="warning"
            showIcon
            message="结论里有未经证实的引用"
            description={
              <div className={styles.verifyBody}>
                <div>{verify.text}</div>
                {verify.corrected && (
                  <div className={styles.verifyNote}>
                    已让 Agent 自纠正过一次，但仍未消除；请以工具返回的原始数据为准。
                  </div>
                )}
                {(verify.unverified_numbers?.length ?? 0) > 0 && (
                  <div className={styles.verifyNote}>
                    另有 {verify.unverified_numbers?.length} 个数值未在工具结果中直接出现
                    （可能是换算或推算得到，仅供参考）：
                    {verify.unverified_numbers?.slice(0, 8).join('、')}
                  </div>
                )}
              </div>
            }
          />
        )}
        {message.text ? (
          <div className={styles.markdown}>
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
              {message.text}
            </ReactMarkdown>
          </div>
        ) : (
          message.streaming && (
            <div className={styles.waiting}>
              <Spin size="small" /> Agent 正在分析...
            </div>
          )
        )}
      </div>
    </div>
  );
}
