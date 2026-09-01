/**
 * The counts the navigation and breadcrumbs quote.
 *
 * Every one comes from a list endpoint the console already reads, so the shared in-flight
 * request means the nav costs nothing beyond what a page was fetching anyway. A count that
 * could not be read is `null` — never zero, because "nobody looked" and "there are none" are
 * different answers and the nav must not merge them.
 */
import { useCallback } from 'react';
import { endpoints } from '@/api/endpoints';
import { useResource } from './useResource';

export interface EstateCounts {
  interfaces: number | null;
  containers: number | null;
  containerGroups: number | null;
  units: number | null;
  services: number | null;
  runs: number | null;
  changes: number | null;
  findings: number | null;
}

/** Compose groups a container into a project; one without the label stands alone. */
export const COMPOSE_PROJECT = 'com.docker.compose.project';

export function useEstateCounts(): EstateCounts {
  const { resource: interfaces } = useResource(
    'interfaces',
    useCallback((signal) => endpoints.interfaces({ signal }), []),
  );
  const { resource: containers } = useResource(
    'containers',
    useCallback((signal) => endpoints.containers({ signal }), []),
  );
  const { resource: units } = useResource(
    'systemd-units',
    useCallback((signal) => endpoints.systemdUnits({ signal }), []),
  );
  const { resource: runs } = useResource(
    'runs:recent',
    useCallback((signal) => endpoints.runs({ limit: 20 }, { signal }), []),
  );
  const { resource: changes } = useResource(
    'changes:recent',
    useCallback((signal) => endpoints.changes({ limit: 20 }, { signal }), []),
  );
  const { resource: findings } = useResource(
    'findings:open',
    useCallback((signal) => endpoints.findings({ status: 'open', limit: 50 }, { signal }), []),
  );

  const containerList = containers.status === 'success' ? containers.data.containers : null;
  const containerGroups =
    containerList === null
      ? null
      : new Set(
          containerList.map(
            (container) => container.labels[COMPOSE_PROJECT] ?? `standalone:${container.object_id}`,
          ),
        ).size;

  return {
    interfaces: interfaces.status === 'success' ? interfaces.data.count : null,
    containers: containers.status === 'success' ? containers.data.count : null,
    containerGroups,
    units: units.status === 'success' ? units.data.count : null,
    services:
      units.status === 'success'
        ? units.data.units.filter((unit) => unit.unit_type === 'service').length
        : null,
    runs: runs.status === 'success' ? runs.data.count : null,
    changes: changes.status === 'success' ? changes.data.count : null,
    findings: findings.status === 'success' ? findings.data.count : null,
  };
}

/**
 * The host's name, for breadcrumbs.
 *
 * A breadcrumb should say `demo-host`, not a 32-character machine id — the id is the
 * identity, but the name is what an operator calls it. Falls back to the id when the host
 * has no readable hostname, and to `host` while the read is in flight.
 */
export function useHostName(): string {
  const { resource } = useResource(
    'host',
    useCallback((signal) => endpoints.host({ signal }), []),
  );
  if (resource.status !== 'success') return 'host';
  return resource.data.hostname ?? resource.data.host_id;
}
