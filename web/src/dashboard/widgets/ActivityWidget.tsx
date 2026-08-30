/** Recent runs — what has been planned from here, and how it ended. */
import { useCallback } from 'react';
import { Link } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { Plate, PlateFoot, PlateHead } from '@/components/primitives/Plate';
import { DataTable } from '@/components/primitives/DataTable';
import { StatusMark } from '@/components/semantic/StatusPill';
import { Value } from '@/components/semantic/UnknownValue';
import { ResourceView } from '@/components/states/ResourceView';
import { Empty } from '@/components/states/SurfaceState';
import { runState } from '@/domain/vocabulary';
import { formatRelative } from '@/domain/format';
import { WidgetAction } from './shared';

const LIMIT = 6;

export function ActivityWidget(): JSX.Element {
  const { resource } = useResource(
    'runs:recent',
    useCallback((signal) => endpoints.runs({ limit: 20 }, { signal }), []),
  );

  return (
    <Plate quiet style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <ResourceView resource={resource} loadingLabel="Reading runs…">
        {(list, meta) => (
          <>
            <PlateHead
              title="Recent runs"
              meta="what has been planned from here, and how it ended"
              asOf={meta.fetchedAt.toLocaleTimeString()}
            >
              <WidgetAction to="/operations">All {list.count} ›</WidgetAction>
            </PlateHead>
            <DataTable
              caption="Recent runs"
              rows={list.runs.slice(0, LIMIT)}
              rowKey={(row) => row.run_id}
              emptyState={
                <Empty
                  title="No runs"
                  explanation="Nothing has been planned on this host. A run is created when an operation is planned; none has been."
                />
              }
              columns={[
                {
                  key: 'state',
                  header: '',
                  width: '28px',
                  align: 'center',
                  render: (row) => <StatusMark semantic={runState(row.state)} />,
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
                  key: 'object',
                  header: 'Target',
                  render: (row) => <Value value={row.object_name} mono />,
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
                  runs · a run plans an operation and publishes a preview; it does not imply a
                  host write
                </>
              }
            />
          </>
        )}
      </ResourceView>
    </Plate>
  );
}
