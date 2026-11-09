/**
 * One explicit inventory observation followed by stored-record reads.
 *
 * This is the generic form of the Docker flow in `routes/workloads/useDockerInventory`.
 * The POST is local to the domain; the console-wide re-read signal remains GET-only and
 * never starts provider work. One POST is in flight at a time; a later successful GET wins
 * over an earlier response; a failed POST cannot roll back newer stored evidence.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '@/api/client';
import type { ProviderIssue, ProviderReadingSummary, Sweep } from '@/api/types';
import { refreshAll, useRefreshVersion, type Resource } from './useResource';

export interface BaseObservationResult {
  sweep_id: string;
  status: string;
  issues: ProviderIssue[];
  providers?: ProviderReadingSummary[];
}

export type InventoryOutcome<Result extends BaseObservationResult> =
  | { phase: 'observing' }
  | { phase: 'reading'; result: Result }
  | { phase: 'complete' | 'partial' | 'unknown'; result: Result; list: ListWithSweep }
  | { phase: 'observation_failed' | 'observation_uncertain'; error: ApiError }
  | { phase: 'reread_failed'; result: Result; error: ApiError };

export interface InventoryObservationHandle<List, Result extends BaseObservationResult> {
  readonly resource: Resource<List>;
  readonly outcome: InventoryOutcome<Result> | null;
  /** A failed console-wide re-read, kept separate from any acknowledged POST result. */
  readonly storedReadError: ApiError | null;
  /** Starts the one provider-reading POST. Synchronously guarded against a second activation. */
  readonly refreshInventory: () => void;
  /** Reads stored records only. It never repeats the provider observation. */
  readonly retryStoredRead: () => void;
}

interface ListWithSweep {
  last_sweep: Sweep | null;
}

function asApiError(cause: unknown, path: string): ApiError {
  if (cause instanceof ApiError) return cause;
  return new ApiError({
    kind: 'malformed',
    message: cause instanceof Error ? cause.message : 'unexpected failure',
    path,
  });
}

/** Validate nested fields consumed by the shared outcome renderer, without hiding bad evidence. */
function hasReadableEvidence(value: BaseObservationResult): boolean {
  const record = (item: unknown): item is Record<string, unknown> =>
    typeof item === 'object' && item !== null && !Array.isArray(item);
  const nullableString = (item: unknown) => item === null || typeof item === 'string';
  return Array.isArray(value.issues) && value.issues.every((issue: unknown) =>
    record(issue) && typeof issue.source === 'string' && typeof issue.code === 'string'
      && typeof issue.message === 'string')
    && (value.providers === undefined || (Array.isArray(value.providers) && value.providers.every((provider: unknown) =>
      record(provider) && typeof provider.provider === 'string' && typeof provider.source === 'string'
        && typeof provider.status === 'string' && nullableString(provider.reason))));
}

function defaultResultPhase(result: BaseObservationResult): 'complete' | 'partial' | 'unknown' {
  if (result.status === 'ok') return result.issues.length === 0 ? 'complete' : 'partial';
  if (result.status === 'partial') return 'partial';
  return 'unknown';
}

export interface InventoryObservationConfig<
  List extends ListWithSweep,
  Result extends BaseObservationResult,
> {
  listKey: string;
  readList: (signal: AbortSignal) => Promise<List>;
  observe: (signal: AbortSignal) => Promise<Result>;
  isResult: (value: unknown) => value is Result;
  resultPhase?: (result: Result) => 'complete' | 'partial' | 'unknown';
}

export function useInventoryObservation<
  List extends ListWithSweep,
  Result extends BaseObservationResult,
>(config: InventoryObservationConfig<List, Result>): InventoryObservationHandle<List, Result> {
  const refreshVersion = useRefreshVersion();
  const [resource, setResource] = useState<Resource<List>>({ status: 'loading' });
  const [outcome, setOutcome] = useState<InventoryOutcome<Result> | null>(null);
  const [storedReadError, setStoredReadError] = useState<ApiError | null>(null);
  const stored = useRef<{ data: List; fetchedAt: Date } | null>(null);
  const alive = useRef(true);
  const readSequence = useRef(0);
  const flow = useRef(0);
  const observationInFlight = useRef(false);
  const storedControllers = useRef(new Set<AbortController>());
  const postControllers = useRef(new Set<AbortController>());

  // Callbacks are stored in refs so a new function identity each render does not restart the
  // read loop. The only identity that should trigger a fresh stored read is `listKey`.
  const readListRef = useRef(config.readList);
  const observeRef = useRef(config.observe);
  const isResultRef = useRef(config.isResult);
  const resultPhaseRef = useRef(config.resultPhase ?? defaultResultPhase);
  readListRef.current = config.readList;
  observeRef.current = config.observe;
  isResultRef.current = config.isResult;
  resultPhaseRef.current = config.resultPhase ?? defaultResultPhase;

  const listKey = config.listKey;
  const getPhase = useCallback(
    (result: Result) => resultPhaseRef.current(result),
    [],
  );

  const startStoredRead = useCallback(async (): Promise<StoredRead<List>> => {
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
      const data = await readListRef.current(controller.signal);
      if (!alive.current || requestSequence !== readSequence.current) return { kind: 'stale' };
      const fetchedAt = new Date();
      stored.current = { data, fetchedAt };
      setStoredReadError(null);
      setResource({ status: 'success', data, fetchedAt, refreshing: false });
      return { kind: 'success', list: data };
    } catch (cause) {
      const error = asApiError(cause, listKey);
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
  }, [listKey]);

  const settleReadingOutcome = useCallback((read: StoredRead<List>) => {
    if (read.kind === 'success') {
      setOutcome((current) => (current?.phase === 'reading' || current?.phase === 'reread_failed'
        || current?.phase === 'complete' || current?.phase === 'partial' || current?.phase === 'unknown')
        ? { phase: getPhase(current.result), result: current.result, list: read.list }
        : current);
    } else if (read.kind === 'failed') {
      setOutcome((current) => current?.phase === 'reading'
        ? { phase: 'reread_failed', result: current.result, error: read.error }
        : current);
    }
  }, [getPhase]);

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
    // The initial value and every console-wide re-read take this GET-only route. A later
    // request wins by sequence, so a browser that settles an aborted request late cannot
    // replace newer stored evidence or an acknowledged POST flow.
    void startStoredRead().then(settleReadingOutcome);
  }, [refreshVersion, listKey, settleReadingOutcome, startStoredRead]);

  const refreshInventory = useCallback(() => {
    if (observationInFlight.current) return;
    observationInFlight.current = true;
    const thisFlow = ++flow.current;
    // An explicit POST supersedes an initial/global list request, but a global re-read never
    // aborts or repeats the POST itself.
    readSequence.current += 1;
    const observationReadSequence = readSequence.current;
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
        const result = await observeRef.current(controller.signal);
        if (!alive.current || thisFlow !== flow.current) return;
        if (!isResultRef.current(result) || !hasReadableEvidence(result)) {
          const error = new ApiError({
            kind: 'malformed',
            message: 'the inventory response could not be read as the declared result',
            path: listKey,
          });
          setOutcome({ phase: 'observation_uncertain', error });
          void startStoredRead().then(settleReadingOutcome);
          return;
        }
        setOutcome({ phase: 'reading', result });
        const read = await startStoredRead();
        if (!alive.current || thisFlow !== flow.current || read.kind === 'stale') return;
        if (read.kind === 'success') {
          setOutcome({ phase: getPhase(result), result, list: read.list });
          // Stored records changed; every mounted stored resource re-reads, but global
          // refresh never starts provider work. This keeps nav/widget counts consistent.
          refreshAll();
        } else {
          setOutcome({ phase: 'reread_failed', result, error: read.error });
        }
      } catch (cause) {
        const error = asApiError(cause, listKey);
        if (alive.current && thisFlow === flow.current && error.kind !== 'cancelled' && !controller.signal.aborted) {
          setOutcome({
            phase: error.kind === 'unreachable' || error.kind === 'timeout' || error.kind === 'malformed'
              ? 'observation_uncertain'
              : 'observation_failed',
            error,
          });
          const latest = stored.current;
          if (latest) setResource({ status: 'success', data: latest.data, fetchedAt: latest.fetchedAt, refreshing: false });
          else if (readSequence.current === observationReadSequence) {
            void startStoredRead().then(settleReadingOutcome);
          }
        }
      } finally {
        postControllers.current.delete(controller);
        if (thisFlow === flow.current) observationInFlight.current = false;
      }
    })();
  }, [listKey, getPhase, settleReadingOutcome, startStoredRead]);

  const retryStoredRead = useCallback(() => {
    const currentOutcome = outcome;
    if (currentOutcome?.phase === 'reread_failed') setOutcome({ phase: 'reading', result: currentOutcome.result });
    void startStoredRead().then(settleReadingOutcome);
  }, [outcome, settleReadingOutcome, startStoredRead]);

  const exposedResource = resource.status === 'failed' ? { ...resource, retry: retryStoredRead } : resource;
  return { resource: exposedResource, outcome, storedReadError, refreshInventory, retryStoredRead };
}

type StoredRead<List> =
  | { kind: 'success'; list: List }
  | { kind: 'failed'; error: ApiError }
  | { kind: 'stale' };
