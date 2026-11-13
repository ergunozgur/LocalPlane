import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiError } from '@/api/client';
import { endpoints } from '@/api/endpoints';
import type { ConnectivitySnapshot, DnsRequest, DnsResult, RouteResult, TcpRequest, TcpResult } from '@/api/types';
import { tcpPolicyUnavailableReason } from './connectivityPolicy';

export type DestinationResult = RouteResult | DnsResult | TcpResult;
export interface ConnectivityState {
  objectId: string;
  snapshot: ConnectivitySnapshot | null;
  readAt: Date | null;
  reading: boolean;
  readError: string | null;
  pending: 'observe' | 'route' | 'dns' | 'tcp' | null;
  pendingTarget: string | null;
  actionError: string | null;
  result: DestinationResult | null;
  policyRefused: boolean;
  destination: string;
  dnsName: string;
  recordType: 'A' | 'AAAA';
  tcpTarget: string;
}
const initial = (objectId: string): ConnectivityState => ({
  objectId, snapshot: null, readAt: null, reading: true, readError: null, pending: null, pendingTarget: null,
  actionError: null, result: null, policyRefused: false,
  destination: '', dnsName: '', recordType: 'A', tcpTarget: '',
});

function failure(error: unknown): string {
  return error instanceof ApiError ? `${error.code ?? error.kind}: ${error.message}`
    : error instanceof Error ? error.message : 'The request did not produce a result.';
}

export function tcpTargetKey(target: { address: string; port: number }): string {
  return `${target.address.includes(':') ? `[${target.address}]` : target.address}:${target.port}`;
}

/** Advisory input check only. The backend remains the authoritative numeric validator. */
export function isNumericDestination(value: string): boolean {
  if (value !== value.trim() || /[%/\s]/.test(value)) return false;
  if (/^\d+\.\d+\.\d+\.\d+$/.test(value)) {
    return value.split('.').every((part) => /^(0|[1-9]\d{0,2})$/.test(part) && Number(part) <= 255);
  }
  if (!value.includes(':') || !/^[\da-fA-F:.]+$/.test(value)) return false;
  try { return new URL(`http://[${value}]/`).hostname.startsWith('['); }
  catch { return false; }
}

/** Owned by the interface page, not its tab body. No POST is started by an effect. */
export function useConnectivity(objectId: string) {
  const token = useMemo(() => ({ objectId, live: false, busy: false, read: 0 }), [objectId]);
  const [stored, setStored] = useState(() => initial(objectId));
  const state = stored.objectId === objectId ? stored : initial(objectId);
  const update = useCallback((change: Partial<ConnectivityState>) => {
    if (token.live) setStored((previous) => previous.objectId === objectId ? { ...previous, ...change } : previous);
  }, [objectId, token]);

  const read = useCallback(async () => {
    const version = ++token.read;
    update({ reading: true, readError: null });
    try {
      const snapshot = await endpoints.connectivity(objectId);
      if (snapshot.object_id !== objectId) throw new Error('Connectivity response belongs to a different interface.');
      if (token.live && version === token.read) update({ snapshot, readAt: new Date(), policyRefused: false });
    } catch (error) {
      if (token.live && version === token.read) update({ readError: failure(error) });
    } finally {
      if (version === token.read) update({ reading: false });
    }
  }, [objectId, token, update]);

  useEffect(() => {
    token.live = true;
    setStored(initial(objectId));
    void read();
    return () => { token.live = false; ++token.read; };
  }, [objectId, token, read]);

  const run = async (kind: NonNullable<ConnectivityState['pending']>, action: () => Promise<DestinationResult | void>, target: string | null = null) => {
    if (!token.live || token.busy) return;
    token.busy = true;
    update({ pending: kind, pendingTarget: target, actionError: null });
    try {
      const result = await action();
      if (result) {
        if ('object_id' in result && result.object_id !== objectId) throw new Error('Route response belongs to a different interface.');
        update({ result, ...('outcome' in result && ['policy_mismatch', 'denied'].includes(result.outcome) ? { policyRefused: true } : {}) });
      }
    } catch (error) {
      update({ actionError: `${failure(error)}. Delivery may have occurred; no automatic retry was made.` });
      if (error instanceof ApiError && /policy|denied/.test(error.code ?? '')) update({ policyRefused: true });
    } finally {
      token.busy = false;
      update({ pending: null, pendingTarget: null });
    }
  };

  const route = () => {
    if (!isNumericDestination(state.destination)) {
      update({ actionError: 'Enter one numeric IPv4 or IPv6 destination, without a URL, prefix or zone.' });
      return;
    }
    const destination = state.destination;
    return run('route', async () => {
      const result = await endpoints.connectivityRoute(objectId, { destination });
      const normalized = (value: string) => new URL(value.includes(':') ? `http://[${value}]/` : `http://${value}/`).hostname;
      if (!isNumericDestination(result.destination) || normalized(result.destination) !== normalized(destination)) throw new Error('Route response does not match the requested destination.');
      return result;
    }, destination);
  };
  const diagnostic = (kind: 'dns' | 'tcp') => {
    const policy = state.snapshot?.policy;
    if (state.policyRefused || policy?.status !== 'valid' || !policy.digest || !/^[0-9a-f]{64}$/.test(policy.digest)) {
      update({ actionError: 'Active policy is unavailable or changed. Re-read stored evidence before an explicit retry.' });
      return;
    }
    let body: DnsRequest | TcpRequest;
    if (kind === 'dns') {
      if (!policy.dns?.includes(state.dnsName)) return;
      body = { kind, name: state.dnsName, record_type: state.recordType, policy_digest: policy.digest };
    } else {
      const unavailable = tcpPolicyUnavailableReason(policy);
      if (unavailable) {
        update({ actionError: unavailable });
        return;
      }
      const target = policy.tcp?.find((candidate) => tcpTargetKey(candidate) === state.tcpTarget);
      if (!target) return;
      body = { kind, address: target.address, port: target.port, policy_digest: policy.digest };
    }
    return run(kind, async () => {
      const result = await endpoints.connectivityDiagnostic(objectId, body);
      if (result.kind !== body.kind || (result.kind === 'dns' && body.kind === 'dns' && (result.name !== body.name || result.record_type !== body.record_type))
        || (result.kind === 'tcp' && body.kind === 'tcp' && (result.address !== body.address || result.port !== body.port))) {
        throw new Error('Diagnostic response does not match the requested target.');
      }
      return result;
    }, body.kind === 'dns' ? `${body.name} (${body.record_type})` : tcpTargetKey(body));
  };
  return {
    state, update,
    reread: () => { if (!token.busy) void read(); },
    observe: () => run('observe', async () => {
      await endpoints.observeConnectivity(objectId);
      // The POST response is not substituted for persisted evidence.
      await read();
    }),
    route, diagnostic,
  };
}

export type ConnectivityController = ReturnType<typeof useConnectivity>;
