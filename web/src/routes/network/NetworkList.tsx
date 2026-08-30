/**
 * Network — every interface LocalPlane has observed.
 *
 * The columns are the axes the product insists are independent: management, ownership,
 * reconciliation, health and freshness. A bridge Docker runs is `observed` and `attributed`
 * and may be perfectly healthy; a managed interface may be drifted and healthy at once. The
 * table shows them side by side rather than reducing them to one status word.
 *
 * **The list is flat.** The hierarchy — which port carries which network, which container
 * sits on which bridge — lives in the topology's `.port` rows and in each object's own
 * workspace. A tree here would invent a containment the kernel does not report, and would
 * put a disclosure triangle between an operator and every second interface.
 *
 * What the rows took from the design and kept: whole-row activation, a drifted row that
 * carries its state in the row itself, the three-shape symbol language instead of three
 * word-pills, the capability that produced the row as a sub-entry under its name, and a
 * reserved traffic slot that stays empty because no throughput series exists.
 */
import { useCallback } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { DataTable } from '@/components/primitives/DataTable';
import { StatusMark, StatusPill } from '@/components/semantic/StatusPill';
import { ManagementChip, ReconciliationChip } from '@/components/semantic/SemanticGlyph';
import { Value } from '@/components/semantic/UnknownValue';
import { ResourceView } from '@/components/states/ResourceView';
import { Empty } from '@/components/states/SurfaceState';
import { PageHeader } from '../PageHeader';
import { ScopeBar } from '@/components/layout/ScopeBar';
import { useHostName } from '@/hooks/useEstateCounts';
import { AddressingSummary } from './AddressingSummary';
import styles from './NetworkList.module.css';
import { SweepCaveat, SweepFoot } from '@/dashboard/widgets/shared';
import {
  freshness as freshnessOf,
  health as healthOf,
  ownershipState,
} from '@/domain/vocabulary';

export function NetworkList(): JSX.Element {
  const { resource } = useResource(
    'interfaces',
    useCallback((signal) => endpoints.interfaces({ signal }), []),
  );
  const hostName = useHostName();
  const navigate = useNavigate();

  return (
    <ResourceView resource={resource} what="interface list" loadingLabel="Reading interfaces…">
      {(list, meta) => (
        <>
          <ScopeBar
            crumbs={[{ label: hostName, to: '/' }, { label: 'network' }]}
            tabs={[
              {
                to: '/network',
                label: 'Interfaces',
                count: list.count,
                end: true,
                drift: list.interfaces.some((item) => item.reconciliation === 'drifted'),
              },
            ]}
            observedAt={meta.fetchedAt}
          />

          <PageHeader
            title="Network"
            count={list.count}
            annotation="Management, ownership, reconciliation, health and freshness are independent of one another. An observed object does not drift; a drifted one may be perfectly healthy."
          />
          <Plate quiet>
            <PlateHead
              title="Interfaces"
              meta="every attachment this host has, and what it is supposed to be"
              asOf={meta.fetchedAt.toLocaleTimeString()}
              chips={
                <>
                  <span className={styles.chip}>
                    <b>{list.interfaces.filter((i) => i.management.state === 'managed').length}</b>{' '}
                    managed
                  </span>
                  <span className={styles.chip}>
                    <b>{list.interfaces.filter((i) => i.management.state === 'observed').length}</b>{' '}
                    observed
                  </span>
                </>
              }
            />
            {SweepCaveat({ sweep: list.last_sweep }) ? (
              <PlateBody tight>
                <SweepCaveat sweep={list.last_sweep} />
              </PlateBody>
            ) : null}
            <DataTable
              caption="Network interfaces"
              rows={list.interfaces}
              rowKey={(row) => row.object_id}
              onRowActivate={(row) => navigate(`/network/${row.object_id}`)}
              rowTone={(row) => (row.reconciliation === 'drifted' ? 'attention' : undefined)}
              emptyState={
                <Empty
                  title="No interfaces observed"
                  explanation="With no sweep to explain it, an empty list means nobody looked — not that this host has no interfaces."
                />
              }
              columns={[
                {
                  key: 'health',
                  header: '',
                  width: '30px',
                  align: 'center',
                  render: (row) => <StatusMark semantic={healthOf(row.health?.state)} />,
                },
                {
                  key: 'name',
                  header: 'Interface',
                  render: (row) => (
                    <span className={styles.nameCell}>
                      <Link to={`/network/${row.object_id}`} className="mono">
                        {row.name}
                      </Link>
                      {/* The capability that produced this row. It names the *mechanism that
                          read it*, which is a fact about this object — not permission to do
                          anything with it, which only a published plan can answer. */}
                      <span className={styles.capability}>
                        {row.observation?.capability ?? 'never observed'}
                        {row.observation?.provider ? ` · ${row.observation.provider}` : ''}
                      </span>
                    </span>
                  ),
                },
                { key: 'kind', header: 'Kind', render: (row) => <Value value={row.interface_kind} mono /> },
                {
                  key: 'state',
                  header: 'Link',
                  render: (row) => <Value value={row.link?.operstate} mono />,
                },
                {
                  key: 'mtu',
                  header: 'MTU',
                  align: 'right',
                  render: (row) => (
                    <Value value={row.link?.mtu} mono reason="the kernel did not report an MTU" />
                  ),
                },
                {
                  key: 'addresses',
                  header: 'Addresses',
                  render: (row) =>
                    row.addresses === null ? (
                      <Value value={null} reason="no address source was available" />
                    ) : row.addresses.length === 0 ? (
                      <span className="mono" title="Observed, and it has none.">
                        none
                      </span>
                    ) : (
                      <span className="mono">
                        {row.addresses[0]?.address}/{row.addresses[0]?.prefix_length}
                        {row.addresses.length > 1 ? ` +${row.addresses.length - 1}` : ''}
                      </span>
                    ),
                },
                {
                  // Three axes, three shapes: circle for health, square for management, mono
                  // operator for reconciliation. Three word-pills in a row made them look
                  // like one status repeated.
                  key: 'management',
                  header: 'Management',
                  render: (row) => <ManagementChip state={row.management.state} />,
                },
                {
                  key: 'reconciliation',
                  header: 'Reconciliation',
                  render: (row) => <ReconciliationChip state={row.reconciliation} />,
                },
                {
                  key: 'ownership',
                  header: 'Ownership',
                  render: (row) => (
                    <StatusPill
                      semantic={ownershipState(row.ownership.state)}
                      size="sm"
                      token={row.ownership.configured_by?.owner.provider ?? row.ownership.reason}
                    />
                  ),
                },
                {
                  key: 'freshness',
                  header: 'Observed',
                  render: (row) => (
                    <StatusPill semantic={freshnessOf(row.observation?.freshness)} size="sm" />
                  ),
                },
                {
                  // The reserved traffic slot. LocalPlane keeps no throughput history, so
                  // nothing is drawn here — the space is held so a real series can arrive
                  // without every row in the table moving.
                  key: 'traffic',
                  header: (
                    <span title="LocalPlane keeps no throughput history. The column is held open so a real series can arrive without every row moving.">
                      Traffic
                    </span>
                  ),
                  width: '64px',
                  render: () => (
                    <span
                      className={styles.trafficSlot}
                      aria-hidden="true"
                      title="No traffic history is kept, so there is no line to draw."
                    />
                  ),
                },
                {
                  key: 'open',
                  header: '',
                  align: 'right',
                  render: (row) => (
                    <Link to={`/network/${row.object_id}`} className={styles.open}>
                      open ›
                    </Link>
                  ),
                },
              ]}
            />
            <SweepFoot sweep={list.last_sweep} subject="interfaces" />
          </Plate>

          <AddressingSummary interfaces={list.interfaces} observedAt={meta.fetchedAt} />
        </>
      )}
    </ResourceView>
  );
}
