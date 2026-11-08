/**
 * Every address this host holds, and where each came from.
 *
 * This sits beneath the interface table, and every column is already in the contract:
 * `Address` carries family, scope, prefix length and both lifetimes, and the interface's
 * ownership names who configures it. The frontend had been showing only the first address
 * per interface and dropping the rest.
 *
 * `dynamic` is the kernel's own flag, and a lifetime of `null` means *forever* only when the
 * kernel said so — an address whose lifetime was not reported renders as unknown rather than
 * as permanent.
 */
import type { NetworkInterface } from '@/api/types';
import { Link } from 'react-router-dom';
import { Plate, PlateFoot, PlateHead } from '@/components/primitives/Plate';
import { DataTable } from '@/components/primitives/DataTable';
import { Value } from '@/components/semantic/UnknownValue';
import { Empty } from '@/components/states/SurfaceState';
import { addressLifetimeState, formatAddressLifetime, formatTimestamp } from '@/domain/format';
import { inventoryMembership } from '@/domain/inventory';
import { freshness as freshnessOf } from '@/domain/vocabulary';
import { StatusPill } from '@/components/semantic/StatusPill';
import styles from './AddressingSummary.module.css';

interface Row {
  key: string;
  address: string;
  interfaceName: string;
  objectId: string;
  membership: boolean | null;
  observation: NetworkInterface['observation'];
  family: string;
  scope: string | null;
  source: string | null;
  dynamic: boolean | null;
  validState: 'unknown' | 'finite' | 'infinite';
  validSeconds: number | null;
  preferredState: 'unknown' | 'finite' | 'infinite';
  preferredSeconds: number | null;
}

function LifetimeCell({
  state,
  seconds,
}: {
  state: 'unknown' | 'finite' | 'infinite';
  seconds: number | null;
}): JSX.Element {
  if (state === 'infinite') {
    return (
      <span className={styles.forever} title="The kernel reports this lifetime does not expire.">
        forever
      </span>
    );
  }
  return (
    <span className={state === 'unknown' ? styles.unknownLifetime : 'mono'}>
      {formatAddressLifetime(state, seconds)}
    </span>
  );
}

export function AddressingSummary({
  interfaces,
  observedAt,
}: {
  interfaces: readonly NetworkInterface[];
  observedAt: Date;
}): JSX.Element {
  const rows: Row[] = [];
  let unread = 0;

  for (const item of interfaces) {
    // `null` addresses means no source was available — different from an interface with none.
    if (item.addresses === null) {
      unread += 1;
      continue;
    }
    for (const address of item.addresses) {
      rows.push({
        key: `${item.object_id}-${address.family}-${address.address}-${address.prefix_length}`,
        address: `${address.address}/${address.prefix_length}`,
        interfaceName: item.name,
        objectId: item.object_id,
        membership: item.observed_in_latest_sweep,
        observation: item.observation,
        family: address.family,
        scope: address.scope,
        source: item.ownership.configured_by?.owner.provider ?? null,
        dynamic: address.dynamic,
        validState: addressLifetimeState(address.valid_lifetime_state, address.valid_lifetime_s),
        validSeconds: address.valid_lifetime_s,
        preferredState: addressLifetimeState(address.preferred_lifetime_state, address.preferred_lifetime_s),
        preferredSeconds: address.preferred_lifetime_s,
      });
    }
  }

  return (
    <Plate quiet className={styles.plate}>
      <PlateHead
        title="Addressing"
        meta="stored address records, including retained interfaces"
        asOf={observedAt.toLocaleTimeString()}
        chips={<span className={styles.chip}>{rows.length} stored addresses</span>}
      />
      <DataTable
        caption="Addressing summary"
        rows={rows}
        rowKey={(row) => row.key}
        emptyState={
          <Empty
            title={unread > 0 ? 'No address records reported' : 'No stored addresses'}
            explanation={
              unread > 0
                ? 'Address sources were unavailable for some interfaces; their addresses are unknown, not absent.'
                : 'No stored interface record reports an address. This does not establish current absence.'
            }
          />
        }
        columns={[
          {
            key: 'address',
            header: 'Address',
            render: (row) => <span className="mono">{row.address}</span>,
          },
          {
            key: 'interface',
            header: 'Interface',
            render: (row) => (
              <>
                <Link className="mono" to={`/network/${encodeURIComponent(row.objectId)}`}>{row.interfaceName}</Link>
                <div className={styles.recordEvidence}>
                  <StatusPill semantic={inventoryMembership(row.membership)} size="sm" />
                  <StatusPill semantic={freshnessOf(row.observation?.freshness ?? 'never_observed')} size="sm" />
                  <span>{formatTimestamp(row.observation?.observed_at) ?? 'Observation time unknown'}</span>
                </div>
              </>
            ),
          },
          { key: 'family', header: 'Family', render: (row) => <Value value={row.family} mono /> },
          { key: 'scope', header: 'Scope', render: (row) => <Value value={row.scope} mono /> },
          {
            key: 'source',
            header: 'Source',
            render: (row) => (
              <Value
                value={row.source}
                mono
                reason="no source attributed this interface's configuration"
              />
            ),
          },
          {
            key: 'assignment',
            header: 'Assignment',
            render: (row) => (
              <Value
                value={row.dynamic === null ? null : row.dynamic ? 'dynamic' : 'static'}
                mono
                reason="the kernel did not report whether this address is dynamic"
              />
            ),
          },
          {
            key: 'valid',
            header: 'Valid lifetime',
            render: (row) => <LifetimeCell state={row.validState} seconds={row.validSeconds} />,
          },
          {
            key: 'preferred',
            header: 'Preferred lifetime',
            render: (row) => <LifetimeCell state={row.preferredState} seconds={row.preferredSeconds} />,
          },
        ]}
      />
      <PlateFoot source="rtnetlink address records, correlated with each interface's ownership">
        <span>Lifetimes are reported at each interface observation, not a live countdown.</span>
        {unread > 0 ? (
          <span className={styles.unread}>
            {unread} interface{unread === 1 ? '' : 's'} had no address source available — their
            addresses are unknown, not absent
          </span>
        ) : null}
      </PlateFoot>
    </Plate>
  );
}
