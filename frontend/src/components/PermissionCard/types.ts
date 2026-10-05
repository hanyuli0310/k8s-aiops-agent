export interface PermissionCardProps {
  requestId: string;
  tool: string;
  /** 人类可读摘要，来自后端 ToolSpec.audit_repr */
  summary: string;
  args: Record<string, unknown>;
  isDestructive: boolean;
  /** 后端确认超时（秒），用于倒计时 */
  timeoutS: number;
  /** 卡片创建时刻（毫秒），倒计时基准 */
  createdAt: number;
  /** 后端已推 permission_expiring —— 即将超时 */
  expiring: boolean;
  onRespond: (approved: boolean, remember: boolean) => void;
}
