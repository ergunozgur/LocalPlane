import { expect, it } from 'vitest';
import type { ConnectivityPolicy } from '@/api/types';
import { CONNECTIVITY } from '@/test/connectivity';
import { tcpPolicyUnavailableReason } from './connectivityPolicy';

it.each([
  ['disabled', { tcp_status: 'disabled' }],
  ['missing declaration / v1', { tcp_status: 'missing_declaration', version: 1, control_plane_complete: false }],
  ['incomplete declaration', { tcp_status: 'incomplete_declaration', control_plane_complete: false }],
  ['policy unavailable', { tcp_status: 'policy_unavailable' }],
  ['invalid policy', { status: 'invalid' }], ['unsafe policy', { status: 'unsafe' }],
  ['unreadable policy', { status: 'unreadable' }], ['missing policy', { status: 'missing' }],
  ['unknown future status', { tcp_status: 'future_status' }], ['absent status', { tcp_status: undefined }],
  ['absent version', { version: undefined }], ['unknown version', { version: null }], ['future version', { version: 3 }],
  ['inconsistent v1 ready', { version: 1 }], ['inconsistent incomplete ready', { control_plane_complete: false }],
  ['unknown attestation', { control_plane_complete: null }], ['absent attestation', { control_plane_complete: undefined }],
  ['unknown count', { address_count: null }], ['absent count', { address_count: undefined }],
  ['negative count', { address_count: -1 }], ['unbounded count', { address_count: 129 }], ['fractional count', { address_count: 1.5 }],
])('fails TCP closed for %s without assuming agent agreement', (_label, patch) => {
  // Deliberately includes future/absent wire fields to test forward fail-closed behavior.
  const policy = { ...CONNECTIVITY.policy, ...patch } as unknown as ConnectivityPolicy;
  expect(tcpPolicyUnavailableReason(policy)).toEqual(expect.any(String));
});

it.each([0, 1, 128])('accepts only consistent backend-ready metadata with declared count %i as UI eligibility', (address_count) => {
  expect(tcpPolicyUnavailableReason({ ...CONNECTIVITY.policy, address_count })).toBeNull();
});

it('does not make missing policy into known-empty or ready', () => {
  expect(tcpPolicyUnavailableReason(null)).toEqual(expect.any(String));
  expect(tcpPolicyUnavailableReason(undefined)).toEqual(expect.any(String));
});
