import { useEffect, useSyncExternalStore } from 'react';
import { ApiError } from '@/api/client';
import { useRefreshVersion } from './useResource';

export const WATCH_INTERVAL = 3000;
export const WATCH_BUDGET = 20;
interface View<T> {
  data: T | null;
  reading: boolean;
  readError: Error | null;
  pending: string | null;
  actionError: Error | null;
  intents: string[];
  watchStopped: boolean;
}
interface Entry<T> {
  value: View<T>; listeners: Set<() => void>; attached: number;
  flight: Promise<void> | null; ticket: number; accepted: number;
  timer: ReturnType<typeof setTimeout> | null; remaining: number; authFailed: boolean;
  refusedIntents: Map<string, number>;
}
const errorOf = (cause: unknown): Error => cause instanceof Error ? cause : new Error('No readable response was received.');
// Preserve the stored spelling from the authentication-refusal repair; it is now
// also used for the explicitly configured pre-dispatch recovery admission refusals.
const REFUSAL_PREFIX = 'pre-handler-auth-refused:';
const refusalMarker = (intent: string): string => `${REFUSAL_PREFIX}${intent}`;
// These codes come from auth.py's require_authentication dependency, before the
// operation handler. An unstructured/unknown 401 proves nothing about dispatch.
const preHandlerAuthRefusal = (cause: unknown): boolean => cause instanceof ApiError &&
  cause.kind === 'backend' && cause.status === 401 &&
  ['authentication_required', 'session_invalid', 'bearer_malformed', 'bearer_invalid'].includes(cause.code ?? '');
function storedIntents(key: string): string[] {
  const stored: unknown = JSON.parse(sessionStorage.getItem(key) ?? '[]');
  if (!Array.isArray(stored) || !stored.every((value: unknown) => typeof value === 'string')) throw new Error('Invalid retained intent');
  return stored;
}
function retainIntents(key: string, intents: string[]): void {
  const serialized = JSON.stringify(intents);
  sessionStorage.setItem(key, serialized);
  if (sessionStorage.getItem(key) !== serialized) throw new Error('Intent was not retained');
}

// A retained observer, not an execution engine. Effects and timers can only GET.
export function durableOperation<T>(options: {
  key: (id: string) => string;
  load: (id: string) => Promise<T>;
  compare: (next: T, held: T) => number;
  watch: (value: View<T>) => boolean;
  onActionSettled?: () => void;
  preDispatchRefusal?: (cause: unknown, id: string) => boolean;
  recordNotFound?: (cause: unknown, id: string) => boolean;
}) {
  const entries = new Map<string, Entry<T>>();
  const knownRefusal = (cause: unknown, id: string): boolean => preHandlerAuthRefusal(cause) || options.preDispatchRefusal?.(cause, id) === true;
  function entry(id: string): Entry<T> {
    let item = entries.get(id);
    if (!item) {
      let intents: string[] = [];
      try {
        intents = storedIntents(options.key(id));
      } catch { intents = ['retention-unavailable']; }
      const refusedIntents = new Map(intents.filter((intent) => intent.startsWith(REFUSAL_PREFIX)).map((marker) => [marker.slice(REFUSAL_PREFIX.length), 0]));
      item = { value: { data: null, reading: true, readError: null, pending: null, actionError: null, intents, watchStopped: false }, listeners: new Set(), attached: 0, flight: null, ticket: 0, accepted: 0, timer: null, remaining: WATCH_BUDGET, authFailed: false, refusedIntents };
      entries.set(id, item);
    }
    return item;
  }
  function update(item: Entry<T>, patch: Partial<View<T>>): void {
    item.value = { ...item.value, ...patch };
    item.listeners.forEach((listener) => listener());
  }
  function accept(item: Entry<T>, data: T, ticket: number): boolean {
    // A late action/read response must not resurrect a record after a newer
    // authoritative main-record absence. A subsequent fresh read may find it again.
    if (!item.value.data && ticket < item.accepted) return false;
    const order = item.value.data ? options.compare(data, item.value.data) : 1;
    if (order > 0 || (order === 0 && ticket >= item.accepted)) {
      item.accepted = ticket;
      update(item, { data });
      return true;
    }
    return false;
  }
  function reconcileRefusals(id: string, item: Entry<T>, ticket: number): void {
    const refused = [...item.refusedIntents].filter(([, afterTicket]) => ticket > afterTicket).map(([intent]) => intent);
    if (!refused.length) return;
    try {
      const stored = storedIntents(options.key(id));
      const proven = refused.filter((intent) => stored.includes(refusalMarker(intent)));
      if (!proven.length) return;
      const remove = new Set(proven.flatMap((intent) => [intent, refusalMarker(intent)]));
      const intents = [...new Set([...item.value.intents, ...stored])].filter((intent) => !remove.has(intent));
      // Persist the narrow release before offering a new deliberate action. Preserve
      // every unrelated historical/ambiguous marker, including legacy apply storage.
      retainIntents(options.key(id), intents);
      proven.forEach((intent) => item.refusedIntents.delete(intent));
      update(item, { intents, ...(knownRefusal(item.value.actionError, id) ? { actionError: null } : {}) });
    } catch {
      // No retention proof means no release. The prior attempted marker stays blocking.
    }
  }
  function schedule(id: string): void {
    const item = entry(id);
    if (item.timer) clearTimeout(item.timer);
    item.timer = null;
    if (!item.attached || document.hidden || item.authFailed || options.recordNotFound?.(item.value.readError, id) || !options.watch(item.value)) return;
    if (!item.remaining) { update(item, { watchStopped: true }); return; }
    item.timer = setTimeout(() => {
      item.timer = null;
      if (!item.attached || document.hidden || item.authFailed) return;
      item.remaining -= 1;
      void read(id);
    }, WATCH_INTERVAL);
  }
  function read(id: string, manual = false): Promise<void> {
    const item = entry(id);
    if (item.flight) return item.flight;
    if (manual) { item.remaining = WATCH_BUDGET; item.authFailed = false; }
    const ticket = ++item.ticket;
    update(item, { reading: true, readError: null, ...(manual ? { watchStopped: false } : {}) });
    item.flight = (async () => {
      try {
        const data = await options.load(id);
        if (accept(item, data, ticket)) reconcileRefusals(id, item, ticket);
        if (manual && !knownRefusal(item.value.actionError, id)) update(item, { actionError: null });
      } catch (cause) {
        if (cause instanceof ApiError && cause.status === 401) item.authFailed = true;
        if (options.recordNotFound?.(cause, id)) {
          if (ticket >= item.accepted) {
            item.accepted = ticket;
            // Absence invalidates the cached view, never the history of attempted writes.
            update(item, { data: null, readError: errorOf(cause) });
          }
        } else update(item, { readError: errorOf(cause) });
      } finally {
        item.flight = null;
        update(item, { reading: false });
        schedule(id);
      }
    })();
    return item.flight;
  }
  return function useDurableOperation(id: string) {
    const item = entry(id);
    const refreshVersion = useRefreshVersion();
    const state = useSyncExternalStore(
      (listener) => { item.listeners.add(listener); return () => { item.listeners.delete(listener); }; },
      () => item.value,
    );
    useEffect(() => {
      item.attached += 1;
      // Authentication return reattaches this same identity, without replaying an intent.
      item.authFailed = false;
      item.remaining = WATCH_BUDGET;
      const visibility = (): void => { schedule(id); };
      document.addEventListener('visibilitychange', visibility);
      void read(id);
      return () => {
        item.attached -= 1;
        document.removeEventListener('visibilitychange', visibility);
        if (!item.attached && item.timer) { clearTimeout(item.timer); item.timer = null; }
      };
    }, [id, item]);
    useEffect(() => {
      if (!item.authFailed) void read(id);
    }, [id, item, refreshVersion]);
    const act = async (name: string, request: () => Promise<T>, intent = name): Promise<void> => {
      if (item.value.pending || item.value.reading || item.value.readError || item.authFailed || item.value.intents.includes('retention-unavailable')) return;
      // Check storage on every click, including another mounted view of this identity.
      try {
        const stored = storedIntents(options.key(id));
        const intents = [...new Set([...item.value.intents, ...stored])];
        if (intents.includes(intent)) { update(item, { intents }); return; }
        intents.push(intent);
        retainIntents(options.key(id), intents);
        update(item, { intents });
      } catch { update(item, { actionError: new Error('Cannot retain the write attempt in this tab. No apply request was sent. No recovery or confirmation request was sent.') }); return; }
      const ticket = ++item.ticket;
      update(item, { pending: name, actionError: null });
      schedule(id);
      try { accept(item, await request(), ticket); }
      catch (cause) {
        if (cause instanceof ApiError && cause.status === 401) item.authFailed = true;
        if (knownRefusal(cause, id)) {
          try {
            const intents = [...new Set([...item.value.intents, ...storedIntents(options.key(id)), refusalMarker(intent)])];
            retainIntents(options.key(id), intents);
            // A GET already in flight when refusal arrives cannot prove reattachment.
            item.refusedIntents.set(intent, item.ticket);
            update(item, { intents });
          } catch { /* Without retained refusal proof the original intent stays blocked. */ }
        }
        update(item, { actionError: errorOf(cause) });
      } finally {
        update(item, { pending: null });
        // Reconcile the durable record and authoritative hold counts even on response loss.
        // Do not hide the action error with a successful background read.
        if (!item.authFailed) {
          options.onActionSettled?.();
          if (item.flight) await item.flight;
          if (item.attached) await read(id);
        }
        schedule(id);
      }
    };
    return { ...state, act, refresh: () => { void read(id, true); } };
  };
}
