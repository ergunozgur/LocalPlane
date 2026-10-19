/**
 * What LocalPlane has recorded about one object.
 *
 * Three bounded object-scoped reads — findings, changes and runs — ordered by episode
 * opening / record creation, not by a claim about when the host last changed.
 *
 * These are object-scoped queries, not a second copy of the Operations lists: each one is
 * `?object_id=…`, keyed on the object, and shared with any other consumer on the page
 * through `useResource`. Nothing here polls.
 *
 * Where a read fails, the rail says which stream is missing instead of quietly showing a
 * shorter history. A timeline with a silent hole in it is worse than no timeline: it invites
 * the reader to conclude that nothing happened.
 */
import { useCallback, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { Plate, PlateFoot, PlateHead } from '@/components/primitives/Plate';
import { Timeline, type TickEntry } from '@/components/semantic/Timeline';
import { Empty } from '@/components/states/SurfaceState';
import { FilterChips } from '@/components/primitives/FilterChips';
import { changeResult, findingStatus, runState } from '@/domain/vocabulary';
import { formatTimestamp } from '@/domain/format';
import styles from './RecordRail.module.css';

const RECORD_LIMIT = 12;
type RecordKind = 'findings' | 'changes' | 'runs';

export function RecordRail({ objectId, objectName }: { objectId: string; objectName: string }): JSX.Element {
  // A new object must never render the previous object's data or filter, even before effects run.
  return <ObjectRecord key={objectId} objectId={objectId} objectName={objectName} />;
}

function ObjectRecord({ objectId, objectName }: { objectId: string; objectName: string }): JSX.Element {
  const [filter, setFilter] = useState('');
  const { resource: findings } = useResource(
    `findings:object:${objectId}:all:${RECORD_LIMIT}`,
    useCallback(
      (signal) => endpoints.findings({ object_id: objectId, status: 'all', limit: RECORD_LIMIT }, { signal }),
      [objectId],
    ),
  );
  const { resource: changes } = useResource(
    `changes:object:${objectId}:${RECORD_LIMIT}`,
    useCallback(
      (signal) => endpoints.changes({ object_id: objectId, limit: RECORD_LIMIT }, { signal }),
      [objectId],
    ),
  );
  const { resource: runs } = useResource(
    `runs:object:${objectId}:all:${RECORD_LIMIT}`,
    useCallback(
      (signal) => endpoints.runs({ object_id: objectId, state: 'all', limit: RECORD_LIMIT }, { signal }),
      [objectId],
    ),
  );

  const records = useMemo(() => {
    const collected: { kind: RecordKind; sort: string; entry: TickEntry }[] = [];

    if (findings.status === 'success') {
      for (const finding of findings.data.findings) {
        const state = findingStatus(finding.status);
        collected.push({
          kind: 'findings',
          sort: finding.first_seen_at,
          entry: {
            id: `finding-${finding.finding_id}`,
            kind: finding.status === 'open' ? 'drift' : 'observe',
            at: `Opened ${formatTimestamp(finding.first_seen_at) ?? '—'}`,
            what: (
              <>
                <Link to={`/operations/findings/${encodeURIComponent(finding.finding_id)}`}>{finding.summary}</Link>
              </>
            ),
            note: `Finding · ${state.label} · ${finding.finding_type.replace(/_/g, ' ')}${finding.resolution ? ` · ${finding.resolution.replace(/_/g, ' ')}` : ''}`,
          },
        });
      }
    }

    if (changes.status === 'success') {
      for (const change of changes.data.changes) {
        const outcome = changeResult(change.result);
        collected.push({
          kind: 'changes',
          sort: change.created_at,
          entry: {
            id: `change-${change.change_id}`,
            kind: 'change',
            at: `Created ${formatTimestamp(change.created_at) ?? '—'}`,
            what: (
              <>
                <Link to={`/operations/changes/${encodeURIComponent(change.change_id)}`}>{change.operation}</Link>
                {change.field ? <> · {change.field}</> : null}
              </>
            ),
            note: `Change · ${outcome.label} · host effect ${change.host_effect.replace(/_/g, ' ')}`,
          },
        });
      }
    }

    if (runs.status === 'success') {
      for (const run of runs.data.runs) {
        // Runs and Changes are distinct records. A Change may be unread or outside its
        // source's bound; never hide a Run merely because it crossed the write boundary.
        collected.push({
          kind: 'runs',
          sort: run.created_at,
          entry: {
            id: `run-${run.run_id}`,
            kind: 'observe',
            at: `Created ${formatTimestamp(run.created_at) ?? '—'}`,
            what: (
              <>
                <Link to={`/operations/runs/${encodeURIComponent(run.run_id)}`}>{run.operation}</Link>
                {run.change_id ? <> · <Link to={`/operations/changes/${encodeURIComponent(run.change_id)}`}>Change record</Link></> : null}
              </>
            ),
            note: `Run · ${runState(run.state).label} · host effect ${run.host_effect.replace(/_/g, ' ')}`,
          },
        });
      }
    }

    return collected.sort((a, b) => Date.parse(b.sort) - Date.parse(a.sort)
      || a.entry.id.localeCompare(b.entry.id));
  }, [findings, changes, runs]);

  const sources = [{ label: 'findings', resource: findings }, { label: 'changes', resource: changes }, { label: 'runs', resource: runs }];
  const unread = sources.filter(({ resource }) => resource.status === 'failed');
  const pending = sources.filter(({ resource }) => resource.status === 'loading');
  const refreshing = sources.filter(({ resource }) => resource.status === 'success' && resource.refreshing);
  const filtered = records.filter((record) => !filter || record.kind === filter);
  const entries = filtered.slice(0, RECORD_LIMIT).map((record) => record.entry);

  return (
    <Plate className={styles.rail} aria-label="Machine record">
      <PlateHead title="Machine record" level={3} asOf={objectName} />
      <div className={styles.body}>
        <div className={styles.filters}>
          <FilterChips legend="Record kind" value={filter} onChange={setFilter} options={[
            { value: '', label: 'All' },
            { value: 'findings', label: 'Findings' },
            { value: 'runs', label: 'Runs' },
            { value: 'changes', label: 'Changes' },
          ]} />
        </div>
        <p className={styles.coverage}>
          Up to {RECORD_LIMIT} per source: Runs and Changes by creation, Findings (open and resolved) by episode opening. Not exhaustive history.
        </p>
        <p className={styles.coverage} role="status">
          Showing {entries.length} of {filtered.length} loaded {filter || 'records'}.
          {filtered.length > RECORD_LIMIT ? ' Select a kind to inspect its loaded records.' : ''}
        </p>
        {entries.length > 0 ? <Timeline entries={entries} /> : null}

        {entries.length === 0 && pending.length === 0 && unread.length === 0 ? (
          <Empty
            title="No loaded records"
            explanation="No records match this view in the bounded reads. This does not establish that nothing happened to the object."
          />
        ) : null}

        {pending.length > 0 ? <p className={styles.pending}>Reading {pending.map((source) => source.label).join(' and ')}…</p> : null}
        {refreshing.length > 0 ? <p className={styles.pending}>Refreshing {refreshing.map((source) => source.label).join(' and ')}; previously read records remain visible.</p> : null}

        {unread.length > 0 ? (
          <div className={styles.unread}>
            The {unread.map((source) => source.label).join(' and ')} {unread.length === 1 ? 'stream' : 'streams'} could not be
            read, so this timeline is incomplete. What is shown is real; what is missing is
            unknown.
            {unread.map(({ label, resource }) => resource.status === 'failed' ? (
              <button className={styles.retry} type="button" key={label} onClick={resource.retry}>Retry {label}</button>
            ) : null)}
          </div>
        ) : null}
      </div>
      <PlateFoot source="Recorded evidence, not current host state. Runs and Changes may describe the same attempt; resolved Findings do not by themselves prove repair." />
    </Plate>
  );
}
