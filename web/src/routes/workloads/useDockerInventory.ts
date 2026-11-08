/**
 * One explicit Docker inventory request followed by stored-record reads.
 *
 * The POST is deliberately local to Workloads. The console-wide re-read signal remains a
 * GET-only operation: it updates the records this screen displays but never starts Docker work.
 */
import { useCallback } from 'react';
import { endpoints } from '@/api/endpoints';
import type { ContainerObservationResult, DockerContainerList } from '@/api/types';
import {
  useInventoryObservation,
  type InventoryObservationHandle,
  type InventoryOutcome as InventoryOutcomeOf,
} from '@/hooks/useInventoryObservation';

export type InventoryOutcome = InventoryOutcomeOf<ContainerObservationResult>;

export type DockerInventoryHandle = InventoryObservationHandle<DockerContainerList, ContainerObservationResult>;

function isObservationResult(value: unknown): value is ContainerObservationResult {
  return typeof value === 'object'
    && value !== null
    && typeof (value as Record<string, unknown>).sweep_id === 'string'
    && typeof (value as Record<string, unknown>).status === 'string'
    && typeof (value as Record<string, unknown>).container_count === 'number'
    && Array.isArray((value as Record<string, unknown>).issues)
    && typeof (value as Record<string, unknown>).provider_version === 'string';
}

function resultPhase(result: ContainerObservationResult): 'complete' | 'partial' | 'unknown' {
  if (result.status === 'ok') return result.issues.length === 0 ? 'complete' : 'partial';
  if (result.status === 'partial') return 'partial';
  return 'unknown';
}

export function useDockerInventory(): DockerInventoryHandle {
  const readList = useCallback(
    (signal: AbortSignal) => endpoints.containers({ signal }),
    [],
  );
  const observe = useCallback(
    (signal: AbortSignal) => endpoints.refreshContainers({ signal }),
    [],
  );

  return useInventoryObservation({
    listKey: '/docker/containers',
    readList,
    observe,
    isResult: isObservationResult,
    resultPhase,
  });
}
