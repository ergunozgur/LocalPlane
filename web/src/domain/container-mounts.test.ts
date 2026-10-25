import { describe, expect, it } from 'vitest';
import { emptyMountsContext, reportedMountAccess } from './container-mounts';
import type { ContainerMount } from '@/api/types';

function mount(overrides: Partial<ContainerMount> = {}): ContainerMount {
  return {
    type: 'volume', name: 'named-data', source: '/var/lib/docker/volumes/named-data/_data',
    destination: '/data', driver: 'local', mode: 'z', read_write: true, propagation: '', ...overrides,
  };
}

describe('container mount facts', () => {
  it('keeps Docker-reported read-write, read-only, and missing access distinct', () => {
    expect(reportedMountAccess(mount({ read_write: true }))).toBe('rw (reported)');
    expect(reportedMountAccess(mount({ read_write: false }))).toBe('ro (reported)');
    expect(reportedMountAccess(mount({ read_write: null }))).toBeNull();
  });

  it('does not turn an empty Mounts array into a filesystem absence claim', () => {
    expect(emptyMountsContext()).toContain('no mount records');
    expect(emptyMountsContext()).toContain('does not establish');
  });
});
