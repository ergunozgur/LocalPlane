/**
 * Request state, as a discriminated union.
 *
 * This is deliberately small and deliberately hand-written. What a read-only console needs
 * from a data layer is cancellation, a total status union and a manual refresh — about a
 * hundred lines. A query library would additionally impose its own `isLoading` /
 * `isFetching` / `isError` vocabulary, which would then have to be mapped onto LocalPlane's
 * state vocabulary on every surface. Keeping one vocabulary is the point.
 *
 * Two properties matter more than the size:
 *
 *  - **A refresh never blanks the screen.** Previous data stays visible with `refreshing`
 *    set, so an operator watching a value does not lose it to a spinner.
 *  - **Transport failure and domain state stay separate.** This hook reports only whether a
 *    body arrived. What the body *says* — `unknown`, `stale`, `partial` — is the domain
 *    layer's business and is never folded into `status`.
 *
 * ## The temporal seam
 *
 * LocalPlane will one day want to be read at a past moment — a `HISTORICAL@timestamp` replay
 * beside today's `LIVE`. **None of that is built here, and no part of it should be.** What is
 * preserved is the one property that makes it buildable later without touching every screen:
 *
 *   **`fetchedAt` below is the only wall clock in the rendering path.**
 *
 * It is produced once, when a body arrives, and travels down as an explicit value —
 * `ScopeBar observedAt`, `PlateHead asOf`, `ObjectWorkspace observedAt`. No component asks
 * what time it is, and no component decides for itself whether what it holds is current;
 * both are told. `formatRelative` takes `now` as a parameter for the same reason.
 *
 * A replay mode is therefore a change to where the data and this one timestamp come from,
 * not to what any surface renders. Nothing here knows that modes exist, and nothing should
 * acquire that knowledge until there is a contract behind it: a time control that cannot
 * actually read the past is precisely the fake capability this product exists not to ship.
 */
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { ApiError } from '@/api/client';

/**
 * A console-wide re-read.
 *
 * Every surface holds its own resource, so "refresh everything" needs one signal they all
 * observe. A module-level counter is the whole mechanism: `refreshAll` bumps it, every
 * mounted resource re-runs, and nothing needs to know what else is on screen.
 */
let globalRefreshCount = 0;
const globalRefreshListeners = new Set<() => void>();

export function refreshAll(): void {
  globalRefreshCount += 1;
  for (const listener of globalRefreshListeners) listener();
}

function subscribeToGlobalRefresh(listener: () => void): () => void {
  globalRefreshListeners.add(listener);
  return () => globalRefreshListeners.delete(listener);
}

const readGlobalRefresh = (): number => globalRefreshCount;

/**
 * In-flight request sharing.
 *
 * Several components legitimately want the same resource at once — the Overview's machine
 * panel, its Network widget and its attention rail all read the interface list. Without
 * sharing, one page load issued four identical requests for it and four more for the
 * container list. Every one of those is a round trip the backend serves and a body the
 * browser parses, for one answer.
 *
 * So a request is keyed, and a second subscriber for a key that is already in flight waits
 * on the first rather than starting another. The underlying request is aborted only when
 * every subscriber has gone, which keeps the cancellation-on-unmount behaviour without one
 * component's unmount cancelling another's read.
 */
interface Flight {
  promise: Promise<unknown>;
  controller: AbortController;
  subscribers: number;
}

const inFlight = new Map<string, Flight>();

function share<T>(key: string, run: (signal: AbortSignal) => Promise<T>): Flight {
  const existing = inFlight.get(key);
  if (existing) {
    existing.subscribers += 1;
    return existing;
  }
  const controller = new AbortController();
  const flight: Flight = { promise: Promise.resolve(), controller, subscribers: 1 };
  flight.promise = run(controller.signal).finally(() => {
    // Cleared on settle so the next mount fetches again: this shares a request, it is not a
    // cache, and a stale body served from memory would be a claim about the host that nobody
    // made.
    if (inFlight.get(key) === flight) inFlight.delete(key);
  });
  inFlight.set(key, flight);
  return flight;
}

function release(key: string, flight: Flight): void {
  flight.subscribers -= 1;
  if (flight.subscribers > 0) return;
  if (inFlight.get(key) === flight) inFlight.delete(key);
  flight.controller.abort();
}

export type Resource<T> =
  | { status: 'loading' }
  | { status: 'success'; data: T; fetchedAt: Date; refreshing: boolean }
  | { status: 'failed'; error: ApiError; retry: () => void };

export interface ResourceHandle<T> {
  resource: Resource<T>;
  refresh: () => void;
}

function toApiError(cause: unknown, path: string): ApiError {
  if (cause instanceof ApiError) return cause;
  return new ApiError({
    kind: 'malformed',
    message: cause instanceof Error ? cause.message : 'unexpected failure',
    path,
  });
}

/**
 * Fetch once per `key`, and again on demand.
 *
 * `key` is the identity of the request. Change it and the in-flight request is abandoned and
 * a new one starts; keep it stable across renders and nothing refetches. `fetcher` is read
 * from a ref, so an inline arrow function does not cause a loop.
 */
export function useResource<T>(
  key: string,
  fetcher: (signal: AbortSignal) => Promise<T>,
): ResourceHandle<T> {
  const [resource, setResource] = useState<Resource<T>>({ status: 'loading' });
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  // Held so a refresh can keep the previous body on screen rather than dropping to loading.
  const dataRef = useRef<{ key: string; data: T; fetchedAt: Date } | null>(null);
  const [nonce, setNonce] = useState(0);
  const globalNonce = useSyncExternalStore(
    subscribeToGlobalRefresh,
    readGlobalRefresh,
    readGlobalRefresh,
  );

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    let live = true;

    const held = dataRef.current;
    if (held && held.key === key) {
      setResource({
        status: 'success',
        data: held.data,
        fetchedAt: held.fetchedAt,
        refreshing: true,
      });
    } else {
      dataRef.current = null;
      setResource({ status: 'loading' });
    }

    // The nonce is part of the sharing key so a deliberate refresh always issues a new
    // request rather than joining one that began before the operator asked.
    const flightKey = `${key}#${nonce}#${globalNonce}`;
    const flight = share(flightKey, (signal) => fetcherRef.current(signal));

    void (async () => {
      try {
        const data = (await flight.promise) as T;
        if (!live) return;
        const fetchedAt = new Date();
        dataRef.current = { key, data, fetchedAt };
        setResource({ status: 'success', data, fetchedAt, refreshing: false });
      } catch (cause) {
        if (!live) return;
        const error = toApiError(cause, key);
        // A cancellation is this component's own doing, not something an operator did or
        // needs to see. Leave the last state alone.
        if (error.kind === 'cancelled' || flight.controller.signal.aborted) return;
        setResource({ status: 'failed', error, retry: refresh });
      }
    })();

    return () => {
      live = false;
      release(flightKey, flight);
    };
  }, [key, nonce, globalNonce, refresh]);

  return { resource, refresh };
}

/**
 * Combine two resources into one, so a surface that needs both can render one set of states.
 *
 * Failure wins over loading: if either request failed, the operator is told which, rather
 * than being left on a spinner that will never resolve.
 */
export function combine<A, B>(a: Resource<A>, b: Resource<B>): Resource<[A, B]> {
  // A failed resource carries no data, so it is already a `Resource` of any type.
  if (a.status === 'failed') return a;
  if (b.status === 'failed') return b;
  if (a.status === 'loading' || b.status === 'loading') return { status: 'loading' };
  return {
    status: 'success',
    data: [a.data, b.data],
    fetchedAt: a.fetchedAt < b.fetchedAt ? a.fetchedAt : b.fetchedAt,
    refreshing: a.refreshing || b.refreshing,
  };
}

/**
 * A resource whose failure is an acceptable answer.
 *
 * Some facts are genuinely optional — an interface that is merely observed has no intent, and
 * the backend says so with a 404. That is not a broken screen; it is the absence of a thing.
 * Anything other than a 404 stays a failure.
 */
export function optional<T>(resource: Resource<T>): Resource<T | null> {
  if (resource.status === 'failed' && resource.error.notFound) {
    return { status: 'success', data: null, fetchedAt: new Date(), refreshing: false };
  }
  return resource;
}
