/** services 层：唯一 API 入口（SSE 流式对话 + 辅助查询 + 动态闭环控制台） */
import type {
  AuditData,
  ChatEvent,
  ExecuteResult,
  FaultScenario,
  GovernancePlan,
  PermissionMode,
  PrediagnosisData,
  RealtimeData,
  RiskReportData,
  ScanReport,
  SkillContent,
  StatusData,
  TopologyData,
  WorldStatus,
} from '../types';

const BASE = '/api';

/** POST /api/chat：解析 SSE 流，每个事件回调 onEvent。mode 为运行模式（权限门禁） */
export async function streamChat(
  sessionId: string,
  message: string,
  onEvent: (ev: ChatEvent) => void,
  signal?: AbortSignal,
  mode?: PermissionMode,
): Promise<void> {
  const resp = await fetch(`${BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message, mode }),
    signal,
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`chat 请求失败: ${resp.status}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split('\n\n');
    buffer = parts.pop() ?? '';
    for (const part of parts) {
      const line = part.trim();
      if (!line.startsWith('data:')) continue;
      try {
        onEvent(JSON.parse(line.slice(5)) as ChatEvent);
      } catch {
        // 忽略无法解析的心跳行
      }
    }
  }
}

/** POST /api/chat/stop：中止指定会话正在进行的 Agent 运行 */
export async function stopChat(sessionId: string): Promise<{ stopped: boolean }> {
  const resp = await fetch(`${BASE}/chat/stop?session_id=${encodeURIComponent(sessionId)}`, {
    method: 'POST',
  });
  return (await resp.json()) as { stopped: boolean };
}

/** POST /api/chat/approve：回应权限确认卡片，唤醒阻塞中的 Agent */
export async function approveTool(
  requestId: string,
  approved: boolean,
  remember = false,
): Promise<{ had_waiter: boolean }> {
  const resp = await fetch(`${BASE}/chat/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ request_id: requestId, approved, remember }),
  });
  return (await resp.json()) as { had_waiter: boolean };
}

/** GET /api/audit：工具调用审计 */
export async function fetchAudit(
  limit = 100,
  destructiveOnly = false,
): Promise<AuditData> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (destructiveOnly) params.set('destructive_only', 'true');
  const resp = await fetch(`${BASE}/audit?${params.toString()}`);
  return (await resp.json()) as AuditData;
}

export async function fetchStatus(): Promise<StatusData> {
  const resp = await fetch(`${BASE}/status`);
  return (await resp.json()) as StatusData;
}

export async function fetchTopology(): Promise<TopologyData> {
  const resp = await fetch(`${BASE}/topology`);
  return (await resp.json()) as TopologyData;
}

export async function fetchRisks(): Promise<RiskReportData> {
  const resp = await fetch(`${BASE}/risks`);
  return (await resp.json()) as RiskReportData;
}

/** —— 动态闭环控制台（spec v1.1 契约 B）—— */

export async function fetchScenarios(): Promise<{ scenarios: FaultScenario[]; error?: string }> {
  const resp = await fetch(`${BASE}/fault/scenarios`);
  return resp.json();
}

export async function injectFault(scenarioId: string): Promise<{ fault_id?: string; error?: string }> {
  const resp = await fetch(`${BASE}/fault/inject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scenario_id: scenarioId }),
  });
  return resp.json();
}

export async function recoverFault(faultId: string): Promise<{ status?: string; error?: string }> {
  const resp = await fetch(`${BASE}/fault/recover`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ fault_id: faultId }),
  });
  return resp.json();
}

export async function fetchWorld(): Promise<WorldStatus> {
  const resp = await fetch(`${BASE}/world`);
  return resp.json();
}

export async function fetchRealtime(service?: string, minutes = 10): Promise<RealtimeData> {
  const params = new URLSearchParams();
  if (service) params.set('service', service);
  params.set('minutes', String(minutes));
  const resp = await fetch(`${BASE}/realtime?${params.toString()}`);
  return resp.json();
}

/** GET /api/prediagnosis：自主预诊断结果 */
export async function fetchPrediagnosis(limit = 20): Promise<PrediagnosisData> {
  const resp = await fetch(`${BASE}/prediagnosis?limit=${limit}`);
  return (await resp.json()) as PrediagnosisData;
}

/** GET /api/skill/{name}：取 Skill 正文（reference 传细则名则取细则） */
export async function fetchSkillContent(
  name: string,
  reference?: string,
): Promise<SkillContent> {
  const qs = reference ? `?reference=${encodeURIComponent(reference)}` : '';
  const resp = await fetch(`${BASE}/skill/${encodeURIComponent(name)}${qs}`);
  if (!resp.ok) {
    throw new Error(`读取 Skill 失败: ${resp.status}`);
  }
  return (await resp.json()) as SkillContent;
}

export async function fetchScanReports(limit = 20): Promise<ScanReport[]> {
  const resp = await fetch(`${BASE}/scan-reports?limit=${limit}`);
  return resp.json();
}

export async function createGovernancePlan(): Promise<GovernancePlan> {
  const resp = await fetch(`${BASE}/governance/plan`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  return resp.json();
}

export async function executeGovernancePlan(planId: number, itemIds: string[]): Promise<ExecuteResult> {
  const resp = await fetch(`${BASE}/governance/execute`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ plan_id: planId, item_ids: itemIds }),
  });
  return resp.json();
}
