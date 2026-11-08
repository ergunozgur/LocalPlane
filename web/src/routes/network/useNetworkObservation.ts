/**
 * One explicit network observation followed by stored-record reads.
 *
 * The POST asks the agent to read interfaces now; the visible list is updated only after a
 * successful stored-record GET.
 */
import { useCallback } from 'react';
import { endpoints } from '@/api/endpoints';
import type { NetworkInterfaceList, RefreshResult } from '@/api/types';
import {
  useInventoryObservation,
  type InventoryObservationHandle,
  type InventoryOutcome,
} from '@/hooks/useInventoryObservation';

export type { InventoryOutcome };

export type NetworkObservationHandle = InventoryObservationHandle<NetworkInterfaceList, RefreshResult>;

function isObservationResult(value: unknown): value is RefreshResult {
  return typeof value === 'object'
    && value !== null
    && typeof (value as Record<string, unknown>).sweep_id === 'string'
    && typeof (value as Record<string, unknown>).status === 'string'
    && typeof (value as Record<string, unknown>).object_count === 'number'
    && Array.isArray((value as Record<string, unknown>).issues);
}

export function useNetworkObservation(): NetworkObservationHandle {
  const readList = useCallback(
    (signal: AbortSignal) => endpoints.interfaces({ signal }),
    [],
  );
  const observe = useCallback(
    (signal: AbortSignal) => endpoints.refreshNetwork({ signal }),
    [],
  );

  return useInventoryObservation({
    listKey: '/network/interfaces',
    readList,
    observe,
    isResult: isObservationResult,
  });
}
