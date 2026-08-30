/**
 * System — the systemd estate.
 *
 * 304 units on the host this was built against, so search and filtering are not a nicety.
 * Both are client-side: the endpoint takes no query parameters, and pulling the whole
 * bounded loaded-unit estate once is cheaper than a round trip per keystroke. If the estate
 * ever outgrows that, the seam is this component and a `q`/`limit` parameter on the endpoint.
 */
import { useCallback, useState } from 'react';
import { Link } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { DataTable } from '@/components/primitives/DataTable';
import { StatusMark, StatusPill } from '@/components/semantic/StatusPill';
import { ManagementChip } from '@/components/semantic/SemanticGlyph';
import { Value } from '@/components/semantic/UnknownValue';
import { ResourceView } from '@/components/states/ResourceView';
import { Empty, Degraded } from '@/components/states/SurfaceState';
import { PageHeader } from '../PageHeader';
import { ScopeBar } from '@/components/layout/ScopeBar';
import { useHostName } from '@/hooks/useEstateCounts';
import { activeSince } from '@/domain/systemd';
import { SweepCaveat, SweepFoot } from '@/dashboard/widgets/shared';
import {
  capabilityStatus,
  unitActiveState,
  unitFileState,
  unitLoadState,
} from '@/domain/vocabulary';
import styles from './SystemList.module.css';

const UNIT_TYPES = ['service', 'socket', 'timer', 'target', 'mount', 'path'] as const;
const ACTIVE_STATES = ['active', 'inactive', 'failed', 'activating', 'deactivating'] as const;

export function SystemList(): JSX.Element {
  const { resource } = useResource(
    'systemd-units',
    useCallback((signal) => endpoints.systemdUnits({ signal }), []),
  );
  const hostName = useHostName();
  const [query, setQuery] = useState('');
  const [type, setType] = useState<string>('');
  const [active, setActive] = useState<string>('');

  return (
    <ResourceView resource={resource} what="unit list" loadingLabel="Reading units…">
      {(list, meta) => {
        const needle = query.trim().toLowerCase();
        const managed = list.units.filter((u) => u.management.state === 'managed').length;
        const failed = list.units.filter((u) => u.active_state === 'failed').length;
        const units = list.units.filter((unit) => {
          if (type && unit.unit_type !== type) return false;
          if (active && unit.active_state !== active) return false;
          if (!needle) return true;
          return (
            unit.canonical_id.toLowerCase().includes(needle) ||
            (unit.description ?? '').toLowerCase().includes(needle)
          );
        });

        return (
          <>
            <ScopeBar
              crumbs={[{ label: hostName, to: '/' }, { label: 'system' }]}
              tabs={[
                { to: '/system', label: 'Units', count: list.count, end: true },
                {
                  to: '/system?type=service',
                  label: 'Services',
                  count: list.units.filter((u) => u.unit_type === 'service').length,
                },
              ]}
              observedAt={meta.fetchedAt}
            />

            <PageHeader
              title="System"
              count={list.count}
              annotation="The system manager's bounded loaded-unit estate, read through the official D-Bus API. LocalPlane never loads a unit to look at it, so a unit systemd has not loaded does not appear here."
            />

            {/* The unit list is the one envelope carrying its capability. An unavailable
                capability and an empty estate are indistinguishable without it. */}
            {list.capability && list.capability.status !== 'available' ? (
              <div className={styles.capabilityNotice}>
                <Degraded title="The systemd observation capability is not fully available">
                  <StatusPill
                    semantic={capabilityStatus(list.capability.status)}
                    size="sm"
                    token={list.capability.capability}
                  />{' '}
                  {list.capability.reason ?? ''} — what is listed below is what could be read.
                </Degraded>
              </div>
            ) : null}

            <Plate quiet>
              <PlateHead
                title="Units"
                meta="every unit the system manager has loaded"
                mark={unitActiveState(failed > 0 ? 'failed' : 'active')}
                asOf={meta.fetchedAt.toLocaleTimeString()}
                chips={
                  <>
                    <span className={styles.chip}>
                      <b>{managed}</b> managed
                    </span>
                    <span className={styles.chip}>
                      <b>{list.count - managed}</b> observed
                    </span>
                    {failed > 0 ? (
                      <span className={`${styles.chip} ${styles.chipBad}`}>
                        <b>{failed}</b> failed
                      </span>
                    ) : null}
                  </>
                }
              />
              <PlateBody>
                <div className={styles.filters}>
                  <label className={styles.search}>
                    <span className="visually-hidden">Search units</span>
                    <input
                      type="search"
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder="Search by unit name or description…"
                      className={styles.input}
                    />
                  </label>

                  <label className={styles.filter}>
                    <span className="visually-hidden">Unit type</span>
                    <select
                      value={type}
                      onChange={(event) => setType(event.target.value)}
                      className={styles.select}
                    >
                      <option value="">All types</option>
                      {UNIT_TYPES.map((option) => (
                        <option key={option} value={option}>{option}</option>
                      ))}
                    </select>
                  </label>

                  <label className={styles.filter}>
                    <span className="visually-hidden">Active state</span>
                    <select
                      value={active}
                      onChange={(event) => setActive(event.target.value)}
                      className={styles.select}
                    >
                      <option value="">All states</option>
                      {ACTIVE_STATES.map((option) => (
                        <option key={option} value={option}>{option}</option>
                      ))}
                    </select>
                  </label>

                  <span className={styles.resultCount}>
                    {units.length} of {list.count}
                  </span>
                </div>

                <SweepCaveat sweep={list.last_sweep} />
              </PlateBody>

              <DataTable
                caption="systemd units"
                rows={units}
                rowKey={(row) => row.object_id}
                emptyState={
                  <Empty
                    title={needle || type || active ? 'No unit matches' : 'No units'}
                    explanation={
                      needle || type || active
                        ? 'No loaded unit matches these filters.'
                        : 'Nothing has read the systemd estate, or the manager reported no loaded units.'
                    }
                  />
                }
                columns={[
                  {
                    key: 'mark',
                    header: '',
                    width: '30px',
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
                    key: 'description',
                    header: 'Description',
                    render: (row) => <Value value={row.description} />,
                  },
                  {
                    key: 'load',
                    header: 'Load',
                    render: (row) => (
                      <StatusPill semantic={unitLoadState(row.load_state)} size="sm" />
                    ),
                  },
                  {
                    key: 'active',
                    header: 'Active',
                    render: (row) => (
                      <StatusPill semantic={unitActiveState(row.active_state)} size="sm" />
                    ),
                  },
                  { key: 'sub', header: 'Sub', render: (row) => <Value value={row.sub_state} mono /> },
                  {
                    key: 'file',
                    header: 'Unit file',
                    render: (row) => (
                      <StatusPill semantic={unitFileState(row.unit_file_state)} size="sm" />
                    ),
                  },
                  {
                    key: 'management',
                    header: 'Management',
                    render: (row) => <ManagementChip state={row.management.state} />,
                  },
                  {
                    key: 'since',
                    header: 'Since',
                    render: (row) => (
                      <Value
                        value={activeSince(row)}
                        reason="the manager reports no activation time for this unit"
                      />
                    ),
                  },
                ]}
              />
              <SweepFoot sweep={list.last_sweep} subject="systemd units" />
          </Plate>
          </>
        );
      }}
    </ResourceView>
  );
}
