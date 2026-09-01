/**
 * Workloads — Docker containers, grouped only where provider labels support it.
 *
 * Durable Application identity does not exist in this build. Compose labels support a useful
 * provider-derived project grouping, while an unlabelled container remains standalone.
 */
import { useCallback } from 'react';
import { Link } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { useEstateCounts, useHostName } from '@/hooks/useEstateCounts';
import { Plate, PlateBody, PlateFoot, PlateHead, StatusDot } from '@/components/primitives/Plate';
import { StatusPill } from '@/components/semantic/StatusPill';
import { Value } from '@/components/semantic/UnknownValue';
import { ResourceView } from '@/components/states/ResourceView';
import { Empty } from '@/components/states/SurfaceState';
import { PageHeader } from '../PageHeader';
import { ScopeBar } from '@/components/layout/ScopeBar';
import { SweepCaveat } from '@/dashboard/widgets/shared';
import { groupContainers, type ContainerGroup } from '@/domain/workloads';
import {
  containerHealth,
  containerState,
  freshness as freshnessOf,
} from '@/domain/vocabulary';
import { formatRelative } from '@/domain/format';
import styles from './WorkloadList.module.css';

export function WorkloadList(): JSX.Element {
  const { resource } = useResource(
    'containers',
    useCallback((signal) => endpoints.containers({ signal }), []),
  );
  const counts = useEstateCounts();
  const hostName = useHostName();

  return (
    <ResourceView resource={resource} what="container list" loadingLabel="Reading containers…">
      {(list, meta) => {
        const groups = groupContainers(list.containers);
        const running = list.containers.filter((c) => c.runtime.state === 'running').length;

        return (
          <>
            <ScopeBar
              crumbs={[{ label: hostName, to: '/' }, { label: 'workloads' }]}
              tabs={[
                { to: '/workloads', label: 'Container groups', count: counts.containerGroups, end: true },
                { to: '/workloads/runtime', label: 'Runtime' },
              ]}
              observedAt={meta.fetchedAt}
            />

            <PageHeader
              title="Workloads"
              count={list.count}
              annotation="Docker reports the containers in this view. Compose labels support project grouping, but these groups are provider-derived views—not canonical LocalPlane Applications. LocalPlane observes them and does not become answerable for them by watching."
            />

            <Plate quiet>
              <PlateHead
                title="Container groups"
                meta="Compose-label projects and standalone containers observed through Docker"
                mark={containerState(running > 0 ? 'running' : 'exited')}
                asOf={meta.fetchedAt.toLocaleTimeString()}
                chips={
                  <>
                    <span className={styles.countChip}>
                      <b>{running}</b>
                      <span>/{list.count} running</span>
                    </span>
                    <span className={styles.countChip}>
                      <b>{groups.length}</b>
                      <span>&nbsp;group{groups.length === 1 ? '' : 's'}</span>
                    </span>
                  </>
                }
              />

              {SweepCaveat({ sweep: list.last_sweep }) ? (
                <PlateBody tight>
                  <SweepCaveat sweep={list.last_sweep} />
                </PlateBody>
              ) : null}

              {groups.length === 0 ? (
                <Empty
                  title="No workloads"
                  explanation={
                    list.last_sweep
                      ? 'The Docker daemon answered and reported no containers.'
                      : 'No sweep has recorded containers, so this is not evidence that there are none.'
                  }
                />
              ) : (
                <div className={styles.applications}>
                  {groups.map((group) => (
                    <ContainerGroupRows key={group.id} group={group} />
                  ))}
                </div>
              )}

              <PlateFoot
                source={
                  <>
                    {list.last_sweep
                      ? `${list.last_sweep.provider} ${list.last_sweep.provider_version} · ${list.last_sweep.capability}`
                      : 'no sweep'}{' '}
                    · compose labels
                  </>
                }
              >
                <span className={styles.note}>
                  a workload is a thing being run; the runtime is only how it is being run
                </span>
              </PlateFoot>
            </Plate>
          </>
        );
      }}
    </ResourceView>
  );
}

function ContainerGroupRows({ group }: { group: ContainerGroup }): JSX.Element {
  const attention = group.containers.some(
    (c) => c.container_health.status === 'unhealthy' || c.runtime.state === 'dead',
  );

  return (
    <section className={styles.application} data-attention={attention ? 'true' : undefined}>
      <header className={styles.applicationHead}>
        <span className={styles.applicationName}>{group.name}</span>
        <span className={styles.kind}>
          {group.origin === 'compose' ? 'Compose project' : 'standalone container'}
        </span>
        <span className={styles.applicationMeta}>
          {group.origin === 'compose'
            ? `${group.containers.length} container${group.containers.length === 1 ? '' : 's'} · grouped from provider labels`
            : 'no Compose project label · observed only'}
        </span>
        <span className={styles.spacer} />
      </header>

      <div className={styles.containers}>
        {group.containers.map((container) => (
          <Link
            key={container.object_id}
            to={`/workloads/${container.object_id}`}
            className={styles.container}
          >
            <StatusDot semantic={containerState(container.runtime.state)} />
            <span className={styles.containerName}>{container.name}</span>
            {container.ports.filter((p) => p.published && p.host_port !== null).length > 0 ? (
              <span className={styles.ports}>
                {container.ports
                  .filter((p) => p.published && p.host_port !== null)
                  .slice(0, 2)
                  .map((p) => `:${p.host_port}`)
                  .join(' ')}
              </span>
            ) : null}
            <span className={styles.kind}>container</span>
            <span className={styles.containerMeta}>
              <Value value={container.runtime.state} mono />
              {' · '}
              <Value value={container.image.reference} mono />
              {container.runtime.started_at ? ` · up ${formatRelative(container.runtime.started_at)?.replace(' ago', '')}` : ''}
            </span>
            <span className={styles.spacer} />
            <StatusPill semantic={containerHealth(container.container_health.status)} size="sm" />
            <StatusPill
              semantic={freshnessOf(container.observation?.freshness)}
              size="sm"
            />
            <span className={styles.open}>open ›</span>
          </Link>
        ))}
      </div>
    </section>
  );
}
