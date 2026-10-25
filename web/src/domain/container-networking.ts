import type { ContainerPort } from '@/api/types';

/** Format only Docker-reported binding facts; this is not a reachability assessment. */
export function portTarget(port: ContainerPort): string {
  return port.container_port === null ? `target not reported/${port.protocol}` : `${port.container_port}/${port.protocol}`;
}

export function bindingFact(port: ContainerPort): string {
  if (!port.published) return 'Exposed only';
  if (port.host_ip === null && port.host_port === null) return 'Published; binding details not reported';
  if (port.host_ip === null) return `Published; bind address not reported · port ${port.host_port}`;
  if (port.host_port === null) return `Published; ${formatHostAddress(port.host_ip)} · host port not reported`;
  return `${formatHostAddress(port.host_ip)}:${port.host_port}`;
}

export function bindScope(hostIp: string | null): string {
  if (hostIp === null) return 'Bind address not reported';
  if (hostIp === '0.0.0.0') return 'All IPv4 addresses';
  if (hostIp === '::') return 'All IPv6 addresses';
  if (hostIp === '::1' || (isIpv4(hostIp) && hostIp.startsWith('127.'))) return 'Loopback address';
  if (isIpv4(hostIp)) return 'Specific IPv4 address';
  if (isIpv6Form(hostIp)) return 'Specific IPv6 address';
  return 'Unclassified reported address';
}

export function networkModeContext(mode: string | null): string {
  if (mode === 'host') return 'Shares the host network namespace; Docker ignores published-port mappings.';
  if (mode === 'none') return 'Docker reports no network attachment for this mode.';
  if (mode === 'container' || mode?.startsWith('container:')) return 'Shares another container network namespace; this record can have no attachment or port rows.';
  return mode ? 'Attachment and port rows below are Docker-reported facts for this mode.' : 'Network mode was not reported.';
}

export function emptyAttachmentsContext(mode: string | null): string {
  if (mode === 'host') return 'Docker reported no attachment rows for host networking. This does not establish connectivity or listeners.';
  if (mode === 'none') return 'Docker reported no attachment rows for this mode. This does not establish all connectivity facts.';
  if (mode === 'container' || mode?.startsWith('container:')) return 'Docker reported no attachment rows for this shared network namespace. This does not establish connectivity.';
  return 'Docker reported no attachment rows. This does not establish no connectivity.';
}

function formatHostAddress(address: string): string {
  return isIpv6Form(address) ? `[${address}]` : address;
}

function isIpv4(address: string): boolean {
  const parts = address.split('.');
  return parts.length === 4 && parts.every((part) => {
    if (part === '' || ![...part].every((character) => character >= '0' && character <= '9')) return false;
    const value = Number(part);
    return value >= 0 && value <= 255 && String(value) === part;
  });
}

function isIpv6Form(address: string): boolean {
  if (!address.includes(':') || !/^[0-9a-fA-F:.]+$/.test(address)) return false;
  try {
    new URL(`http://[${address}]/`);
    return true;
  } catch {
    return false;
  }
}
