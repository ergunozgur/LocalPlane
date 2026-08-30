/**
 * What LocalPlane has recorded about one object.
 *
 * Its footer sentence is the whole idea: *every entry here was
 * written by an observation, not by a person*. Three object-scoped reads — findings, changes
 * and runs — merged onto one timeline in the order they happened.
 *
 * These are object-scoped queries, not a second copy of the Operations lists: each one is
 * `?object_id=…`, keyed on the object, and shared with any other consumer on the page
 * through `useResource`. Nothing here polls.
 *
 * Where a read fails, the rail says which stream is missing instead of quietly showing a
 * shorter history. A timeline with a silent hole in it is worse than no timeline: it invites
 * the reader to conclude that nothing happened.
 */
import { useCallback, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { Plate, PlateFoot, PlateHead } from '@/components/primitives/Plate';
import { Timeline, type TickEntry } from '@/components/semantic/Timeline';
import { Empty } from '@/components/states/SurfaceState';
import { changeResult, findingStatus, runState } from '@/domain/vocabulary';
import { formatTimestamp } from '@/domain/format';
import styles from './RecordRail.module.css';

const RECORD_LIMIT = 12;

export function RecordRail({ objectId, objectName }: { objectId: string; objectName: string }): JSX.Element {
  const { resource: findings } = useResource(
    `findings:object:${objectId}`,
    useCallback(
      (signal) => endpoints.findings({ object_id: objectId, limit: RECORD_LIMIT }, { signal }),
      [objectId],
    ),
  );
  const { resource: changes } = useResource(
    `changes:object:${objectId}`,
    useCallback(
      (signal) => endpoints.changes({ object_id: objectId, limit: RECORD_LIMIT }, { signal }),
      [objectId],
    ),
  );
  const { resource: runs } = useResource(
    `runs:object:${objectId}`,
    useCallback(
      (signal) => endpoints.runs({ object_id: objectId, limit: RECORD_LIMIT }, { signal }),
      [objectId],
    ),
  );

  const entries = useMemo<TickEntry[]>(() => {
    const collected: { sort: string; entry: TickEntry }[] = [];

    if (findings.status === 'success') {
      for (const finding of findings.data.findings) {
        const state = findingStatus(finding.status);
        collected.push({
          sort: finding.last_seen_at ?? finding.first_seen_at ?? '',
          entry: {
            id: `finding-${finding.finding_id}`,
            kind: finding.status === 'open' ? 'drift' : 'verify',
            at: formatTimestamp(finding.last_seen_at ?? finding.first_seen_at) ?? 'time not recorded',
            what: (
              <>
                <Link to={`/operations/findings/${finding.finding_id}`}>{finding.summary}</Link>
              </>
            ),
            note: `${finding.finding_type.replace(/_/g, ' ')} · ${state.label}`,
          },
        });
      }
    }

    if (changes.status === 'success') {
      for (const change of changes.data.changes) {
        const outcome = changeResult(change.result);
        collected.push({
          sort: change.finished_at ?? change.created_at ?? '',
          entry: {
            id: `change-${change.change_id}`,
            kind: 'change',
            at: formatTimestamp(change.finished_at ?? change.created_at) ?? 'time not recorded',
            what: (
              <>
                <Link to={`/operations/changes/${change.change_id}`}>{change.operation}</Link>
                {change.field ? <> · {change.field}</> : null}
              </>
            ),
            note: `${outcome.label} · host effect ${change.host_effect.replace(/_/g, ' ')}`,
          },
        });
      }
    }

    if (runs.status === 'success') {
      for (const run of runs.data.runs) {
        // A run that produced a change is already on the rail as that change; showing both
        // would double one event into two.
        if (run.change_created) continue;
        collected.push({
          sort: run.finished_at ?? run.created_at ?? '',
          entry: {
            id: `run-${run.run_id}`,
            kind: 'observe',
            at: formatTimestamp(run.finished_at ?? run.created_at) ?? 'time not recorded',
            what: (
              <>
                <Link to={`/operations/runs/${run.run_id}`}>{run.operation}</Link>
              </>
            ),
            note: `${runState(run.state).label} · nothing was written`,
          },
        });
      }
    }

    return collected
      .sort((a, b) => (a.sort < b.sort ? 1 : a.sort > b.sort ? -1 : 0))
      .slice(0, RECORD_LIMIT)
      .map((item) => item.entry);
  }, [findings, changes, runs]);

  const unread = [
    findings.status === 'failed' ? 'findings' : null,
    changes.status === 'failed' ? 'changes' : null,
    runs.status === 'failed' ? 'runs' : null,
  ].filter((name): name is string => name !== null);

  const loading = findings.status === 'loading' || changes.status === 'loading' || runs.status === 'loading';

  return (
    <Plate className={styles.rail}>
      <PlateHead title="Machine record" level={3} asOf={objectName} />
      <div className={styles.body}>
        {entries.length > 0 ? <Timeline entries={entries} /> : null}

        {entries.length === 0 && !loading && unread.length === 0 ? (
          <Empty
            title="Nothing has happened to this object"
            explanation="No finding, no run and no change references it. That is a record of nothing happening, not a record that was not kept."
          />
        ) : null}

        {loading && entries.length === 0 ? <p className={styles.pending}>Reading the record…</p> : null}

        {unread.length > 0 ? (
          <p className={styles.unread}>
            The {unread.join(' and ')} {unread.length === 1 ? 'stream' : 'streams'} could not be
            read, so this timeline is incomplete. What is shown is real; what is missing is
            unknown.
          </p>
        ) : null}
      </div>
      <PlateFoot source="every entry here was written by an observation, not by a person" />
    </Plate>
  );
}
