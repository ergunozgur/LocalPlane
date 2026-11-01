/**
 * One explicit Docker inventory request followed by stored-record reads.
 *
 * The POST is deliberately local to Workloads.  The console-wide re-read signal remains a
 * GET-only operation: it updates the records this screen displays but never starts Docker work.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '@/api/client';
import { endpoints } from '@/api/endpoints';
import type { ContainerObservationResult, DockerContainerList } from '@/api/types';
import { useRefreshVersion, type Resource } from '@/hooks/useResource';

export type InventoryOutcome =
  | { phase: 'observing' }
  | { phase: 'reading'; result: ContainerObservationResult }
  | { phase: 'complete' | 'partial' | 'unknown'; result: ContainerObservationResult; list: DockerContainerList }
  | { phase: 'observation_failed' | 'observation_uncertain'; error: ApiError }
  | { phase: 'reread_failed'; result: ContainerObservationResult; error: ApiError };

interface StoredList {
  data: DockerContainerList;
  fetchedAt: Date;
}

type StoredRead =
  | { kind: 'success'; list: DockerContainerList }
  | { kind: 'failed'; error: ApiError }
  | { kind: 'stale' };

function asApiError(cause: unknown, path: string): ApiError {
  if (cause instanceof ApiError) return cause;
  return new ApiError({
    kind: 'malformed',
    message: cause instanceof Error ? cause.message : 'unexpected failure',
    path,
  });
}

function isObservationResult(value: ContainerObservationResult): boolean {
  return typeof value.sweep_id === 'string'
    && typeof value.status === 'string'
    && typeof value.container_count === 'number'
    && Array.isArray(value.issues)
    && typeof value.provider_version === 'string';
}

function resultPhase(result: ContainerObservationResult): 'complete' | 'partial' | 'unknown' {
  // Preserve a provider token this build does not understand even when it included issues.
  if (result.status === 'ok') return result.issues.length === 0 ? 'complete' : 'partial';
  if (result.status === 'partial') return 'partial';
  return 'unknown';
}

export interface DockerInventoryHandle {
  readonly resource: Resource<DockerContainerList>;
  readonly outcome: InventoryOutcome | null;
  /** A failed console-wide re-read, kept separate from any acknowledged POST result. */
  readonly storedReadError: ApiError | null;
  /** Starts the one Docker-reading POST. Synchronously guarded against a second activation. */
  readonly refreshInventory: () => void;
  /** Reads stored records only. It never repeats the Docker observation. */
  readonly retryStoredRead: () => void;
}

export function useDockerInventory(): DockerInventoryHandle {
  const refreshVersion = useRefreshVersion();
  const [resource, setResource] = useState<Resource<DockerContainerList>>({ status: 'loading' });
  const [outcome, setOutcome] = useState<InventoryOutcome | null>(null);
  const [storedReadError, setStoredReadError] = useState<ApiError | null>(null);
  const stored = useRef<StoredList | null>(null);
  const alive = useRef(true);
  const readSequence = useRef(0);
  const flow = useRef(0);
  const observationInFlight = useRef(false);
  const storedControllers = useRef(new Set<AbortController>());
  const postControllers = useRef(new Set<AbortController>());

  const startStoredRead = useCallback(async (): Promise<StoredRead> => {
    const requestSequence = ++readSequence.current;
    const previous = stored.current;
    if (previous) {
      setResource({ status: 'success', data: previous.data, fetchedAt: previous.fetchedAt, refreshing: true });
    } else {
      setResource({ status: 'loading' });
    }

    const controller = new AbortController();
    storedControllers.current.add(controller);
    try {
      const data = await endpoints.containers({ signal: controller.signal });
      if (!alive.current || requestSequence !== readSequence.current) return { kind: 'stale' };
      const fetchedAt = new Date();
      stored.current = { data, fetchedAt };
      setStoredReadError(null);
      setResource({ status: 'success', data, fetchedAt, refreshing: false });
      return { kind: 'success', list: data };
    } catch (cause) {
      const error = asApiError(cause, '/docker/containers');
      if (!alive.current || requestSequence !== readSequence.current || error.kind === 'cancelled' || controller.signal.aborted) {
        return { kind: 'stale' };
      }
      setStoredReadError(error);
      if (previous) {
        setResource({ status: 'success', data: previous.data, fetchedAt: previous.fetchedAt, refreshing: false });
      } else {
        setResource({ status: 'failed', error, retry: () => void startStoredRead() });
      }
      return { kind: 'failed', error };
    } finally {
      storedControllers.current.delete(controller);
    }
  }, []);

  const settleReadingOutcome = useCallback((read: StoredRead) => {
    if (read.kind === 'success') {
      setOutcome((current) => (current?.phase === 'reading' || current?.phase === 'reread_failed')
        ? { phase: resultPhase(current.result), result: current.result, list: read.list }
        : current);
    } else if (read.kind === 'failed') {
      setOutcome((current) => current?.phase === 'reading'
        ? { phase: 'reread_failed', result: current.result, error: read.error }
        : current);
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    const storedRequests = storedControllers.current;
    const postRequests = postControllers.current;
    return () => {
      alive.current = false;
      for (const controller of storedRequests) controller.abort();
      for (const controller of postRequests) controller.abort();
      storedRequests.clear();
      postRequests.clear();
    };
  }, []);

  useEffect(() => {
    // The initial value and every console-wide Re-read take this GET-only route.  A later
    // request wins by sequence, so a browser that settles an aborted request late cannot
    // replace newer stored evidence or an acknowledged POST flow.
    void startStoredRead().then(settleReadingOutcome);
  }, [refreshVersion, settleReadingOutcome, startStoredRead]);

  const refreshInventory = useCallback(() => {
    if (observationInFlight.current) return;
    observationInFlight.current = true;
    const thisFlow = ++flow.current;
    // An explicit POST supersedes an initial/global list request, but a global re-read never
    // aborts or repeats the POST itself.
    readSequence.current += 1;
    for (const controller of storedControllers.current) controller.abort();
    storedControllers.current.clear();
    setOutcome({ phase: 'observing' });
    setStoredReadError(null);
    const previous = stored.current;
    if (previous) {
      setResource({ status: 'success', data: previous.data, fetchedAt: previous.fetchedAt, refreshing: true });
    }

    void (async () => {
      const controller = new AbortController();
      postControllers.current.add(controller);
      try {
        const result = await endpoints.refreshContainers({ signal: controller.signal });
        if (!alive.current || thisFlow !== flow.current) return;
        if (!isObservationResult(result)) {
          const error = new ApiError({
            kind: 'malformed',
            message: 'the inventory response could not be read as the declared result',
            path: '/docker/containers/observations/refresh',
          });
          setOutcome({ phase: 'observation_uncertain', error });
          // A pure stored read may reveal new evidence, but cannot turn this lost response into
          // an acknowledged result or invent its timestamp.
          void startStoredRead().then(settleReadingOutcome);
          return;
        }
        setOutcome({ phase: 'reading', result });
        const read = await startStoredRead();
        if (!alive.current || thisFlow !== flow.current || read.kind === 'stale') return;
        if (read.kind === 'success') {
          setOutcome({ phase: resultPhase(result), result, list: read.list });
        } else {
          setOutcome({ phase: 'reread_failed', result, error: read.error });
        }
      } catch (cause) {
        const error = asApiError(cause, '/docker/containers/observations/refresh');
        if (alive.current && thisFlow === flow.current && error.kind !== 'cancelled' && !controller.signal.aborted) {
          setOutcome({
            phase: error.kind === 'unreachable' || error.kind === 'timeout' || error.kind === 'malformed'
              ? 'observation_uncertain'
              : 'observation_failed',
            error,
          });
          // A global stored-record GET can have won while the POST was in flight. Restore the
          // newest retained evidence, not the pre-POST list captured above.
          const latest = stored.current;
          if (latest) setResource({ status: 'success', data: latest.data, fetchedAt: latest.fetchedAt, refreshing: false });
        }
      } finally {
        postControllers.current.delete(controller);
        if (thisFlow === flow.current) observationInFlight.current = false;
      }
    })();
  }, [settleReadingOutcome, startStoredRead]);

  const retryStoredRead = useCallback(() => {
    const currentOutcome = outcome;
    if (currentOutcome?.phase === 'reread_failed') setOutcome({ phase: 'reading', result: currentOutcome.result });
    void startStoredRead().then(settleReadingOutcome);
  }, [outcome, settleReadingOutcome, startStoredRead]);

  return { resource, outcome, storedReadError, refreshInventory, retryStoredRead };
}
