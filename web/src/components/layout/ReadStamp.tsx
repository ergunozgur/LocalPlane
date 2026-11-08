/**
 * When this console last heard from the backend, and a control to ask again.
 *
 * The design direction shows a live-state control with a pulse and a running clock. This
 * is the honest version of it. It does **not** claim a live feed: nothing here polls, and a
 * pulsing "live" badge over a page that fetched once would be a decorative lie about how
 * current the numbers are.
 *
 * "Re-read" reloads this console's view of LocalPlane's stored records. It does **not** ask
 * the host to be observed again — explicit observation controls on Network, System, Storage
 * and Workloads start provider reads. This button only triggers stored-record reads.
 */
import { useCallback } from 'react';
import { endpoints } from '@/api/endpoints';
import { refreshAll, useResource } from '@/hooks/useResource';
import styles from './ReadStamp.module.css';

export function ReadStamp(): JSX.Element {
  const { resource } = useResource(
    'status',
    useCallback((signal) => endpoints.status({ signal }), []),
  );

  const stamp =
    resource.status === 'success' ? resource.fetchedAt.toLocaleTimeString() : '—';
  const reachable = resource.status === 'success';

  return (
    <button
      type="button"
      className={styles.stamp}
      onClick={refreshAll}
      aria-label="Re-read stored records. This does not ask the host to be observed again."
      data-reachable={reachable}
    >
      <span className={styles.dot} aria-hidden="true" />
      <span className={styles.label}>read</span>
      <span className={styles.clock}>{stamp}</span>
      <span className={styles.glyph} aria-hidden="true">
        ⟳
      </span>
    </button>
  );
}
