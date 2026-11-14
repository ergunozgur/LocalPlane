import { afterEach, expect, it, vi } from 'vitest';
import { endpoints } from './endpoints';
import { CONNECTIVITY, DNS, ROUTE, TCP } from '@/test/connectivity';

afterEach(() => vi.unstubAllGlobals());
it('uses the normal same-origin client and exact closed request shapes for connectivity', async () => {
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(new Response(JSON.stringify(CONNECTIVITY)))
    .mockResolvedValueOnce(new Response(JSON.stringify(CONNECTIVITY)))
    .mockResolvedValueOnce(new Response(JSON.stringify(ROUTE)))
    .mockResolvedValueOnce(new Response(JSON.stringify(DNS)))
    .mockResolvedValueOnce(new Response(JSON.stringify(TCP)));
  vi.stubGlobal('fetch', fetch);
  await endpoints.connectivity('id/one');
  await endpoints.observeConnectivity('id/one');
  await endpoints.connectivityRoute('id/one', { destination: '2001:db8::20' });
  await endpoints.connectivityDiagnostic('id/one', { kind: 'dns', name: DNS.name, record_type: 'AAAA', policy_digest: 'a'.repeat(64) });
  await endpoints.connectivityDiagnostic('id/one', { kind: 'tcp', address: TCP.address, port: TCP.port, policy_digest: 'a'.repeat(64) });
  expect(fetch.mock.calls.map(([url]) => url)).toEqual([
    '/api/v1/network/interfaces/id%2Fone/connectivity',
    '/api/v1/network/interfaces/id%2Fone/connectivity/observations/refresh',
    '/api/v1/network/interfaces/id%2Fone/connectivity/route',
    '/api/v1/network/interfaces/id%2Fone/connectivity/diagnostics',
    '/api/v1/network/interfaces/id%2Fone/connectivity/diagnostics',
  ]);
  expect(fetch.mock.calls.map(([, options]) => options?.method)).toEqual(['GET', 'POST', 'POST', 'POST', 'POST']);
  for (const [, options] of fetch.mock.calls) expect(options?.credentials).toBe('same-origin');
  expect(fetch.mock.calls[2]?.[1]?.body).toBe(JSON.stringify({ destination: '2001:db8::20' }));
  expect(fetch.mock.calls[3]?.[1]?.body).toBe(JSON.stringify({ kind: 'dns', name: DNS.name, record_type: 'AAAA', policy_digest: 'a'.repeat(64) }));
  expect(fetch.mock.calls[4]?.[1]?.body).toBe(JSON.stringify({ kind: 'tcp', address: TCP.address, port: TCP.port, policy_digest: 'a'.repeat(64) }));
});
