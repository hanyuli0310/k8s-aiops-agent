import type { AgentInfo, SkillInfo, SkillStats, ToolSafety } from '../../types';

export interface AssetPanelProps {
  /** 方法论 Skill 清单（来自 /api/status 的 skills） */
  skills: SkillInfo[];
  /** 三层披露的体量统计，用于展示常驻成本 */
  stats?: SkillStats;
  /** 专家 Agent 清单（来自 /api/status 的 agents） */
  agents: AgentInfo[];
  /** 工具安全属性，用于给 Agent 的工具标签按只读/写/破坏性上色 */
  toolSafety?: ToolSafety[];
}
