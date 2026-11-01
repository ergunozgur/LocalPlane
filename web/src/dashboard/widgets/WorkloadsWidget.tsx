/** Workloads — the containers this host runs, as the Docker daemon reports them. */
import { useCallback } from 'react';
import { Link } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { DataTable } from '@/components/primitives/DataTable';
import { StatusMark, StatusPill } from '@/components/semantic/StatusPill';
import { Value } from '@/components/semantic/UnknownValue';
import { ResourceView } from '@/components/states/ResourceView';
import { Empty } from '@/components/states/SurfaceState';
import { containerState } from '@/domain/vocabulary';
import { inventoryMembership } from '@/domain/workloads';
import { SweepCaveat, SweepFoot, WidgetAction } from './shared';

const LIMIT = 8;

export function WorkloadsWidget(): JSX.Element {
  const { resource } = useResource(
    'containers',
    useCallback((signal) => endpoints.containers({ signal }), []),
  );

  return (
    <Plate quiet style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <ResourceView resource={resource} loadingLabel="Reading containers…">
        {(list, meta) => {
          return (
            <>
              <PlateHead
                title="Workloads"
                meta="Stored Docker records with latest-inventory membership"
                asOf={meta.fetchedAt.toLocaleTimeString()}
              >
                <WidgetAction to="/workloads">All {list.count} stored ›</WidgetAction>
              </PlateHead>

              {SweepCaveat({ sweep: list.last_sweep }) ? (
                <PlateBody tight>
                  <SweepCaveat sweep={list.last_sweep} />
                </PlateBody>
              ) : null}

              <DataTable
                caption="Containers"
                rows={list.containers.slice(0, LIMIT)}
                rowKey={(row) => row.object_id}
                emptyState={
                  <Empty
                    title="No containers"
                    explanation={
                      list.last_sweep?.status === 'ok' && list.last_sweep.issues.length === 0
                        ? 'A complete stored Docker inventory recorded no containers. This does not describe a live daemon read.'
                        : list.last_sweep?.status === 'failed'
                          ? 'The most recent Docker inventory failed, so an empty stored list does not establish that no containers exist.'
                          : list.last_sweep
                            ? 'The most recent Docker inventory was incomplete or has an unrecognised status, so an empty stored list does not establish that no containers exist.'
                            : 'No sweep has recorded containers, so this is not evidence that there are none.'
                    }
                  />
                }
                columns={[
                  {
                    key: 'state',
                    header: '',
                    width: '28px',
                    align: 'center',
                    render: (row) => <StatusMark semantic={containerState(row.runtime.state)} />,
                  },
                  {
                    key: 'name',
                    header: 'Container',
                    render: (row) => (
                      <Link to={`/workloads/${encodeURIComponent(row.object_id)}`} className="mono">
                        {row.name}
                      </Link>
                    ),
                  },
                  {
                    key: 'image',
                    header: 'Image',
                    render: (row) => <Value value={row.image.reference} mono />,
                  },
                  {
                    key: 'runtime',
                    header: 'State',
                    render: (row) => <Value value={row.runtime.state} mono />,
                  },
                  {
                    key: 'membership',
                    header: 'Latest inventory',
                    render: (row) => <StatusPill semantic={inventoryMembership(row.observed_in_latest_sweep)} size="sm" />,
                  },
                ]}
              />
              <SweepFoot sweep={list.last_sweep} subject="containers" />
            </>
          );
        }}
      </ResourceView>
    </Plate>
  );
}
