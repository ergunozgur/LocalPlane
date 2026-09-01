/**
 * Operations — runs and changes.
 *
 * A Run plans a typed operation and publishes an immutable preview of what it would involve.
 * A Change is the record that LocalPlane entered the path on which a host write may occur.
 * They are two tables because they are two things, and a page that merged them would lose the
 * distinction the product is built on: planning, confirming and arming all happen without a
 * Change, because none of them can have moved anything.
 *
 * Filtering is the chip bar rather than a select, so the available states and how much
 * sits in each are visible without a click. Filtering still happens on the backend — a
 * narrowed list is the backend's answer, not a slice this console took — which is why counts
 * appear on the chips only while nothing is filtered. With a filter on, the other states'
 * counts are rows nobody fetched, and a number there would be invented.
 *
 * There is no Risk column. The design direction has one, and producing it here would mean a
 * preview request per row: risk is assessed per plan, and this surface lists up to 100 of
 * them.
 */
import { useCallback, useState } from 'react';
import { Link } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { Plate, PlateFoot, PlateHead } from '@/components/primitives/Plate';
import { DataTable } from '@/components/primitives/DataTable';
import { StatusMark, StatusPill } from '@/components/semantic/StatusPill';
import { ReconciliationChip } from '@/components/semantic/SemanticGlyph';
import { Value } from '@/components/semantic/UnknownValue';
import { ResourceView } from '@/components/states/ResourceView';
import { Empty } from '@/components/states/SurfaceState';
import { FilterChips, type FilterOption } from '@/components/primitives/FilterChips';
import { PageHeader } from '../PageHeader';
import { ScopeBar } from '@/components/layout/ScopeBar';
import { useEstateCounts } from '@/hooks/useEstateCounts';
import {
  changeResult,
  hostEffect,
  mutationOutcome,
  recoveryState,
  reconciliation as reconciliationOf,
  runState as runStateOf,
  verificationOutcome,
} from '@/domain/vocabulary';
import { formatRelative } from '@/domain/format';
import styles from './Operations.module.css';

const RUN_STATES: readonly FilterOption[] = [
  { value: '', label: 'All' },
  { value: 'preview', label: 'preview', tone: 'neutral' },
  { value: 'awaiting_confirmation', label: 'awaiting confirmation', tone: 'warn' },
  { value: 'succeeded', label: 'succeeded', tone: 'good' },
  { value: 'failed', label: 'failed', tone: 'bad' },
  { value: 'cancelled', label: 'cancelled', tone: 'neutral' },
];

const CHANGE_RESULTS: readonly FilterOption[] = [
  { value: '', label: 'All' },
  { value: 'in_flight', label: 'in flight', tone: 'warn' },
  { value: 'succeeded', label: 'succeeded', tone: 'good' },
  { value: 'failed', label: 'failed', tone: 'bad' },
  { value: 'rolled_back', label: 'rolled back', tone: 'attention' },
  { value: 'recovery_required', label: 'recovery required', tone: 'attention' },
];

/**
 * The operation's domain — the first segment of the backend's own typed operation name.
 *
 * `network.interface.reconcile_mtu` is in the `network` domain because the backend named it
 * that way. Nothing is inferred from the target object.
 */
function domainOf(operation: string): string {
  return operation.split('.')[0] ?? operation;
}

/** Counts per value, from rows already fetched. Returns undefined counts when filtered. */
function tally<T>(
  options: readonly FilterOption[],
  rows: readonly T[],
  of: (row: T) => string,
  filtered: boolean,
): readonly FilterOption[] {
  if (filtered) return options;
  const counts = new Map<string, number>();
  for (const row of rows) counts.set(of(row), (counts.get(of(row)) ?? 0) + 1);
  return options.map((option) => ({
    ...option,
    count: option.value === '' ? rows.length : (counts.get(option.value) ?? 0),
  }));
}

export function Operations(): JSX.Element {
  const [runState, setRunState] = useState<string>('');
  const [changeResultFilter, setChangeResultFilter] = useState<string>('');
  const counts = useEstateCounts();

  // Filters are the API's own query parameters, so a narrowed list is the backend's answer
  // rather than a slice this console took of a wider one.
  const { resource: runs } = useResource(
    `runs:${runState}`,
    useCallback(
      (signal) =>
        endpoints.runs({ ...(runState ? { state: runState } : {}), limit: 100 }, { signal }),
      [runState],
    ),
  );
  const { resource: changes } = useResource(
    `changes:${changeResultFilter}`,
    useCallback(
      (signal) =>
        endpoints.changes(
          { ...(changeResultFilter ? { result: changeResultFilter } : {}), limit: 100 },
          { signal },
        ),
      [changeResultFilter],
    ),
  );
  const { resource: drifted } = useResource(
    'interfaces',
    useCallback((signal) => endpoints.interfaces({ signal }), []),
  );

  return (
    <>
      <ScopeBar
        crumbs={[{ label: 'operations' }]}
        tabs={[
          { to: '/operations', label: 'Runs and changes', count: counts.changes, end: true },
          {
            to: '/operations/findings',
            label: 'Findings',
            count: counts.findings,
            // The dot is the count's qualifier, not a duplicate of it: a surface with open
            // findings is a surface somebody should read, and the count alone does not say so.
            drift: (counts.findings ?? 0) > 0,
          },
        ]}
      />

      <PageHeader
        title="Operations"
        annotation="A run plans a typed operation and publishes a preview. A change is the record that LocalPlane entered the path on which a host write may occur. A preview may exist while execution is blocked — that is the useful case."
      />

      <div className={styles.stack}>
        <ResourceView resource={drifted} what="interface list">
          {(list, meta) => {
            const managed = list.interfaces.filter((i) => i.management.state === 'managed');
            const drift = managed.filter((i) => i.reconciliation === 'drifted');
            const unknown = managed.filter((i) => i.reconciliation === 'unknown');
            return (
              <Plate tone={drift.length > 0 ? 'attention' : undefined}>
                <PlateHead
                  title="Drift"
                  level={3}
                  meta="managed objects whose runtime differs from their intent"
                  asOf={meta.fetchedAt.toLocaleTimeString()}
                  chips={
                    <>
                      <StatusPill semantic={reconciliationOf(drift.length > 0 ? 'drifted' : 'in_sync')} size="sm" />
                      <span className={styles.count}>
                        {managed.length} managed
                      </span>
                    </>
                  }
                />
                {managed.length === 0 ? (
                  <Empty
                    title="Nothing is managed"
                    explanation="Drift is a comparison against retained intent. With no managed object there is nothing to compare, which is a different statement from “no drift”."
                  />
                ) : drift.length === 0 && unknown.length === 0 ? (
                  <Empty
                    title="No drift"
                    explanation={`Every controlled field on ${managed.length} managed object${managed.length === 1 ? '' : 's'} matches its intent.`}
                  />
                ) : (
                  <DataTable
                    caption="Drifted objects"
                    rows={[...drift, ...unknown]}
                    rowKey={(row) => row.object_id}
                    columns={[
                      {
                        key: 'object',
                        header: 'Object',
                        render: (row) => (
                          <Link to={`/network/${row.object_id}`} className="mono">
                            {row.name}
                          </Link>
                        ),
                      },
                      {
                        key: 'reconciliation',
                        header: 'Reconciliation',
                        render: (row) => <ReconciliationChip state={row.reconciliation} />,
                      },
                      {
                        key: 'fields',
                        header: 'Controlled fields',
                        render: (row) => (
                          <Value value={row.intent?.controlled_fields.join(', ')} mono />
                        ),
                      },
                      {
                        key: 'observed',
                        header: 'Observed',
                        render: (row) => (
                          <Value value={formatRelative(row.last_seen_at)} />
                        ),
                      },
                    ]}
                  />
                )}
                <PlateFoot source="reconciliation is recomputed on every read">
                  <span className={styles.aside}>
                    drift is a fact about a managed object; a finding is a claim LocalPlane is
                    making
                  </span>
                </PlateFoot>
              </Plate>
            );
          }}
        </ResourceView>

        <Plate>
          <PlateHead title="Runs" level={3} meta="planned operations" />
          <ResourceView resource={runs} what="run list" loadingLabel="Reading runs…">
            {(list) => (
              <>
              <div className={styles.filterBar}>
                <FilterChips
                  legend="Run state"
                  options={tally(RUN_STATES, list.runs, (row) => row.state, runState !== '')}
                  value={runState}
                  onChange={setRunState}
                />
                <span className={styles.filterNote}>
                  {runState === ''
                    ? `${list.count} run${list.count === 1 ? '' : 's'} recorded`
                    : 'counts are hidden while a filter is on — the other states were not fetched'}
                </span>
              </div>
              <DataTable
                caption="Runs"
                rows={list.runs}
                rowKey={(row) => row.run_id}
                emptyState={
                  <Empty
                    title="No runs"
                    explanation="Nothing has been planned on this host. This build's frontend does not create runs; it renders the ones that exist."
                  />
                }
                columns={[
                  {
                    key: 'mark',
                    header: '',
                    width: '30px',
                    align: 'center',
                    render: (row) => <StatusMark semantic={runStateOf(row.state)} />,
                  },
                  {
                    key: 'operation',
                    header: 'Operation',
                    render: (row) => (
                      <Link to={`/operations/runs/${row.run_id}`} className="mono">
                        {row.operation}
                      </Link>
                    ),
                  },
                  {
                    key: 'domain',
                    header: 'Domain',
                    render: (row) => <span className={styles.domain}>{domainOf(row.operation)}</span>,
                  },
                  { key: 'object', header: 'Target', render: (row) => <Value value={row.object_name} mono /> },
                  {
                    key: 'state',
                    header: 'State',
                    render: (row) => <StatusPill semantic={runStateOf(row.state)} size="sm" />,
                  },
                  {
                    key: 'effect',
                    header: 'Host effect',
                    render: (row) => (
                      <StatusPill semantic={hostEffect(row.host_effect)} size="sm" />
                    ),
                  },
                  {
                    key: 'change',
                    header: 'Change',
                    render: (row) =>
                      row.change_id ? (
                        <Link to={`/operations/changes/${row.change_id}`} className="mono">
                          yes
                        </Link>
                      ) : (
                        <span className={styles.noChange} title="This run never crossed the write boundary.">
                          none
                        </span>
                      ),
                  },
                  {
                    key: 'actor',
                    header: 'Actor',
                    render: () => (
                      <span className={styles.actor} title="Requester attribution is not present in this summary record.">
                        not recorded
                      </span>
                    ),
                  },
                  { key: 'created', header: 'Created', render: (row) => <Value value={formatRelative(row.created_at)} /> },
                ]}
              />
              </>
            )}
          </ResourceView>
        </Plate>

        <Plate>
          <PlateHead
            title="Changes"
            level={3}
            meta="the write-boundary record"
          />
          <ResourceView resource={changes} what="change list" loadingLabel="Reading changes…">
            {(list) => (
              <>
              <div className={styles.filterBar}>
                <FilterChips
                  legend="Change result"
                  options={tally(
                    CHANGE_RESULTS,
                    list.changes,
                    (row) => row.result,
                    changeResultFilter !== '',
                  )}
                  value={changeResultFilter}
                  onChange={setChangeResultFilter}
                />
                <span className={styles.filterNote}>
                  {changeResultFilter === ''
                    ? `${list.count} change${list.count === 1 ? '' : 's'} recorded`
                    : 'counts are hidden while a filter is on — the other results were not fetched'}
                </span>
              </div>
              <DataTable
                caption="Changes"
                rows={list.changes}
                rowKey={(row) => row.change_id}
                emptyState={
                  <Empty
                    title="No changes"
                    explanation="LocalPlane has never crossed the write boundary on this host."
                  />
                }
                columns={[
                  {
                    key: 'operation',
                    header: 'Operation',
                    render: (row) => (
                      <Link to={`/operations/changes/${row.change_id}`} className="mono">
                        {row.operation}
                      </Link>
                    ),
                  },
                  {
                    key: 'domain',
                    header: 'Domain',
                    render: (row) => <span className={styles.domain}>{domainOf(row.operation)}</span>,
                  },
                  { key: 'object', header: 'Target', render: (row) => <Value value={row.object_name} mono /> },
                  {
                    key: 'mutation',
                    header: 'Host write',
                    render: (row) => (
                      <StatusPill
                        semantic={mutationOutcome(row.mutation_outcome)}
                        size="sm"
                        token={row.mutation_outcome}
                      />
                    ),
                  },
                  {
                    key: 'verification',
                    header: 'Verification',
                    render: (row) => (
                      <StatusPill
                        semantic={verificationOutcome(row.verification_outcome)}
                        size="sm"
                      />
                    ),
                  },
                  {
                    key: 'result',
                    header: 'Result',
                    render: (row) => <StatusPill semantic={changeResult(row.result)} size="sm" />,
                  },
                  {
                    key: 'recovery',
                    header: 'Recovery',
                    render: (row) => (
                      <StatusPill semantic={recoveryState(row.recovery_state)} size="sm" />
                    ),
                  },
                  {
                    key: 'actor',
                    header: 'Actor',
                    render: () => (
                      <span className={styles.actor} title="Requester attribution is not present in this summary record.">
                        not recorded
                      </span>
                    ),
                  },
                  { key: 'created', header: 'Created', render: (row) => <Value value={formatRelative(row.created_at)} /> },
                ]}
              />
              </>
            )}
          </ResourceView>
        </Plate>
      </div>
    </>
  );
}
