/** 后端 SSE 事件流与业务数据类型定义 */

export type ChatEventType =
  | 'intent'
  | 'agent_start'
  | 'phase'
  | 'phase_result'
  | 'thinking'
  | 'tool_call'
  | 'tool_result'
  | 'answer'
  | 'error'
  | 'done'
  // —— Harness 第 2 步（可中断 + 自愈）——
  | 'aborted'          // 用户中断 / 超预算
  | 'compacted'        // 上下文紧急压缩后重试
  | 'model_fallback'   // 主模型不可用，已降级
  | 'usage'            // token 用量与耗时（每步一次）
  // —— Harness 第 3 步（权限门禁）——
  | 'permission_request'    // 需要用户确认才能执行
  | 'permission_expiring'   // 确认即将超时
  | 'permission_granted'    // 已批准
  // —— Harness 第 5 步（子 Agent）——
  | 'subagent_start'        // 派发子 Agent
  | 'subagent_done'         // 子 Agent 完成
  // —— Harness 第 9 步（E-2 任务清单与分段续跑）——
  | 'plan_update'           // 模型更新了任务清单
  | 'continued'             // 步数用尽但任务未完成，已交接并开新一段
  // —— Harness 第 10 步（E-4 结论事实核对）——
  | 'verify_warning';       // 回答里存在未在工具结果中出现的 traceID / 资源名

/** 疑似写错的资源名：说出来的 vs 证据里最接近的真实项 */
export interface NearMissResource {
  said: string;
  closest: string;
  distance: number;
}

/** 任务清单里的一步（由后端 update_plan 工具维护） */
export interface PlanStep {
  title: string;
  status: 'pending' | 'in_progress' | 'done';
}

/** 运行模式：只读巡检 / 确认执行 / 无人值守 */
export type PermissionMode = 'readonly' | 'confirm' | 'auto';

export interface ChatEvent {
  type: ChatEventType;
  intent?: string;
  router?: string;
  entities?: Record<string, string>;
  agent?: string;
  phase?: string;
  task?: string;
  text?: string;
  tool?: string;
  args?: Record<string, unknown>;
  result?: unknown;
  duration_ms?: number;
  // aborted / compacted
  reason?: string;
  freed_chars?: number;
  // model_fallback
  to?: string;
  // usage
  tokens_in?: number;
  tokens_out?: number;
  est_cost_cny?: number;
  budget_pct?: number;
  elapsed_s?: number;
  // permission_*
  request_id?: string;
  summary?: string;
  is_destructive?: boolean;
  timeout_s?: number;
  seconds_left?: number;
  by?: string;
  // subagent_*：子 Agent 派发与完成；透传事件也带 subagent/depth/description
  subagent?: string;
  subagent_type?: string;
  description?: string;
  depth?: number;
  tool_calls?: number;
  tokens?: number;
  // plan_update / continued（E-2）
  plan?: PlanStep[];
  segment?: number;
  // verify_warning（E-4）
  fake_ids?: string[];
  near_miss?: NearMissResource[];
  unverified_numbers?: string[];
  corrected?: boolean;
}

/** 待确认的工具调用（由 permission_request 事件生成） */
export interface PendingApproval {
  requestId: string;
  tool: string;
  summary: string;
  args: Record<string, unknown>;
  isDestructive: boolean;
  timeoutS: number;
  createdAt: number;
  expiring: boolean;
  secondsLeft?: number;
}

/** 本轮用量（由 usage 事件累积） */
export interface UsageInfo {
  tokensIn: number;
  tokensOut: number;
  estCostCny: number;
  /** 预算上限已停用时为 null —— 不能填 0，那会显示成误导性的「预算 0%」 */
  budgetPct: number | null;
  elapsedS: number;
}

export interface TopologyNode {
  id: string;
  type?: string;
  namespace?: string | null;
  replicas?: number;
}

export interface TopologyEdge {
  source: string;
  target: string;
  call_count: number;
  error_rate: number;
  avg_ms: number;
  p99_ms: number;
}

export interface TopologyData {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
}

export interface RiskFinding {
  rule_id: string;
  severity: string;
  title: string;
  resource_ref: string;
  status: string;
  suggestion: string;
  evidence: Record<string, unknown>;
}

export interface RiskReportData {
  summary: { open: number; resolved: number; P1: number; P2: number };
  open_findings: RiskFinding[];
  resolved_findings: RiskFinding[];
}

/** 一条对话消息（assistant 消息附带完整事件轨迹） */
export interface ChatMessageItem {
  role: 'user' | 'assistant';
  text: string;
  events: ChatEvent[];
  streaming: boolean;
}

export interface ToolSafety {
  name: string;
  read_only: boolean;
  writes_business_data: boolean;
  destructive: boolean;
  concurrency_safe: boolean;
  max_result_chars: number;
}

export interface PermissionStatus {
  default_mode: PermissionMode;
  modes: PermissionMode[];
  approval_timeout_s: number;
  pending_approvals: string[];
}

/** —— Skill / Agent 资产（P2-1 / P2-2）—— */

export interface SkillInfo {
  name: string;
  display_name: string;
  description: string;
  when_to_use: string;
  allowed_tools: string[];
  references: string[];
  layout: 'dir' | 'file';
  /** L1 目录行的字符数（常驻成本） */
  catalog_chars: number;
  /** L2 正文字符数（按需加载） */
  body_chars: number;
  /** L3 细则合计字符数（按需加载） */
  refs_chars: number;
}

export interface SkillStats {
  count: number;
  reference_count: number;
  catalog_chars: number;
  total_chars: number;
  catalog_pct: number;
}

export interface AgentInfo {
  key: string;
  name: string;
  description: string;
  when_to_use: string;
  model: string;
  model_tier: 'primary' | 'fast';
  /** 主 Skill（全文注入），无则为 null */
  skill: string | null;
  /** 能否被 dispatch_agent 派发 */
  dispatchable: boolean;
  tools: string[];
}

/** GET /api/skill/{name} 的返回：模型实际看到的 Skill 内容 */
export interface SkillContent {
  name: string;
  reference?: string;
  content: string;
  references?: string[];
}

export interface StatusData {
  llm_available: boolean;
  llm_model: string;
  llm_model_fast?: string;
  data_source?: string;
  db_counts: Record<string, number>;
  tools: string[];
  permission?: PermissionStatus;
  tool_safety?: ToolSafety[];
  skills?: SkillInfo[];
  skill_stats?: SkillStats;
  agents?: AgentInfo[];
}

/** —— 工具调用审计（P1-4）—— */

export interface AuditRecord {
  id: number;
  session_id: string;
  agent_name: string;
  tool_name: string;
  audit_repr: string;
  decision: 'allow' | 'ask' | 'deny';
  reason_type: string;
  run_mode: string;
  is_destructive: number;
  result_status: string;
  duration_ms: number;
  created_at: number;
  args_json: string;
}

export interface AuditSummary {
  total: number;
  destructive: number;
  denied: number;
  executed_ok: number;
  by_status: Record<string, number>;
}

export interface AuditData {
  summary: AuditSummary;
  records: AuditRecord[];
}

/** —— 自主预诊断（P2-7）：定时扫描发现新增 P1 时子 Agent 自动分析的根因 —— */

export interface PrediagnosisRecord {
  id: number;
  finding_key: string;
  rule_id: string;
  resource_ref: string;
  severity: string;
  title: string;
  conclusion: string;
  tool_calls: number;
  tokens: number;
  status: 'ok' | 'aborted' | 'error' | 'failed';
  duration_ms: number;
  created_at: number;
}

export interface PrediagnosisData {
  enabled: boolean;
  max_per_scan: number;
  records: PrediagnosisRecord[];
}

/** —— 动态闭环控制台（spec v1.1）—— */

export interface FaultScenario {
  id: string;
  title: string;
  target: string;
  description: string;
  recover_actions: string[];
  expected_rules: string[];
}

export interface ActiveFault {
  fault_id: string;
  scenario_id: string;
  title: string;
  target: string;
  affected: string[];
  status: string;
}

export interface WorldService {
  name: string;
  kind: string;
  namespace: string;
  replicas: number;
  healthy_replicas: number;
}

export interface WorldStatus {
  world_version: number;
  tick: number;
  entry_rps: number;
  cpu_oversale_pct: number;
  services: WorldService[];
  active_faults: ActiveFault[];
  recent_actions: { action_type: string; target: string; effect: string }[];
  error?: string;
}

export interface RealtimeSeries {
  name: string;
  service: string;
  instance: string;
  metric: string;
  points: [number, number][];
}

export interface RealtimeData {
  series: RealtimeSeries[];
  services: string[];
  row_count: number;
}

export interface ScanReport {
  id: number;
  scan_ts: number;
  trigger: string;
  total_findings: number;
  new_findings: number;
  resolved: number;
  summary: { new: string[]; resolved: string[]; open_titles: string[]; P1: number; P2: number };
}

export interface ChecklistItem {
  item_id: string;
  finding_rule: string;
  finding_ref: string;
  tool: string;
  desc: string;
  risk: string;
  default_checked: boolean;
}

export interface GovernancePlan {
  plan_id: number;
  solution_md: string;
  checklist: ChecklistItem[];
  finding_count: number;
  error?: string;
}

export interface ExecuteResult {
  plan_id: number;
  plan_status: string;
  results: { item_id: string; status: string; message: string; mock_feedback?: string }[];
  error?: string;
}
