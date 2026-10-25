import { describe, expect, it } from 'vitest';
import {
  bindScope,
  bindingFact,
  emptyAttachmentsContext,
  networkModeContext,
  portTarget,
} from './container-networking';
import type { ContainerPort } from '@/api/types';

function port(overrides: Partial<ContainerPort> = {}): ContainerPort {
  return { container_port: 8080, protocol: 'tcp', host_ip: '0.0.0.0', host_port: 8080, published: true, ...overrides };
}

describe('container networking facts', () => {
  it('keeps published bindings, exposed-only targets and missing facts distinct', () => {
    expect(portTarget(port())).toBe('8080/tcp');
    expect(bindingFact(port({ published: false, host_ip: null, host_port: null }))).toBe('Exposed only');
    expect(bindingFact(port({ host_ip: null, host_port: null }))).toBe('Published; binding details not reported');
    expect(bindingFact(port({ host_ip: null, host_port: 8080 }))).toBe('Published; bind address not reported · port 8080');
    expect(portTarget(port({ container_port: null, protocol: 'udp' }))).toBe('target not reported/udp');
  });

  it('preserves multiple bindings and brackets an IPv6 host address', () => {
    expect(bindingFact(port({ host_ip: '127.0.0.1', host_port: 8080 }))).toBe('127.0.0.1:8080');
    expect(bindingFact(port({ host_ip: '2001:0db8:0000:0000:0000:ff00:0042:8329', host_port: 8080 }))).toBe('[2001:0db8:0000:0000:0000:ff00:0042:8329]:8080');
    expect(bindingFact(port({ host_ip: '2001:db8::1', host_port: 8080 }))).toBe('[2001:db8::1]:8080');
    expect(bindingFact(port({ host_ip: '::ffff:192.0.2.128', host_port: 8080 }))).toBe('[::ffff:192.0.2.128]:8080');
    expect(bindingFact(port({ host_ip: '::1', host_port: 8080 }))).toBe('[::1]:8080');
    expect(bindingFact(port({ host_ip: '::', host_port: 8080 }))).toBe('[::]:8080');
  });

  it('classifies only familiar bind-address forms and leaves unusual values unclassified', () => {
    expect(bindScope('0.0.0.0')).toBe('All IPv4 addresses');
    expect(bindScope('::')).toBe('All IPv6 addresses');
    expect(bindScope('127.0.0.1')).toBe('Loopback address');
    expect(bindScope('::1')).toBe('Loopback address');
    expect(bindScope('192.0.2.12')).toBe('Specific IPv4 address');
    expect(bindScope('2001:db8::12')).toBe('Specific IPv6 address');
    expect(bindScope('::ffff:192.0.2.128')).toBe('Specific IPv6 address');
    expect(bindScope('127.bad')).toBe('Unclassified reported address');
    expect(bindScope('1:::1')).toBe('Unclassified reported address');
    expect(bindScope('a:b')).toBe('Unclassified reported address');
    for (const address of ['::1]/path', '::1]\t/path', '::1]\n/path', '::1]\u0000/path']) {
      expect(bindScope(address)).toBe('Unclassified reported address');
      expect(bindingFact(port({ host_ip: address, host_port: 8080 }))).toBe(`${address}:8080`);
    }
    expect(bindScope('docker-host')).toBe('Unclassified reported address');
    expect(bindingFact(port({ host_ip: '1:::1', host_port: 8080 }))).toBe('1:::1:8080');
  });

  it('makes empty records contextual without claiming absent connectivity', () => {
    expect(networkModeContext('host')).toContain('host network namespace');
    expect(networkModeContext('none')).toContain('no network attachment');
    expect(networkModeContext('container')).toContain('Shares another container');
    expect(networkModeContext('container:abc')).toContain('Shares another container');
    expect(networkModeContext('unusual-mode')).toContain('Docker-reported facts');
    expect(networkModeContext(null)).toBe('Network mode was not reported.');
    expect(emptyAttachmentsContext('host')).toContain('does not establish connectivity');
    expect(emptyAttachmentsContext('none')).not.toContain('attached to none');
    expect(emptyAttachmentsContext('bridge')).toContain('does not establish no connectivity');
  });
});
