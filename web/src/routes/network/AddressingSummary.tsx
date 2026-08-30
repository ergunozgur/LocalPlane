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
import { Plate, PlateFoot, PlateHead } from '@/components/primitives/Plate';
import { DataTable } from '@/components/primitives/DataTable';
import { Value } from '@/components/semantic/UnknownValue';
import { Empty } from '@/components/states/SurfaceState';
import { formatDuration } from '@/domain/format';
import styles from './AddressingSummary.module.css';

interface Row {
  key: string;
  address: string;
  interfaceName: string;
  objectId: string;
  family: string;
  scope: string | null;
  source: string | null;
  dynamic: boolean | null;
  valid: number | null;
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
        family: address.family,
        scope: address.scope,
        source: item.ownership.configured_by?.owner.provider ?? null,
        dynamic: address.dynamic,
        valid: address.valid_lifetime_s,
      });
    }
  }

  return (
    <Plate quiet className={styles.plate}>
      <PlateHead
        title="Addressing"
        meta="every address this host holds, and what put it there"
        asOf={observedAt.toLocaleTimeString()}
        chips={<span className={styles.chip}>{rows.length}</span>}
      />
      <DataTable
        caption="Addressing summary"
        rows={rows}
        rowKey={(row) => row.key}
        emptyState={
          <Empty
            title="No addresses"
            explanation="No observed interface reports an address."
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
            render: (row) => <span className="mono">{row.interfaceName}</span>,
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
            key: 'lifetime',
            header: 'Lifetime',
            render: (row) =>
              row.valid === null ? (
                <span className={styles.forever} title="The kernel reports no expiry.">
                  forever
                </span>
              ) : (
                <span className="mono">{formatDuration(row.valid)}</span>
              ),
          },
        ]}
      />
      <PlateFoot source="rtnetlink address records, correlated with each interface's ownership">
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
