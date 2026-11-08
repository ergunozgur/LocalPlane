/**
 * One explicit systemd loaded-unit observation followed by stored-record reads.
 *
 * The POST asks the agent to read the bounded loaded-unit estate now; the visible list is
 * updated only after a successful stored-record GET.
 */
import { useCallback } from 'react';
import { endpoints } from '@/api/endpoints';
import type { SystemdObservationResult, SystemdUnitList } from '@/api/types';
import {
  useInventoryObservation,
  type InventoryObservationHandle,
  type InventoryOutcome,
} from '@/hooks/useInventoryObservation';

export type { InventoryOutcome };

export type SystemdObservationHandle = InventoryObservationHandle<SystemdUnitList, SystemdObservationResult>;

function isObservationResult(value: unknown): value is SystemdObservationResult {
  return typeof value === 'object'
    && value !== null
    && typeof (value as Record<string, unknown>).sweep_id === 'string'
    && typeof (value as Record<string, unknown>).status === 'string'
    && typeof (value as Record<string, unknown>).unit_count === 'number'
    && Array.isArray((value as Record<string, unknown>).issues)
    && typeof (value as Record<string, unknown>).provider_version === 'string';
}

export function useSystemdObservation(): SystemdObservationHandle {
  const readList = useCallback(
    (signal: AbortSignal) => endpoints.systemdUnits({ signal }),
    [],
  );
  const observe = useCallback(
    (signal: AbortSignal) => endpoints.refreshSystemd({ signal }),
    [],
  );

  return useInventoryObservation({
    listKey: '/systemd/units',
    readList,
    observe,
    isResult: isObservationResult,
  });
}
