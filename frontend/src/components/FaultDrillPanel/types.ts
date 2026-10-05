import type { FaultScenario, WorldStatus } from '../../types';

export interface FaultDrillPanelProps {
  scenarios: FaultScenario[];
  world: WorldStatus | null;
  onChanged: () => void;
}
