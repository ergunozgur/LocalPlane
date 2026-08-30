/**
 * Services — systemd units, as the system manager reports them.
 *
 * The Services widget is an Observed | = | Intended comparison, which is the right shape
 * for a managed estate. Nothing in this build's systemd support is managed — units are
 * observe-only and carry no retained intent — so the comparison column would be empty on
 * every row, and an empty "Intended" column reads as an unset value rather than as an
 * inapplicable question. The observed side is shown on its own, and the head says why.
 */
import { useCallback } from 'react';
import { Link } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { DataTable } from '@/components/primitives/DataTable';
import { StatusMark, StatusPill } from '@/components/semantic/StatusPill';
import { Value } from '@/components/semantic/UnknownValue';
import { ResourceView } from '@/components/states/ResourceView';
import { Empty, Degraded } from '@/components/states/SurfaceState';
import { capabilityStatus, health as healthOf, unitActiveState } from '@/domain/vocabulary';
import { SweepCaveat, SweepFoot, WidgetAction } from './shared';

const LIMIT = 8;

export function ServicesWidget(): JSX.Element {
  const { resource } = useResource(
    'systemd-units',
    useCallback((signal) => endpoints.systemdUnits({ signal }), []),
  );

  return (
    <Plate quiet style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <ResourceView resource={resource} loadingLabel="Reading units…">
        {(list, meta) => {
          const failed = list.units.filter((unit) => unit.active_state === 'failed');
          const shown = failed.length > 0 ? failed : list.units;
          return (
            <>
              <PlateHead
                title="Services"
                meta={`${list.count} units under watch, none managed`}
                mark={healthOf(failed.length > 0 ? 'failed' : 'healthy')}
                asOf={meta.fetchedAt.toLocaleTimeString()}
              >
                <WidgetAction to="/system">All {list.count} ›</WidgetAction>
              </PlateHead>

              {list.capability && list.capability.status !== 'available' ? (
                <PlateBody tight>
                  <Degraded title="The systemd observation capability is not fully available">
                    <StatusPill
                      semantic={capabilityStatus(list.capability.status)}
                      size="sm"
                      token={list.capability.capability}
                    />{' '}
                    {list.capability.reason ?? ''}
                  </Degraded>
                </PlateBody>
              ) : SweepCaveat({ sweep: list.last_sweep }) ? (
                <PlateBody tight>
                  <SweepCaveat sweep={list.last_sweep} />
                </PlateBody>
              ) : null}

              {failed.length > 0 ? (
                <PlateBody tight>
                  <p className="note" style={{ margin: 0, color: 'var(--crimson)' }}>
                    Showing the {failed.length} failed unit{failed.length === 1 ? '' : 's'} first.
                  </p>
                </PlateBody>
              ) : null}

              <DataTable
                caption="systemd units"
                rows={shown.slice(0, LIMIT)}
                rowKey={(row) => row.object_id}
                emptyState={
                  <Empty
                    title="No units"
                    explanation="Nothing has read the systemd estate, or the manager reported no loaded units."
                  />
                }
                columns={[
                  {
                    key: 'state',
                    header: '',
                    width: '28px',
                    align: 'center',
                    render: (row) => <StatusMark semantic={unitActiveState(row.active_state)} />,
                  },
                  {
                    key: 'unit',
                    header: 'Unit',
                    render: (row) => (
                      <Link to={`/system/${row.object_id}`} className="mono">
                        {row.canonical_id}
                      </Link>
                    ),
                  },
                  {
                    key: 'active',
                    header: 'Active',
                    render: (row) => <Value value={row.active_state} mono />,
                  },
                  { key: 'sub', header: 'Sub', render: (row) => <Value value={row.sub_state} mono /> },
                  {
                    key: 'file',
                    header: 'Unit file',
                    render: (row) => <Value value={row.unit_file_state} mono />,
                  },
                ]}
              />
              <SweepFoot sweep={list.last_sweep} subject="systemd units" />
            </>
          );
        }}
      </ResourceView>
    </Plate>
  );
}
