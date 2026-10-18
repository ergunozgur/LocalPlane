import { useEffect, useState, useSyncExternalStore } from 'react';
import { endpoints } from '@/api/endpoints';
import { ApiError } from '@/api/client';
import type { Run } from '@/api/types';
import { useRefreshVersion } from './useResource';

interface RunView {
  run: Run | null;
  reading: boolean;
  readError: Error | null;
  pending: string | null;
  actionError: Error | null;
  applyAttempted: boolean;
}
interface Entry { value: RunView; listeners: Set<() => void>; generation: number; }
const entries = new Map<string, Entry>();
const attemptKey = (id: string): string => `localplane.apply-attempt:${id}`;
function attempted(id: string): boolean {
  try { return sessionStorage.getItem(attemptKey(id)) === 'sent'; } catch { return false; }
}
function entry(id: string): Entry {
  let item = entries.get(id);
  if (!item) {
    item = { value: { run: null, reading: true, readError: null, pending: null, actionError: null, applyAttempted: attempted(id) }, listeners: new Set(), generation: 0 };
    entries.set(id, item);
  }
  return item;
}
function update(item: Entry, patch: Partial<RunView>): void {
  item.value = { ...item.value, ...patch };
  item.listeners.forEach((listener) => listener());
}
function errorOf(cause: unknown): Error {
  return cause instanceof Error ? cause : new Error('No readable response was received.');
}

// Reads may be repeated. Writes are never coupled to component effects or unmount.
async function read(id: string): Promise<void> {
  const item = entry(id);
  if (item.value.pending) return;
  const generation = ++item.generation;
  update(item, { reading: true, readError: null });
  try {
    const run = await endpoints.run(id);
    if (generation === item.generation) update(item, { run, reading: false, actionError: null });
  } catch (cause) {
    if (generation === item.generation) update(item, { reading: false, readError: errorOf(cause), ...(cause instanceof ApiError && cause.status === 404 ? { run: null } : {}) });
  }
}

export function useRun(id: string) {
  const item = entry(id);
  const refreshVersion = useRefreshVersion();
  const state = useSyncExternalStore(
    (listener) => { item.listeners.add(listener); return () => { item.listeners.delete(listener); }; },
    () => item.value,
  );
  const [readFor, setReadFor] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    void read(id).finally(() => { if (active) setReadFor(id); });
    return () => { active = false; };
  }, [id, refreshVersion]);

  const act = async (name: string, request: () => Promise<Run>): Promise<void> => {
    if (item.value.pending || item.value.reading || item.value.readError) return;
    if (name === 'Applying') {
      if (item.value.applyAttempted) return;
      // Persist before dispatch so a reload cannot silently offer a repeat of a lost apply.
      // If storage cannot retain the marker, fail before sending the host-write request.
      try { sessionStorage.setItem(attemptKey(id), 'sent'); }
      catch { update(item, { actionError: new Error('Cannot retain the apply attempt in this tab. No apply request was sent.') }); return; }
    }
    ++item.generation;
    update(item, { pending: name, actionError: null, applyAttempted: item.value.applyAttempted || name === 'Applying' });
    try { update(item, { run: await request() }); }
    catch (cause) { update(item, { actionError: errorOf(cause) }); }
    finally { update(item, { pending: null }); }
  };
  return { ...state, reading: state.reading || readFor !== id, refresh: () => { void read(id); }, act };
}

export function runRequestError(error: Error): string {
  if (!(error instanceof ApiError)) return error.message;
  if (error.status === 401) return `Authentication required: ${error.message}`;
  if (error.status === 403) return `Authorization / Origin refusal (${error.code}): ${error.message}`;
  if (error.code && /stale|preview.*mismatch/.test(error.code)) return `Preview no longer valid (${error.code}): ${error.message}`;
  if (error.kind === 'backend') return `Backend refusal or error (${error.code}): ${error.message}`;
  return `Response unavailable (${error.kind}). Delivery and outcome are unknown: ${error.message}`;
}
