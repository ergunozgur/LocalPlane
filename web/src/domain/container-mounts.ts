import type { ContainerMount } from '@/api/types';

/** Read/write is Docker's reported mount flag, not a filesystem permission check. */
export function reportedMountAccess(mount: ContainerMount): string | null {
  if (mount.read_write === null) return null;
  return mount.read_write ? 'rw (reported)' : 'ro (reported)';
}

/** Empty Mounts records say nothing about the container's root filesystem or storage generally. */
export function emptyMountsContext(): string {
  return 'Docker reported no mount records. This does not establish that the container has no storage, root filesystem, or other filesystem context.';
}
