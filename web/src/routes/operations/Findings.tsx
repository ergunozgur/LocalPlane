/**
 * Findings — the durable claims LocalPlane is making about this host.
 *
 * A finding is not a drift comparison. `reconciliation` is recomputed on every read; a
 * finding is the record that LocalPlane *noticed*, when it first noticed, and how it ended.
 * That distinction is the backend's and this surface exists to keep it visible — it was
 * previously reduced to a number on the attention rail.
 *
 * Nothing here re-derives a conclusion. The status, the resolution and the evidence are all
 * the backend's, rendered as it published them.
 */
import { useCallback, useState } from 'react';
import { Link } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { useEstateCounts } from '@/hooks/useEstateCounts';
import { Plate, PlateFoot, PlateHead } from '@/components/primitives/Plate';
import { DataTable } from '@/components/primitives/DataTable';
import { StatusMark, StatusPill } from '@/components/semantic/StatusPill';
import { Value } from '@/components/semantic/UnknownValue';
import { ResourceView } from '@/components/states/ResourceView';
import { Empty } from '@/components/states/SurfaceState';
import { FilterChips, type FilterOption } from '@/components/primitives/FilterChips';
import { PageHeader } from '../PageHeader';
import { ScopeBar } from '@/components/layout/ScopeBar';
import { findingStatus, humanise } from '@/domain/vocabulary';
import { formatRelative } from '@/domain/format';
import styles from './Findings.module.css';

const FILTERS: readonly FilterOption[] = [
  { value: '', label: 'All' },
  { value: 'open', label: 'open', tone: 'attention' },
  { value: 'resolved', label: 'resolved', tone: 'good' },
];

export function Findings(): JSX.Element {
  const [status, setStatus] = useState<string>('open');
  const counts = useEstateCounts();

  const { resource } = useResource(
    `findings:${status}`,
    useCallback(
      (signal) =>
        endpoints.findings({ ...(status ? { status } : {}), limit: 200 }, { signal }),
      [status],
    ),
  );

  return (
    <>
      <ScopeBar
        crumbs={[{ label: 'operations', to: '/operations' }, { label: 'findings' }]}
        tabs={[
          { to: '/operations', label: 'Runs and changes', count: counts.changes, end: true },
          {
            to: '/operations/findings',
            label: 'Findings',
            count: counts.findings,
            drift: (counts.findings ?? 0) > 0,
          },
        ]}
      />

      <PageHeader
        title="Findings"
        annotation="A finding is the durable record that LocalPlane noticed something, when it first noticed, and how it ended. Drift is a comparison recomputed on every read; a finding is the claim. They are different things and this surface keeps them apart."
      />

      <Plate quiet>
        <ResourceView resource={resource} what="finding list" loadingLabel="Reading findings…">
          {(list, meta) => (
            <>
              <PlateHead
                title="Findings"
                meta="interpretations, not facts — each one can be argued with"
                asOf={meta.fetchedAt.toLocaleTimeString()}
                chips={
                  <FilterChips
                    legend="Finding status"
                    options={
                      // Only the fetched status has a real count; the others were not read.
                      status === ''
                        ? FILTERS.map((option) =>
                            option.value === ''
                              ? { ...option, count: list.count }
                              : {
                                  ...option,
                                  count: list.findings.filter((f) => f.status === option.value).length,
                                },
                          )
                        : FILTERS.map((option) =>
                            option.value === status ? { ...option, count: list.count } : option,
                          )
                    }
                    value={status}
                    onChange={setStatus}
                  />
                }
              />

              <DataTable
                caption="Findings"
                rows={list.findings}
                rowKey={(row) => row.finding_id}
                emptyState={
                  <Empty
                    title={status === 'open' ? 'No open findings' : 'No findings'}
                    explanation={
                      status === 'open'
                        ? 'LocalPlane is not currently making any durable claim about this host that it thinks somebody should read. That is a statement about the claims it holds, not a guarantee about the host.'
                        : 'Nothing has ever been recorded as a finding on this host.'
                    }
                  />
                }
                columns={[
                  {
                    key: 'mark',
                    header: '',
                    width: '30px',
                    align: 'center',
                    render: (row) => <StatusMark semantic={findingStatus(row.status)} />,
                  },
                  {
                    key: 'summary',
                    header: 'Finding',
                    render: (row) => (
                      <Link to={`/operations/findings/${row.finding_id}`} className={styles.summary}>
                        {row.summary}
                      </Link>
                    ),
                  },
                  {
                    key: 'type',
                    header: 'Type',
                    render: (row) => <Value value={row.finding_type} mono />,
                  },
                  {
                    key: 'object',
                    header: 'Object',
                    render: (row) => <Value value={row.object_name} mono />,
                  },
                  {
                    key: 'subject',
                    header: 'Subject',
                    render: (row) => <Value value={row.subject} mono />,
                  },
                  {
                    key: 'status',
                    header: 'Status',
                    render: (row) => (
                      <StatusPill semantic={findingStatus(row.status)} size="sm" />
                    ),
                  },
                  {
                    key: 'first',
                    header: 'First seen',
                    render: (row) => <Value value={formatRelative(row.first_seen_at)} />,
                  },
                  {
                    key: 'resolution',
                    header: 'Resolution',
                    render: (row) =>
                      row.resolution ? (
                        <span className={styles.resolution} title={row.resolution}>
                          {humanise(row.resolution)}
                        </span>
                      ) : (
                        <Value value={null} reason="this finding is still open" />
                      ),
                  },
                ]}
              />

              <PlateFoot source={`${list.count} finding${list.count === 1 ? '' : 's'} recorded`}>
                <span className={styles.aside}>
                  a finding is a claim LocalPlane is making; drift is a comparison it recomputes
                </span>
              </PlateFoot>
            </>
          )}
        </ResourceView>
      </Plate>
    </>
  );
}
