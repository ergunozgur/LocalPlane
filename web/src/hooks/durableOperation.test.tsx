import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ApiError } from '@/api/client';
import { durableOperation, WATCH_BUDGET, WATCH_INTERVAL } from './durableOperation';

interface Record { version: number; active: boolean; }
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
let sequence = 0;
function controller(load = vi.fn<() => Promise<Record>>().mockResolvedValue({ version: 1, active: false })) {
  const key = `durable-test-${++sequence}`;
  return { key, load, useController: durableOperation<Record>({ key: () => key, load,
    compare: (next, held) => next.version - held.version,
    watch: (view) => !!view.pending || view.data?.active === true || (!!view.actionError && view.intents.length > 0),
  }) };
}
const flush = async () => { await act(async () => { await Promise.resolve(); }); };
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); sessionStorage.clear(); });

it('allows GET while action is pending, ignores an older action response and retains inputs/data on read loss', async () => {
  const c = controller(); const { result } = renderHook(() => c.useController('same'));
  await flush(); const write = deferred<Record>();
  act(() => { void result.current.act('Retry', () => write.promise, 'retry:exact'); });
  c.load.mockResolvedValue({ version: 3, active: false });
  act(() => result.current.refresh()); await flush();
  expect(result.current.data?.version).toBe(3);
  expect(result.current.pending).toBe('Retry');
  await act(async () => write.resolve({ version: 2, active: false }));
  expect(result.current.data?.version).toBe(3);
  c.load.mockRejectedValue(new Error('read lost'));
  act(() => result.current.refresh()); await flush();
  expect(result.current.data?.version).toBe(3);
  expect(result.current.readError?.message).toBe('read lost');
});

it('does not let an older read overwrite a newer action response', async () => {
  const c = controller(); const { result } = renderHook(() => c.useController('same'));
  await flush(); const write = deferred<Record>(); const oldRead = deferred<Record>();
  act(() => { void result.current.act('Apply', () => write.promise); });
  c.load.mockReturnValueOnce(oldRead.promise).mockResolvedValue({ version: 2, active: false });
  act(() => result.current.refresh());
  await act(async () => write.resolve({ version: 2, active: false }));
  await act(async () => oldRead.resolve({ version: 1, active: true }));
  expect(result.current.data?.version).toBe(2);
});

it('coalesces read refreshes and bounds non-overlapping polling; manual refresh remains after budget', async () => {
  vi.useFakeTimers();
  const c = controller(vi.fn().mockResolvedValue({ version: 1, active: true }));
  const { result } = renderHook(() => c.useController('same')); await flush();
  const slow = deferred<Record>(); c.load.mockReturnValueOnce(slow.promise);
  await act(async () => vi.advanceTimersByTimeAsync(WATCH_INTERVAL));
  act(() => { result.current.refresh(); result.current.refresh(); });
  await act(async () => vi.advanceTimersByTimeAsync(WATCH_INTERVAL * 4));
  expect(c.load).toHaveBeenCalledTimes(2);
  await act(async () => slow.resolve({ version: 1, active: true }));
  await act(async () => vi.advanceTimersByTimeAsync(WATCH_INTERVAL * WATCH_BUDGET * 2));
  expect(c.load).toHaveBeenCalledTimes(WATCH_BUDGET + 1);
  expect(result.current.watchStopped).toBe(true);
  act(() => result.current.refresh()); await flush();
  expect(c.load).toHaveBeenCalledTimes(WATCH_BUDGET + 2);
});

it('stops watching on hidden, unmount, terminal state and authentication failure', async () => {
  vi.useFakeTimers(); let hidden = false;
  vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden);
  const c = controller(vi.fn().mockResolvedValue({ version: 1, active: true }));
  const view = renderHook(() => c.useController('same')); await flush();
  hidden = true; act(() => { document.dispatchEvent(new Event('visibilitychange')); });
  await act(async () => vi.advanceTimersByTimeAsync(WATCH_INTERVAL * 2));
  expect(c.load).toHaveBeenCalledTimes(1);
  hidden = false; act(() => { document.dispatchEvent(new Event('visibilitychange')); });
  c.load.mockResolvedValueOnce({ version: 2, active: false });
  await act(async () => vi.advanceTimersByTimeAsync(WATCH_INTERVAL * 2));
  expect(c.load).toHaveBeenCalledTimes(2);
  c.load.mockResolvedValueOnce({ version: 3, active: true });
  act(() => view.result.current.refresh()); await flush();
  c.load.mockRejectedValue(new ApiError({ kind: 'backend', status: 401, path: '/same', message: 'Sign in' }));
  await act(async () => vi.advanceTimersByTimeAsync(WATCH_INTERVAL * 2));
  expect(c.load).toHaveBeenCalledTimes(4);
  view.unmount(); await act(async () => vi.advanceTimersByTimeAsync(WATCH_INTERVAL * 2));
  expect(c.load).toHaveBeenCalledTimes(4);
});

it('retains write intent before dispatch, prevents duplicates across detach/remount and fresh stores', async () => {
  const c = controller(); const view = renderHook(() => c.useController('same')); await flush();
  const request = vi.fn(async () => {
    expect(sessionStorage.getItem(c.key)).toContain('confirm');
    throw new Error('lost grant response');
  });
  await act(async () => { await view.result.current.act('Grant', request, 'confirm'); });
  act(() => { void view.result.current.act('Grant', request, 'confirm'); });
  expect(request).toHaveBeenCalledTimes(1);
  expect(view.result.current.actionError?.message).toBe('lost grant response');
  view.unmount();
  const useReloaded = durableOperation<Record>({ key: () => c.key, load: c.load, compare: (a, b) => a.version - b.version, watch: () => false });
  const reloaded = renderHook(() => useReloaded('same')); await flush();
  await act(async () => { await reloaded.result.current.act('Grant', request, 'confirm'); });
  expect(request).toHaveBeenCalledTimes(1);
});

it('fails closed before dispatch if retention is unreadable or unwritable', async () => {
  const c = controller(); const { result } = renderHook(() => c.useController('same')); await flush();
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied'); });
  const request = vi.fn();
  await act(async () => { await result.current.act('Apply', request); });
  expect(request).not.toHaveBeenCalled();
  expect(result.current.actionError?.message).toContain('No apply request was sent');
});

it('detaches an active observer without cancelling work or leaving a polling timer', async () => {
  vi.useFakeTimers();
  const c = controller(vi.fn().mockResolvedValue({ version: 1, active: true }));
  const view = renderHook(() => c.useController('same')); await flush();
  view.unmount();
  await act(async () => vi.advanceTimersByTimeAsync(WATCH_INTERVAL * 5));
  expect(c.load).toHaveBeenCalledTimes(1);
  const reattached = renderHook(() => c.useController('same')); await flush();
  expect(reattached.result.current.data?.version).toBe(1);
  expect(c.load).toHaveBeenCalledTimes(2);
});

it.each(['authentication_required', 'session_invalid', 'bearer_malformed', 'bearer_invalid'])('releases only the provably pre-handler %s intent after a fresh successful read', async (code) => {
  const c = controller(); sessionStorage.setItem(c.key, JSON.stringify(['historic-ambiguous']));
  sessionStorage.setItem('localplane.apply-attempt:same', 'sent');
  const view = renderHook(() => c.useController('same')); await flush();
  const request = vi.fn().mockRejectedValueOnce(new ApiError({ kind: 'backend', status: 401, code, path: '/same', message: 'Sign in' })).mockResolvedValue({ version: 2, active: false });
  await act(async () => { await view.result.current.act('Apply', request); });
  expect(view.result.current.intents).toContain('Apply');
  expect(view.result.current.actionError?.message).toBe('Sign in');
  expect(request).toHaveBeenCalledTimes(1);
  view.unmount();
  c.load.mockRejectedValueOnce(new Error('reattachment read failed'));
  const reattached = renderHook(() => c.useController('same')); await flush();
  expect(reattached.result.current.intents).toContain('Apply');
  expect(reattached.result.current.actionError?.message).toBe('Sign in');
  act(() => reattached.result.current.refresh()); await flush();
  expect(reattached.result.current.intents).toEqual(['historic-ambiguous']);
  expect(reattached.result.current.readError).toBeNull();
  expect(reattached.result.current.actionError).toBeNull();
  expect(sessionStorage.getItem('localplane.apply-attempt:same')).toBe('sent');
  expect(request).toHaveBeenCalledTimes(1);
  await act(async () => { await reattached.result.current.act('Apply', request); });
  expect(request).toHaveBeenCalledTimes(2);
});

it.each([
  { kind: 'backend' as const, code: 'unknown_auth_code', status: 401 },
  { kind: 'backend' as const, code: 'http_401', status: 401 },
  { kind: 'unreachable' as const, code: 'authentication_required', status: 401 },
  { kind: 'malformed' as const, code: 'session_invalid', status: 401 },
  { kind: 'backend' as const, code: 'session_invalid', status: 503 },
])('preserves ambiguous intent for $kind / $code / $status despite a successful reread', async (failure) => {
  const c = controller(); const view = renderHook(() => c.useController('same')); await flush();
  const request = vi.fn().mockRejectedValue(new ApiError({ ...failure, path: '/same', message: 'Unknown delivery' }));
  await act(async () => { await view.result.current.act('Grant', request); });
  view.unmount(); const reattached = renderHook(() => c.useController('same')); await flush();
  act(() => reattached.result.current.refresh()); await flush();
  expect(reattached.result.current.intents).toContain('Grant');
  await act(async () => { await reattached.result.current.act('Grant', request); });
  expect(request).toHaveBeenCalledTimes(1);
});

it('a GET started before authentication refusal cannot release that intent', async () => {
  const c = controller(); const view = renderHook(() => c.useController('same')); await flush();
  const write = deferred<Record>(); const read = deferred<Record>();
  act(() => { void view.result.current.act('Grant', () => write.promise); });
  c.load.mockReturnValueOnce(read.promise);
  act(() => view.result.current.refresh());
  await act(async () => write.reject(new ApiError({ kind: 'backend', status: 401, code: 'session_invalid', path: '/same', message: 'Sign in' })));
  await act(async () => read.resolve({ version: 1, active: false }));
  expect(view.result.current.intents).toContain('Grant');
  expect(view.result.current.actionError?.message).toBe('Sign in');
  act(() => view.result.current.refresh()); await flush();
  expect(view.result.current.intents).not.toContain('Grant');
  expect(view.result.current.actionError).toBeNull();
});

it('retains pre-handler refusal proof across a fresh controller store without erasing ambiguous history', async () => {
  const c = controller(); const view = renderHook(() => c.useController('same')); await flush();
  await act(async () => { await view.result.current.act('Grant', () => Promise.reject(new ApiError({ kind: 'backend', status: 401, code: 'session_invalid', path: '/same', message: 'Sign in' }))); });
  view.unmount();
  const useReloaded = durableOperation<Record>({ key: () => c.key, load: c.load, compare: (a, b) => a.version - b.version, watch: () => false });
  const reloaded = renderHook(() => useReloaded('same')); await flush();
  expect(reloaded.result.current.intents).not.toContain('Grant');
  expect(sessionStorage.getItem(c.key)).toBe('[]');
});

it('a late action cannot resurrect an authoritatively missing record or erase write intent', async () => {
  const missing = new ApiError({ kind: 'backend', status: 404, code: 'run_not_found', path: '/runs/same', message: 'No such Run' });
  const load = vi.fn<() => Promise<Record>>().mockResolvedValue({ version: 1, active: false });
  const key = `missing-order-${++sequence}`;
  const useController = durableOperation<Record>({ key: () => key, load, compare: (a, b) => a.version - b.version, watch: () => true, recordNotFound: (cause) => cause === missing });
  const view = renderHook(() => useController('same')); await flush();
  const write = deferred<Record>();
  act(() => { void view.result.current.act('Apply', () => write.promise); });
  const nextRead = deferred<Record>();
  load.mockRejectedValueOnce(missing).mockReturnValueOnce(nextRead.promise);
  act(() => view.result.current.refresh()); await flush();
  expect(view.result.current.data).toBeNull();
  await act(async () => write.resolve({ version: 2, active: false }));
  expect(view.result.current.data).toBeNull();
  expect(view.result.current.intents).toContain('Apply');
  expect(sessionStorage.getItem(key)).toContain('Apply');
  await act(async () => nextRead.reject(missing));
});
