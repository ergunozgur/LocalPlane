/** Network — interfaces, their link state and who configures them. */
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
import { health as healthOf } from '@/domain/vocabulary';
import { SweepCaveat, SweepFoot, WidgetAction } from './shared';

const LIMIT = 8;

export function NetworkWidget(): JSX.Element {
  const { resource } = useResource(
    'interfaces',
    useCallback((signal) => endpoints.interfaces({ signal }), []),
  );

  return (
    <Plate quiet style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <ResourceView resource={resource} loadingLabel="Reading interfaces…">
        {(list, meta) => {
          const failed = list.interfaces.filter((row) => row.health?.state === 'failed').length;
          return (
            <>
              <PlateHead
                title="Network"
                meta="ports, bridges and tunnels, and who configures each"
                mark={healthOf(failed > 0 ? 'failed' : 'healthy')}
                asOf={meta.fetchedAt.toLocaleTimeString()}
              >
                <WidgetAction to="/network">All {list.count} ›</WidgetAction>
              </PlateHead>

              {SweepCaveat({ sweep: list.last_sweep }) ? (
                <PlateBody tight>
                  <SweepCaveat sweep={list.last_sweep} />
                </PlateBody>
              ) : null}

              <DataTable
                caption="Network interfaces"
                rows={list.interfaces.slice(0, LIMIT)}
                rowKey={(row) => row.object_id}
                emptyState={
                  <Empty
                    title="No interfaces"
                    explanation="No interface has been observed. With no sweep to explain it, this is an absence of evidence rather than an absence of interfaces."
                  />
                }
                columns={[
                  {
                    key: 'health',
                    header: '',
                    width: '28px',
                    align: 'center',
                    render: (row) => <StatusMark semantic={healthOf(row.health?.state)} />,
                  },
                  {
                    key: 'name',
                    header: 'Interface',
                    render: (row) => (
                      <Link to={`/network/${row.object_id}`} className="mono">
                        {row.name}
                      </Link>
                    ),
                  },
                  {
                    key: 'kind',
                    header: 'Kind',
                    render: (row) => <Value value={row.interface_kind} mono />,
                  },
                  {
                    key: 'owner',
                    header: 'Configured by',
                    render: (row) => (
                      <Value
                        value={row.ownership.configured_by?.owner.provider}
                        mono
                        reason="no source attributed this interface"
                      />
                    ),
                  },
                ]}
              />
              <SweepFoot sweep={list.last_sweep} subject="interfaces" />
            </>
          );
        }}
      </ResourceView>
    </Plate>
  );
}
