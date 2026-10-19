import type { SystemdServiceOperation, SystemdUnit } from '@/api/types';
import { OPERATIONS_WITH_UI_CONTROLS } from './execution';

export type ServiceOperation = SystemdServiceOperation['type'];
export type ServiceIntent =
  | { state: 'none' }
  | { state: 'invalid'; reason: string }
  | { state: 'staged'; operation: ServiceOperation };

export function serviceIntentUrl(objectId: string, operation: ServiceOperation): string {
  const params = new URLSearchParams({ tab: 'lifecycle', operation, target: objectId });
  return `/system/${encodeURIComponent(objectId)}?${params}`;
}

/** A navigation proposal, never permission to plan or execute. Validate against detail. */
export function serviceIntent(
  params: URLSearchParams, objectId: string, unit: SystemdUnit, refreshing: boolean,
): ServiceIntent {
  if (!params.has('operation') && !params.has('target')) return { state: 'none' };
  const operation = OPERATIONS_WITH_UI_CONTROLS.find((type) => type === params.get('operation'));
  if (!operation || params.getAll('operation').length !== 1 || params.getAll('target').length !== 1 ||
      params.get('target') !== objectId || unit.object_id !== objectId) {
    return { state: 'invalid', reason: 'The service action link is invalid or names a different target. Choose the action again from search.' };
  }
  if (unit.unit_type !== 'service' || unit.load_state !== 'loaded') {
    return { state: 'invalid', reason: 'This action no longer targets a loaded service. Choose the action again from search.' };
  }
  if (refreshing || unit.observation?.freshness !== 'current' || unit.observed_in_latest_sweep !== true) {
    return { state: 'invalid', reason: 'Current service evidence is unavailable or being refreshed. The staged action cannot be planned from stale or unknown evidence.' };
  }
  return { state: 'staged', operation };
}
