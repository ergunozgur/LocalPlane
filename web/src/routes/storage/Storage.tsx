import { useState } from 'react';
import { endpoints } from '@/api/endpoints';
import type { StorageCounters, StorageMount, StorageObservation } from '@/api/types';
import { useResource } from '@/hooks/useResource';
import { ResourceView } from '@/components/states/ResourceView';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { PageHeader } from '../PageHeader';
import styles from './Storage.module.css';
import { availablePercent, displayPath, size } from '@/domain/storage';

function CounterDetail({ title, counters, bytes }: { title: string; counters: StorageCounters | null; bytes?: boolean }): JSX.Element {
  return <section><h3>{title}</h3>{counters ? <dl className={styles.facts}>
    {(['total', 'used', 'free', 'available', 'reserved'] as const).map((name) => <div key={name}>
      <dt>{name}</dt><dd title={counters[name]}>{bytes ? size(counters[name]) : BigInt(counters[name]).toLocaleString()}
        <span className={styles.sub}>{counters[name]} {bytes ? 'bytes' : 'inodes'}</span></dd>
    </div>)}
  </dl> : <p className="dim">Unknown — no supported counters.</p>}</section>;
}

function MountDetail({ mount }: { mount: StorageMount }): JSX.Element {
  return <Plate aria-label="Selected mount evidence"><PlateHead title={<span className={styles.path}>{displayPath(mount.mountpoint)}</span>} meta={mount.filesystem} />
    <PlateBody>
      <p>{mount.reason}</p>
      <dl className={styles.facts}>
        <div><dt>Mount / parent ID</dt><dd>{mount.mount_id} / {mount.parent_id}</dd></div>
        <div><dt>Filesystem root</dt><dd className={styles.path}>{displayPath(mount.root)}</dd></div>
        <div><dt>Access</dt><dd>{mount.read_only ? 'Read-only mount' : 'Read-write mount'} · no write authority implied</dd></div>
        <div><dt>Kernel device</dt><dd>{mount.device} (major:minor)</dd></div>
        <div><dt>Immediate backing</dt><dd>{mount.backing_name ?? 'Unknown'}<span className={styles.sub}>{mount.backing_reason}</span></dd></div>
      </dl>
      <CounterDetail title="Bytes" counters={mount.bytes} bytes />
      <p className="note">Used = total − free. Available excludes reserved space; reserved = free − available. These are independent kernel counters, not directory usage.</p>
      <CounterDetail title="Inodes" counters={mount.inodes} />
      <p className="note">{mount.inode_reason}</p>
    </PlateBody>
  </Plate>;
}

const SYSTEM_FILESYSTEMS = new Set([
  'autofs', 'binfmt_misc', 'bpf', 'cgroup', 'cgroup2', 'configfs', 'debugfs', 'devpts', 'devtmpfs',
  'efivarfs', 'fusectl', 'hugetlbfs', 'mqueue', 'nsfs', 'proc', 'pstore', 'rpc_pipefs', 'securityfs',
  'sysfs', 'tmpfs', 'tracefs',
]);

/** Presentation grouping only; it does not make a mount healthy or identify a device. */
function isSystemVirtual(mount: StorageMount): boolean {
  return SYSTEM_FILESYSTEMS.has(mount.filesystem.toLowerCase());
}

function matchesQuery(mount: StorageMount, query: string): boolean {
  const evidence = [
    displayPath(mount.mountpoint), mount.filesystem, mount.device, mount.status, mount.reason,
    mount.backing_name ?? '', mount.backing_reason,
  ].join(' ').toLowerCase();
  return evidence.includes(query.toLowerCase());
}

function byPressure(sort: 'bytes' | 'inodes') {
  return (a: StorageMount, b: StorageMount): number =>
    (availablePercent(a[sort]) ?? Infinity) - (availablePercent(b[sort]) ?? Infinity) || a.mount_id - b.mount_id;
}

function Capacity({ counters, bytes }: { counters: StorageCounters | null; bytes?: boolean }): JSX.Element {
  if (!counters) return <span className="dim">Unknown</span>;
  const available = availablePercent(counters);
  return <span title={`${counters.available} ${bytes ? 'bytes' : 'inodes'} available`}>
    {bytes ? size(counters.available) : BigInt(counters.available).toLocaleString()} available
    <span className={styles.sub}>{available === null ? 'Total is zero; ratio unknown' : `${available.toFixed(2)}% available`}</span>
    {counters.available === '0' && <strong className={styles.exhausted}>No usable {bytes ? 'bytes' : 'inodes'}</strong>}
  </span>;
}

function MountRow({ mount, selected, refreshing, onSelect }: {
  mount: StorageMount;
  selected: number | null;
  refreshing: boolean;
  onSelect: (mountId: number) => void;
}): JSX.Element {
  return <tr>
    <td className={styles.mountCell}><button className="btn sm" disabled={refreshing} aria-pressed={mount.mount_id === selected && !refreshing}
      onClick={() => onSelect(mount.mount_id)}>{displayPath(mount.mountpoint)}</button>
      <span className={styles.sub}>{mount.filesystem} · {mount.device}{mount.read_only ? ' · read-only' : ''}</span></td>
    <td>{mount.bytes ? size(mount.bytes.total) : <span className="dim">Unknown</span>}</td>
    <td>{mount.bytes ? size(mount.bytes.used) : <span className="dim">Unknown</span>}</td>
    <td><Capacity counters={mount.bytes} bytes /></td>
    <td><Capacity counters={mount.inodes} /></td>
    <td>{mount.backing_name ?? 'Unknown'}<span className={styles.sub}>{mount.device} (major:minor)</span></td>
  </tr>;
}

function Observation({ data, refreshing }: { data: StorageObservation; refreshing: boolean }): JSX.Element {
  const [selected, setSelected] = useState<number | null>(null);
  const [sort, setSort] = useState<'bytes' | 'inodes'>('bytes');
  const [query, setQuery] = useState('');
  const mounts = data.mounts.filter((mount) => matchesQuery(mount, query));
  const capacityMounts = mounts.filter((mount) => mount.status === 'observed' && (mount.bytes || mount.inodes)).sort(byPressure(sort));
  const systemMounts = mounts.filter((mount) => mount.status !== 'observed' && isSystemVirtual(mount)).sort(byPressure(sort));
  const unresolvedMounts = mounts.filter((mount) => mount.status !== 'observed' && !isSystemVirtual(mount)).sort(byPressure(sort));
  const mount = !refreshing ? data.mounts.find((m) => m.mount_id === selected) : undefined;
  const sampled = data.mounts.filter((m) => m.status === 'observed').length;
  return <>
    <div className={styles.coverage} role="status">
      <strong>{refreshing ? 'Refreshing — previous observation, not current' : `${data.status} observation`}</strong>
      <span>{sampled} / {data.mounts.length} listed mounts sampled · {data.reason}</span>
      <span>Observed {data.observed_at} · namespace {data.namespace ?? 'unknown'}</span>
    </div>
    <div className={styles.grid}>
      <Plate aria-label="Mounted filesystems"><PlateHead title="Filesystems" meta="capacity and inode pressure first" />
        <PlateBody><div className={styles.controls}>
          <label>Filter mounts <input className="inp" value={query} onChange={(e) => setQuery(e.target.value)} /></label>
          <label>Order by <select className="inp" value={sort} onChange={(e) => setSort(e.target.value as 'bytes' | 'inodes')}>
            <option value="bytes">Available byte percentage</option><option value="inodes">Available inode percentage</option>
          </select></label>
        </div></PlateBody>
        {capacityMounts.length > 0 && <div className={styles.tableWrap}><table className={`${styles.table} ${styles.mountTable}`}>
          <thead><tr><th>Mount / type</th><th>Total</th><th>Used</th><th>Available</th><th>Inode pressure</th><th>Immediate backing</th></tr></thead>
          <tbody>{capacityMounts.map((mount) => <MountRow key={mount.mount_id} mount={mount} selected={selected} refreshing={refreshing} onSelect={setSelected} />)}</tbody>
        </table></div>}
        {systemMounts.length > 0 && <details className={styles.mountGroup} open={query.length > 0}>
          <summary>System and virtual mounts <span>{systemMounts.length} rows · presentation grouping only</span></summary>
          <div className={styles.tableWrap}><table className={`${styles.table} ${styles.mountTable}`}>
            <thead><tr><th>Mount / type</th><th>Status</th><th>Reason</th><th>Immediate backing</th></tr></thead>
            <tbody>{systemMounts.map((mount) => <tr key={mount.mount_id}><td className={styles.mountCell}><button className="btn sm" disabled={refreshing} aria-pressed={mount.mount_id === selected && !refreshing} onClick={() => setSelected(mount.mount_id)}>{displayPath(mount.mountpoint)}</button><span className={styles.sub}>{mount.filesystem} · {mount.device}</span></td><td>{mount.status}</td><td>{mount.reason}</td><td>{mount.backing_name ?? 'Unknown'}<span className={styles.sub}>{mount.backing_reason}</span></td></tr>)}</tbody>
          </table></div>
        </details>}
        {unresolvedMounts.length > 0 && <section className={styles.mountGroup} aria-label="Other unsupported or unresolved mounts">
          <h3>Other unsupported or unresolved mounts <span>{unresolvedMounts.length} rows</span></h3>
          <p>These rows are separate from system and virtual mounts. Their classification does not state health.</p>
          <div className={styles.tableWrap}><table className={`${styles.table} ${styles.mountTable}`}>
            <thead><tr><th>Mount / type</th><th>Status</th><th>Reason</th><th>Immediate backing</th></tr></thead>
            <tbody>{unresolvedMounts.map((mount) => <tr key={mount.mount_id}><td className={styles.mountCell}><button className="btn sm" disabled={refreshing} aria-pressed={mount.mount_id === selected && !refreshing} onClick={() => setSelected(mount.mount_id)}>{displayPath(mount.mountpoint)}</button><span className={styles.sub}>{mount.filesystem} · {mount.device}</span></td><td>{mount.status}</td><td>{mount.reason}</td><td>{mount.backing_name ?? 'Unknown'}<span className={styles.sub}>{mount.backing_reason}</span></td></tr>)}</tbody>
          </table></div>
        </section>}
        {!mounts.length && <PlateBody>{data.mounts.length ? 'No mounts match this filter.' : 'No mount rows were returned; this does not establish host storage coverage.'}</PlateBody>}
        <PlateBody><p className="note">Capacity rows show only observed mounts with counters. Immediate backing is supplied per observed mount; it is not a whole-disk inventory or topology. Bind mounts can share counters: there is no summed host total.</p></PlateBody>
      </Plate>
      {mount ? <MountDetail mount={mount} /> : <Plate><PlateHead title="Mount evidence" /><PlateBody>
        <p>{refreshing ? 'Selection is unavailable during refresh.' : 'Select a mount to inspect its bytes, inodes and backing device.'}</p>
        <p className="note">Selection is scoped to this observation and resets on every new response. Mount IDs and paths are not durable filesystem identities.</p>
      </PlateBody></Plate>}
    </div>
    <details className={styles.bounds}><summary>Provenance and collection limits</summary>
      <p>Linux mountinfo → pinned descriptor / fdinfo mount ID and device → fstatvfs. Immediate block backing uses the sysfs major:minor join. Observation {data.observation_id}.</p>
      <p>Paths escape backslashes and control characters for readable, distinct evidence.</p>
      <p>At most {data.mount_limit} mount rows; {data.metadata_limit_bytes} metadata bytes per mount table; {data.response_limit_bytes} response bytes; {data.work_limit_ms} ms per mount work; {data.collection_limit_ms} ms total collection wait.</p>
      <p>Mount paths are limited to 4096 characters and 64 components. Descriptor metadata is limited to 4096 bytes; the sysfs device-number read to 64 bytes.</p>
      <p>Only ext4/xfs and supported ancestors are sampled. Remote, FUSE, autofs and other filesystems remain unsupported. Timed-out work retains the single collector slot until actual exit. Namespace changes can invalidate evidence; this is not a namespace freeze or guaranteed kernel-I/O cancellation.</p>
      <p>No contents, directory scans, cleanup, SMART, backups, health, causes or growth are measured. These counters are a point-in-time observation in the agent namespace, not necessarily the host namespace.</p>
    </details>
  </>;
}

export function Storage(): JSX.Element {
  const { resource, refresh } = useResource('storage-filesystems', (signal) => endpoints.filesystems({ signal }));
  return <>
    <PageHeader title="Storage" annotation="Which mounted filesystem is short of usable bytes or inodes?">
      <button className="btn" onClick={refresh} disabled={resource.status === 'loading' || (resource.status === 'success' && resource.refreshing)}>Refresh filesystems</button>
    </PageHeader>
    <ResourceView resource={resource} what="filesystem observation" loadingLabel="Reading mounted filesystem evidence…">
      {(data, meta) => <Observation key={data.observation_id} data={data} refreshing={meta.refreshing} />}
    </ResourceView>
  </>;
}
