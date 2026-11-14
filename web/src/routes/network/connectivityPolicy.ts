import type { ConnectivityPolicy } from '@/api/types';

/** UI eligibility only: neither agent agreement nor current live guards are proved here. */
export function tcpPolicyUnavailableReason(policy: ConnectivityPolicy | null | undefined): string | null {
  if (!policy || policy.status !== 'valid') return 'TCP policy is unavailable. DNS authorization is assessed independently.';
  switch (policy.tcp_status) {
    case 'disabled': return 'TCP diagnostics are disabled by the loaded policy.';
    case 'missing_declaration': return 'TCP is unavailable without a complete v2 control-plane declaration. V1 DNS authorization remains independent.';
    case 'incomplete_declaration': return 'TCP is unavailable because the administrator control-plane declaration is incomplete.';
    case 'policy_unavailable': return 'TCP policy eligibility is unavailable; no TCP attempt can be made.';
    case 'ready': break;
    default: return 'TCP policy status is unknown or unsupported; no TCP attempt can be made.';
  }
  if (policy.version !== 2 || policy.control_plane_complete !== true) {
    return 'TCP declaration metadata is unknown, unsupported or inconsistent; no TCP attempt can be made.';
  }
  if (typeof policy.address_count !== 'number' || !Number.isSafeInteger(policy.address_count) || policy.address_count < 0 || policy.address_count > 128) {
    return 'Declared control-plane address count is unknown or invalid; no TCP attempt can be made.';
  }
  return null;
}
