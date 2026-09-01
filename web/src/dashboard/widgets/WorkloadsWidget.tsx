/** Workloads — the containers this host runs, as the Docker daemon reports them. */
import { useCallback } from 'react';
import { Link } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { DataTable } from '@/components/primitives/DataTable';
import { StatusMark } from '@/components/semantic/StatusPill';
import { Value } from '@/components/semantic/UnknownValue';
import { ResourceView } from '@/components/states/ResourceView';
import { Empty } from '@/components/states/SurfaceState';
import { containerState } from '@/domain/vocabulary';
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
                meta="Docker containers as the provider last reported them"
                asOf={meta.fetchedAt.toLocaleTimeString()}
              >
                <WidgetAction to="/workloads">All {list.count} ›</WidgetAction>
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
                      list.last_sweep
                        ? 'The Docker daemon answered and reported none.'
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
                      <Link to={`/workloads/${row.object_id}`} className="mono">
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
