import { ApiError } from './client';
import type { StorageCounters, StorageObservation } from './types';

function counters(value: StorageCounters | null): boolean {
  if (value === null) return true;
  if (!value || typeof value !== 'object') return false;
  if (!['total', 'used', 'free', 'available', 'reserved'].every((key) => {
    const n: unknown = value[key as keyof StorageCounters];
    return typeof n === 'string' && /^(0|[1-9][0-9]{0,38})$/.test(n);
  })) return false;
  return BigInt(value.used) + BigInt(value.free) === BigInt(value.total)
    && BigInt(value.available) + BigInt(value.reserved) === BigInt(value.free);
}

/** Reject unreadable evidence before it can render a ratio or bind a row selection. */
export function storageResponse(value: StorageObservation): StorageObservation {
  const fail = (): never => { throw new ApiError({ kind: 'malformed', path: '/storage/filesystems/observe', message: 'The backend returned unreadable filesystem evidence.' }); };
  if (!value || typeof value !== 'object' || !/^[a-f0-9]{32}$/.test(value.observation_id)
      || !Number.isFinite(Date.parse(value.observed_at))
      || !['complete', 'partial', 'busy', 'unavailable'].includes(value.status)
      || typeof value.reason !== 'string' || !Array.isArray(value.mounts) || value.mounts.length > 128
      || (value.namespace !== null && !/^mnt:\[[0-9]+\]$/.test(value.namespace))
      || (value.mounts.length > 0 && value.namespace === null)
      || (['complete', 'partial'].includes(value.status) && value.namespace === null)
      || (value.status === 'busy' && value.mounts.length > 0)
      || value.mount_limit !== 128 || value.metadata_limit_bytes !== 1048576
      || value.response_limit_bytes !== 524288 || value.work_limit_ms !== 500 || value.collection_limit_ms !== 3000) fail();
  const ids = new Set<number>();
  for (const mount of value.mounts) {
    if (!mount || !Number.isSafeInteger(mount.mount_id) || mount.mount_id <= 0 || ids.has(mount.mount_id)
        || !Number.isSafeInteger(mount.parent_id) || mount.parent_id < 0
        || !['mountpoint', 'root', 'filesystem', 'device', 'reason', 'inode_reason', 'backing_reason'].every((key) => typeof mount[key as keyof typeof mount] === 'string')
        || typeof mount.read_only !== 'boolean' || (mount.backing_name !== null && typeof mount.backing_name !== 'string')
        || !['observed', 'unsupported', 'unavailable', 'changed'].includes(mount.status)
        || !counters(mount.bytes) || !counters(mount.inodes)) fail();
    if (mount.inodes && (mount.inodes.total === '0' || [mount.inodes.total, mount.inodes.free, mount.inodes.available]
      .some((n) => n === '4294967295' || n === '18446744073709551615'))) fail();
    if (mount.status === 'observed') {
      if (!['ext4', 'xfs'].includes(mount.filesystem) || mount.bytes === null || ['busy', 'unavailable'].includes(value.status)) fail();
    } else if (mount.bytes !== null || mount.inodes !== null || mount.backing_name !== null) fail();
    if (value.status === 'complete' && (mount.status !== 'observed' || mount.inodes === null)) fail();
    ids.add(mount.mount_id);
  }
  return value;
}
