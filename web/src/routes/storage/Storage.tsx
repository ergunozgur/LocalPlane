import { useState } from 'react';
import { endpoints } from '@/api/endpoints';
import type { StorageCounters, StorageMount, StorageObservation } from '@/api/types';
import { useResource } from '@/hooks/useResource';
import { ResourceView } from '@/components/states/ResourceView';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { PageHeader } from '../PageHeader';
import styles from './Storage.module.css';
import { availablePercent, displayPath, size } from '@/domain/storage';

function Availability({ value, bytes }: { value: StorageCounters | null; bytes?: boolean }): JSX.Element {
  if (!value) return <span className="dim">Unknown</span>;
  const percent = availablePercent(value);
  return <span title={`${value.available} ${bytes ? 'bytes' : 'inodes'} available`}>
    {bytes ? size(value.available) : BigInt(value.available).toLocaleString()}
    <span className={styles.sub}>{percent === null ? 'Total is zero; ratio unknown' : `${percent.toFixed(2)}% available`}</span>
    {value.available === '0' && <strong className={styles.exhausted}>No usable {bytes ? 'bytes' : 'inodes'}</strong>}
  </span>;
}

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

function Observation({ data, refreshing }: { data: StorageObservation; refreshing: boolean }): JSX.Element {
  const [selected, setSelected] = useState<number | null>(null);
  const [sort, setSort] = useState<'bytes' | 'inodes'>('bytes');
  const [query, setQuery] = useState('');
  const mounts = data.mounts.filter((m) => `${displayPath(m.mountpoint)} ${m.filesystem} ${m.device}`.toLowerCase().includes(query.toLowerCase()))
    .sort((a, b) => (availablePercent(a[sort]) ?? Infinity) - (availablePercent(b[sort]) ?? Infinity) || a.mount_id - b.mount_id);
  const mount = !refreshing ? data.mounts.find((m) => m.mount_id === selected) : undefined;
  const sampled = data.mounts.filter((m) => m.status === 'observed').length;
  return <>
    <div className={styles.coverage} role="status">
      <strong>{refreshing ? 'Refreshing — previous observation, not current' : `${data.status} observation`}</strong>
      <span>{sampled} / {data.mounts.length} listed mounts sampled · {data.reason}</span>
      <span>Observed {data.observed_at} · namespace {data.namespace ?? 'unknown'}</span>
    </div>
    <div className={styles.grid}>
      <Plate aria-label="Mounted filesystems"><PlateHead title="Filesystems" meta="least available first" />
        <PlateBody><div className={styles.controls}>
          <label>Filter mounts <input className="inp" value={query} onChange={(e) => setQuery(e.target.value)} /></label>
          <label>Order by <select className="inp" value={sort} onChange={(e) => setSort(e.target.value as 'bytes' | 'inodes')}>
            <option value="bytes">Available byte percentage</option><option value="inodes">Available inode percentage</option>
          </select></label>
        </div></PlateBody>
        <div className={styles.tableWrap}><table className={styles.table}>
          <thead><tr><th>Mount / type</th><th>Usable bytes</th><th>Usable inodes</th><th>Evidence</th></tr></thead>
          <tbody>{mounts.map((m) => <tr key={m.mount_id}>
            <td><button className="btn sm" disabled={refreshing} aria-pressed={m.mount_id === selected && !refreshing}
              onClick={() => setSelected(m.mount_id)}>{displayPath(m.mountpoint)}</button><span className={styles.sub}>{m.filesystem} · {m.device}{m.read_only ? ' · read-only' : ''}</span></td>
            <td><Availability value={m.bytes} bytes /></td><td><Availability value={m.inodes} /></td><td>{m.status}</td>
          </tr>)}</tbody>
        </table></div>
        {!mounts.length && <PlateBody>{data.mounts.length ? 'No mounts match this filter.' : 'No mount rows were returned; this does not establish host storage coverage.'}</PlateBody>}
        <PlateBody><p className="note">Select a mount for exact counters and backing evidence. Bind mounts can share counters: there is no summed host total.</p></PlateBody>
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
