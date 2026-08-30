/**
 * Recent changes — the record of crossing the write boundary.
 *
 * The three columns are three separate questions, and they stay separate: whether the host
 * was written, whether that was proven, and how the change ended. Collapsing them into one
 * success badge is precisely what this product refuses to do.
 */
import { useCallback } from 'react';
import { Link } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { Plate, PlateFoot, PlateHead } from '@/components/primitives/Plate';
import { DataTable } from '@/components/primitives/DataTable';
import { StatusPill } from '@/components/semantic/StatusPill';
import { Value } from '@/components/semantic/UnknownValue';
import { ResourceView } from '@/components/states/ResourceView';
import { Empty } from '@/components/states/SurfaceState';
import { changeResult, mutationOutcome, verificationOutcome } from '@/domain/vocabulary';
import { formatRelative } from '@/domain/format';
import { WidgetAction } from './shared';

const LIMIT = 6;

export function ChangesWidget(): JSX.Element {
  const { resource } = useResource(
    'changes:recent',
    useCallback((signal) => endpoints.changes({ limit: 20 }, { signal }), []),
  );

  return (
    <Plate quiet style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <ResourceView resource={resource} loadingLabel="Reading changes…">
        {(list, meta) => (
          <>
            <PlateHead
              title="Recent changes"
              meta="the record of crossing the write boundary"
              asOf={meta.fetchedAt.toLocaleTimeString()}
            >
              <WidgetAction to="/operations">All {list.count} ›</WidgetAction>
            </PlateHead>
            <DataTable
              caption="Recent changes"
              rows={list.changes.slice(0, LIMIT)}
              rowKey={(row) => row.change_id}
              emptyState={
                <Empty
                  title="No changes"
                  explanation="LocalPlane has never crossed the write boundary on this host. Planning, confirming and arming all happen without a change, so their absence here is expected."
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
                  key: 'object',
                  header: 'Target',
                  render: (row) => <Value value={row.object_name} mono />,
                },
                {
                  key: 'mutation',
                  header: 'Host',
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
                  render: (row) => (
                    <StatusPill semantic={changeResult(row.result)} size="sm" />
                  ),
                },
                {
                  key: 'when',
                  header: 'Created',
                  render: (row) => <Value value={formatRelative(row.created_at)} />,
                },
              ]}
            />
            <PlateFoot
              source={
                <>
                  changes · a change exists only where LocalPlane entered a path on which a
                  host write may occur
                </>
              }
            />
          </>
        )}
      </ResourceView>
    </Plate>
  );
}
