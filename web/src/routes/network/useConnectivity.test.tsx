import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { endpoints } from '@/api/endpoints';
import { ApiError } from '@/api/client';
import { CONNECTIVITY, DNS, ROUTE, TCP } from '@/test/connectivity';
import type { ConnectivityPolicy, ConnectivitySnapshot, RouteResult } from '@/api/types';
import { isNumericDestination, useConnectivity } from './useConnectivity';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
beforeEach(() => {
  vi.spyOn(endpoints, 'connectivity').mockResolvedValue(CONNECTIVITY);
  vi.spyOn(endpoints, 'observeConnectivity').mockResolvedValue(CONNECTIVITY);
  vi.spyOn(endpoints, 'connectivityRoute').mockResolvedValue(ROUTE);
  vi.spyOn(endpoints, 'connectivityDiagnostic').mockResolvedValue(DNS);
});
afterEach(() => vi.restoreAllMocks());

async function setup() {
  const view = renderHook(({ id }) => useConnectivity(id), { initialProps: { id: 'obj_if' } });
  await waitFor(() => expect(view.result.current.state.snapshot).toBe(CONNECTIVITY));
  return view;
}

describe('connectivity request ownership', () => {
  it('mount/rerender/read retry never starts a POST', async () => {
    const view = await setup();
    view.rerender({ id: 'obj_if' });
    act(() => view.result.current.reread());
    await waitFor(() => expect(endpoints.connectivity).toHaveBeenCalledTimes(2));
    expect(endpoints.observeConnectivity).not.toHaveBeenCalled();
    expect(endpoints.connectivityRoute).not.toHaveBeenCalled();
    expect(endpoints.connectivityDiagnostic).not.toHaveBeenCalled();
  });

  it('observes once then re-reads stored evidence, not the POST response', async () => {
    const view = await setup();
    const stored = { ...CONNECTIVITY, policy: { ...CONNECTIVITY.policy, status: 'missing' as const, dns: [], tcp: [] } };
    vi.mocked(endpoints.observeConnectivity).mockResolvedValue({ ...CONNECTIVITY, object_id: 'not-used' });
    vi.mocked(endpoints.connectivity).mockResolvedValue(stored);
    await act(async () => { await view.result.current.observe(); });
    expect(endpoints.observeConnectivity).toHaveBeenCalledTimes(1);
    expect(endpoints.connectivity).toHaveBeenCalledTimes(2);
    expect(view.result.current.state.snapshot).toBe(stored);
  });

  it('prevents duplicate submits synchronously, retains pending through rerenders and does not cancel on unmount', async () => {
    const view = await setup();
    const pending = deferred<RouteResult>();
    vi.mocked(endpoints.connectivityRoute).mockReturnValue(pending.promise);
    act(() => view.result.current.update({ destination: '192.0.2.1' }));
    let action: Promise<void> | undefined;
    act(() => { action = view.result.current.route(); void view.result.current.route(); void view.result.current.observe(); });
    view.rerender({ id: 'obj_if' });
    expect(view.result.current.state.pending).toBe('route');
    expect(endpoints.connectivityRoute).toHaveBeenCalledTimes(1);
    expect(endpoints.connectivityRoute).toHaveBeenCalledWith('obj_if', { destination: '192.0.2.1' });
    expect(endpoints.observeConnectivity).not.toHaveBeenCalled();
    view.unmount();
    await act(async () => { pending.resolve(ROUTE); await action; });
    expect(endpoints.connectivityRoute).toHaveBeenCalledTimes(1);
  });

  it('rejects superseded stored reads', async () => {
    const old = deferred<ConnectivitySnapshot>();
    vi.mocked(endpoints.connectivity).mockReturnValueOnce(old.promise);
    const view = renderHook(() => useConnectivity('obj_if'));
    act(() => view.result.current.reread());
    await waitFor(() => expect(view.result.current.state.snapshot).toBe(CONNECTIVITY));
    await act(async () => { old.resolve({ ...CONNECTIVITY, policy: { ...CONNECTIVITY.policy, status: 'invalid' } }); await old.promise; });
    expect(view.result.current.state.snapshot).toBe(CONNECTIVITY);
  });

  it('does not display late action or stored responses from another object', async () => {
    const view = await setup();
    const old = deferred<RouteResult>();
    vi.mocked(endpoints.connectivityRoute).mockReturnValue(old.promise);
    act(() => view.result.current.update({ destination: '192.0.2.1' }));
    act(() => { void view.result.current.route(); });
    vi.mocked(endpoints.connectivity).mockResolvedValue({ ...CONNECTIVITY, object_id: 'other' });
    view.rerender({ id: 'other' });
    await waitFor(() => expect(view.result.current.state.snapshot?.object_id).toBe('other'));
    await act(async () => { old.resolve(ROUTE); await old.promise; });
    expect(view.result.current.state.result).toBeNull();
    expect(view.result.current.state.destination).toBe('');
    expect(view.result.current.state.pending).toBeNull();
  });

  it('rejects a mismatched stored object identity', async () => {
    vi.mocked(endpoints.connectivity).mockResolvedValue({ ...CONNECTIVITY, object_id: 'wrong' });
    const view = renderHook(() => useConnectivity('obj_if'));
    await waitFor(() => expect(view.result.current.state.readError).toMatch(/different interface/));
    expect(view.result.current.state.snapshot).toBeNull();
  });
});

describe('deliberate diagnostics and retention', () => {
  it.each(['disabled', 'missing_declaration', 'incomplete_declaration', 'policy_unavailable', 'future_status', undefined])('does not dispatch a direct TCP controller action for %s metadata, while DNS stays independent', async (tcp_status) => {
    const policy = { ...CONNECTIVITY.policy, tcp_status } as unknown as ConnectivityPolicy;
    vi.mocked(endpoints.connectivity).mockResolvedValue({ ...CONNECTIVITY, policy });
    const view = renderHook(() => useConnectivity('obj_if'));
    await waitFor(() => expect(view.result.current.state.reading).toBe(false));
    act(() => view.result.current.update({ dnsName: DNS.name, tcpTarget: '[2001:db8::20]:443' }));
    await act(async () => { await view.result.current.diagnostic('tcp'); });
    expect(endpoints.connectivityDiagnostic).not.toHaveBeenCalled();
    expect(view.result.current.state.actionError).toEqual(expect.any(String));
    await act(async () => { await view.result.current.diagnostic('dns'); });
    expect(endpoints.connectivityDiagnostic).toHaveBeenCalledTimes(1);
    expect(vi.mocked(endpoints.connectivityDiagnostic).mock.calls[0]?.[1]).toMatchObject({ kind: 'dns' });
    expect(view.result.current.state.tcpTarget).toBe('[2001:db8::20]:443');
  });
  it('uses independent DNS authorization and never chains a TCP or route request', async () => {
    const view = await setup();
    act(() => view.result.current.update({ dnsName: DNS.name }));
    await act(async () => { await view.result.current.diagnostic('dns'); });
    expect(endpoints.connectivityDiagnostic).toHaveBeenCalledWith('obj_if', { kind: 'dns', name: DNS.name, record_type: 'A', policy_digest: CONNECTIVITY.policy.digest });
    expect(view.result.current.state.result).toBe(DNS);
    expect(view.result.current.state.tcpTarget).toBe('');
    expect(endpoints.connectivityRoute).not.toHaveBeenCalled();
    expect(endpoints.connectivityDiagnostic).toHaveBeenCalledTimes(1);
  });

  it('submits only the selected exact IPv6 TCP pair, never a DNS answer', async () => {
    const view = await setup();
    vi.mocked(endpoints.connectivityDiagnostic).mockResolvedValue(TCP);
    act(() => view.result.current.update({ tcpTarget: '[2001:db8::20]:443' }));
    await act(async () => { await view.result.current.diagnostic('tcp'); });
    expect(endpoints.connectivityDiagnostic).toHaveBeenCalledWith('obj_if', { kind: 'tcp', address: '2001:db8::20', port: 443, policy_digest: CONNECTIVITY.policy.digest });
    act(() => view.result.current.update({ tcpTarget: '192.0.2.20:443' }));
    await act(async () => { await view.result.current.diagnostic('tcp'); });
    expect(endpoints.connectivityDiagnostic).toHaveBeenCalledTimes(1);
  });

  it('retains inputs and previous result through observation/read/action errors', async () => {
    const view = await setup();
    act(() => view.result.current.update({ destination: '192.0.2.1', dnsName: DNS.name, tcpTarget: '[2001:db8::20]:443' }));
    await act(async () => { await view.result.current.route(); });
    vi.mocked(endpoints.observeConnectivity).mockRejectedValue(new Error('provider failed'));
    await act(async () => { await view.result.current.observe(); });
    expect(view.result.current.state.result).toBe(ROUTE);
    vi.mocked(endpoints.connectivity).mockRejectedValue(new Error('stored read failed'));
    act(() => view.result.current.reread());
    await waitFor(() => expect(view.result.current.state.readError).toContain('stored read failed'));
    vi.mocked(endpoints.connectivityRoute).mockRejectedValue(new Error('route request failed'));
    await act(async () => { await view.result.current.route(); });
    expect(view.result.current.state).toMatchObject({ destination: '192.0.2.1', dnsName: DNS.name, tcpTarget: '[2001:db8::20]:443', result: ROUTE, snapshot: CONNECTIVITY });
    expect(view.result.current.state.actionError).toContain('no automatic retry');
  });

  it('policy mismatch requires an explicit stored re-read and never retries a diagnostic', async () => {
    const view = await setup();
    act(() => view.result.current.update({ dnsName: DNS.name }));
    vi.mocked(endpoints.connectivityDiagnostic).mockResolvedValue({ ...DNS, outcome: 'policy_mismatch' });
    await act(async () => { await view.result.current.diagnostic('dns'); });
    expect(view.result.current.state.policyRefused).toBe(true);
    act(() => view.result.current.update({ destination: '192.0.2.1' }));
    await act(async () => { await view.result.current.route(); });
    expect(view.result.current.state.policyRefused).toBe(true);
    await act(async () => { await view.result.current.diagnostic('dns'); });
    expect(endpoints.connectivityDiagnostic).toHaveBeenCalledTimes(1);
    vi.mocked(endpoints.connectivity).mockResolvedValue({ ...CONNECTIVITY, policy: { ...CONNECTIVITY.policy, digest: 'b'.repeat(64), dns: [] } });
    act(() => view.result.current.reread());
    await waitFor(() => expect(view.result.current.state.snapshot?.policy.digest).toBe('b'.repeat(64)));
    expect(view.result.current.state.dnsName).toBe(DNS.name);
    await act(async () => { await view.result.current.diagnostic('dns'); });
    expect(endpoints.connectivityDiagnostic).toHaveBeenCalledTimes(1);
  });

  it('shows typed policy HTTP refusal without auto retry', async () => {
    const view = await setup();
    act(() => view.result.current.update({ dnsName: DNS.name }));
    vi.mocked(endpoints.connectivityDiagnostic).mockRejectedValue(new ApiError({ kind: 'backend', code: 'diagnostic_policy_mismatch', status: 409, path: '/fixture', message: 'Restarted policy differs' }));
    await act(async () => { await view.result.current.diagnostic('dns'); });
    expect(view.result.current.state.actionError).toContain('diagnostic_policy_mismatch');
    expect(view.result.current.state.policyRefused).toBe(true);
    expect(endpoints.connectivityDiagnostic).toHaveBeenCalledTimes(1);
  });

  it('rejects mismatched route or diagnostic targets rather than replacing a result with unrelated evidence', async () => {
    const view = await setup();
    act(() => view.result.current.update({ destination: '192.0.2.1', dnsName: DNS.name }));
    vi.mocked(endpoints.connectivityRoute).mockResolvedValue({ ...ROUTE, destination: '192.0.2.2' });
    await act(async () => { await view.result.current.route(); });
    expect(view.result.current.state.result).toBeNull();
    expect(view.result.current.state.actionError).toContain('does not match');
    vi.mocked(endpoints.connectivityDiagnostic).mockResolvedValue({ ...DNS, name: 'other.example.internal.' });
    await act(async () => { await view.result.current.diagnostic('dns'); });
    expect(view.result.current.state.result).toBeNull();
    expect(view.result.current.state.actionError).toContain('does not match');
  });
});

it.each(['https://example.com', 'example.com', '192.0.2.1/24', '::1%eth0', '999.1.1.1', ' 192.0.2.1', '1:2:3'])('rejects nonnumeric/malformed route input %s', (value) => expect(isNumericDestination(value)).toBe(false));
it.each(['192.0.2.1', '2001:db8::20', '::1'])('accepts a numeric route input %s as guidance, not authorization', (value) => expect(isNumericDestination(value)).toBe(true));
