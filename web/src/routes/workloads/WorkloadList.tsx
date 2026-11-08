/**
 * Workloads — Docker containers, grouped only where provider labels support it.
 *
 * Durable Application identity does not exist in this build. Compose labels support a useful
 * provider-derived project grouping, while an unlabelled container remains standalone.
 */
import { useMemo } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import type { Sweep } from '@/api/types';
import { useHostName } from '@/hooks/useEstateCounts';
import { Plate, PlateBody, PlateFoot, PlateHead, StatusDot } from '@/components/primitives/Plate';
import { StatusPill } from '@/components/semantic/StatusPill';
import { Value } from '@/components/semantic/UnknownValue';
import { ResourceView } from '@/components/states/ResourceView';
import { Degraded, Empty } from '@/components/states/SurfaceState';
import { PageHeader } from '../PageHeader';
import { ScopeBar } from '@/components/layout/ScopeBar';
import { IssueList, SweepCaveat } from '@/dashboard/widgets/shared';
import {
  groupContainers,
  inventoryMembership,
  containerMembershipCounts,
  type ContainerGroup,
} from '@/domain/workloads';
import {
  containerHealth,
  containerState,
  freshness as freshnessOf,
} from '@/domain/vocabulary';
import { formatRelative, formatTimestamp } from '@/domain/format';
import styles from './WorkloadList.module.css';
import { type InventoryOutcome, useDockerInventory } from './useDockerInventory';

function effectiveGroup(params: URLSearchParams): string | null {
  const raw = params.get('group');
  return raw ? raw : null;
}

export function WorkloadList(): JSX.Element {
  const { resource, outcome, storedReadError, refreshInventory, retryStoredRead } = useDockerInventory();
  const hostName = useHostName();
  const [searchParams] = useSearchParams();
  const groupFilter = effectiveGroup(searchParams);

  const clearSearch = useMemo(() => {
    const next = new URLSearchParams(searchParams);
    next.delete('group');
    return { pathname: '/workloads', search: next.toString() };
  }, [searchParams]);

  const refreshPending = outcome?.phase === 'observing' || outcome?.phase === 'reading';

  return (
    <ResourceView resource={resource} what="container list" loadingLabel="Reading containers…">
      {(list, meta) => {
        const groups = groupContainers(list.containers);
        const activeGroups = groupFilter
          ? groups.filter((g) => g.origin === 'compose' && g.name === groupFilter)
          : groups;
        const activeContainers = activeGroups.flatMap((g) => g.containers);
        const membership = containerMembershipCounts(activeContainers);

        return (
          <>
            <ScopeBar
              crumbs={[{ label: hostName, to: '/' }, { label: 'workloads' }]}
              tabs={[
                {
                  to: '/workloads',
                  label: 'Container groups',
                  count: groups.length,
                  end: true,
                  ...(groupFilter ? ({ current: true } as const) : {}),
                },
                { to: '/workloads/runtime', label: 'Runtime' },
              ]}
              observedAt={meta.fetchedAt}
            />

            <PageHeader
              title="Workloads"
              count={list.count}
              annotation="Stored Docker records stay visible until an explicit inventory observation and a successful stored-record reread establish what this view can show. Compose labels support project grouping, but these groups are provider-derived views—not canonical LocalPlane Applications."
            >
              <button
                type="button"
                className="btn"
                onClick={refreshInventory}
                disabled={refreshPending}
              >
                {refreshPending ? 'Refreshing Docker inventory…' : 'Refresh Docker inventory'}
              </button>
            </PageHeader>

            <InventoryOutcomeView
              outcome={outcome}
              onRefresh={refreshInventory}
              onRetryStoredRead={retryStoredRead}
            />
            {storedReadError && outcome?.phase !== 'reread_failed' ? (
              <StoredReadFailure error={storedReadError} onRetry={retryStoredRead} />
            ) : null}

            <Plate quiet>
              <PlateHead
                title="Container groups"
                meta={`${list.count} stored container record${list.count === 1 ? '' : 's'} · Compose-label projects and standalone containers`}
                asOf={meta.fetchedAt.toLocaleTimeString()}
                chips={
                  <>
                    <span className={styles.countChip}>
                      <b>{membership.reportedRunningLatestSighted}</b>
                      <span> reported running among {membership.latestSighted} latest-sighted</span>
                    </span>
                    <span className={styles.countChip}>
                      <b>{activeGroups.length}</b>
                      <span>&nbsp;stored group view{activeGroups.length === 1 ? '' : 's'}</span>
                    </span>
                    {membership.retainedNotSeen > 0 ? <span className={styles.countChip}><b>{membership.retainedNotSeen}</b><span>&nbsp;retained, not seen</span></span> : null}
                    {membership.membershipUnknown > 0 ? <span className={styles.countChip}><b>{membership.membershipUnknown}</b><span>&nbsp;membership unknown</span></span> : null}
                  </>
                }
              />

              <PlateBody tight>
                <StoredInventoryContext sweep={list.last_sweep} fetchedAt={meta.fetchedAt} />
              </PlateBody>

              {groupFilter ? (
                <PlateBody tight>
                  <div className={styles.filterContext}>
                    <span className={styles.filterLabel}>
                      {activeGroups.length > 0 ? (
                        <>
                          Showing Compose project <strong className="mono">{groupFilter}</strong>
                        </>
                      ) : (
                        <>
                          No Compose project named <strong className="mono">{groupFilter}</strong>
                        </>
                      )}
                    </span>
                    <span className={styles.resultCount}>
                      {activeGroups.length} of {groups.length} group{groups.length === 1 ? '' : 's'}
                      {' · '}
                      {activeContainers.length} of {list.count} container
                      {list.count === 1 ? '' : 's'}
                    </span>
                    <Link to={clearSearch} className={styles.clearFilter}>
                      Show all groups
                    </Link>
                  </div>
                </PlateBody>
              ) : null}

              {SweepCaveat({ sweep: list.last_sweep }) ? (
                <PlateBody tight>
                  <SweepCaveat sweep={list.last_sweep} />
                </PlateBody>
              ) : null}

              {activeGroups.length === 0 ? (
                <Empty
                  title={groupFilter ? 'No group matches' : 'No workloads'}
                  explanation={
                    groupFilter
                      ? 'No observed Compose project matches this filter.'
                        : list.last_sweep?.status === 'ok' && list.last_sweep.issues.length === 0
                          ? 'A complete stored Docker inventory recorded no containers. This does not describe a live daemon read.'
                          : list.last_sweep?.status === 'failed'
                            ? 'The most recent Docker inventory failed, so an empty stored list does not establish that no containers exist.'
                            : list.last_sweep
                              ? 'The most recent Docker inventory was incomplete or has an unrecognised status, so an empty stored list does not establish that no containers exist.'
                              : 'No sweep has recorded containers, so this is not evidence that there are none.'
                  }
                />
              ) : (
                <div className={styles.applications}>
                  {activeGroups.map((group) => (
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
            to={`/workloads/${encodeURIComponent(container.object_id)}`}
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
            <StatusPill semantic={inventoryMembership(container.observed_in_latest_sweep)} size="sm" />
            <span className={styles.open}>open ›</span>
          </Link>
        ))}
      </div>
    </section>
  );
}

function InventoryOutcomeView({
  outcome,
  onRefresh,
  onRetryStoredRead,
}: {
  outcome: InventoryOutcome | null;
  onRefresh: () => void;
  onRetryStoredRead: () => void;
}): JSX.Element | null {
  if (!outcome) return null;

  if (outcome.phase === 'observing') {
    return <div id="docker-inventory-status" className={styles.outcome} role="status" aria-live="polite"><Degraded title="Refreshing Docker inventory">The prior stored records remain visible while LocalPlane asks Docker for one inventory observation.</Degraded></div>;
  }
  if (outcome.phase === 'reading') {
    return <div id="docker-inventory-status" className={styles.outcome} role="status" aria-live="polite"><Degraded title="Reading stored container records">Docker returned an inventory response. LocalPlane is re-reading its stored records before presenting the visible list as updated.</Degraded></div>;
  }
  if (outcome.phase === 'observation_failed') {
    return (
      <div id="docker-inventory-status" className={styles.outcome} role="alert">
        <Degraded tone="warn" title="Docker inventory request failed">
          The backend returned no completed inventory result. The visible stored records were not replaced.{' '}
          <ErrorFact error={outcome.error} />{' '}
          <button type="button" className="btn sm" onClick={onRefresh}>Retry inventory</button>
        </Degraded>
      </div>
    );
  }
  if (outcome.phase === 'observation_uncertain') {
    return (
      <div id="docker-inventory-status" className={styles.outcome} role="alert">
        <Degraded tone="unknown" title="Docker inventory outcome is uncertain">
          LocalPlane did not receive a readable inventory result, so Docker may or may not have been observed. A stored-record re-read may show different retained evidence, but cannot acknowledge this response.{' '}
          <ErrorFact error={outcome.error} />{' '}
          <button type="button" className="btn sm" onClick={onRefresh}>Retry inventory</button>
        </Degraded>
      </div>
    );
  }
  if (outcome.phase === 'reread_failed') {
    return (
      <div id="docker-inventory-status" className={styles.outcome} role="alert">
        <Degraded tone="unknown" title="Docker responded, but stored records could not be re-read">
          The prior stored records remain visible. Re-read stored records does not contact Docker or repeat the inventory observation.{' '}
          <AcknowledgedResult result={outcome.result} /> <ErrorFact error={outcome.error} />{' '}
          <button type="button" className="btn sm" onClick={onRetryStoredRead}>Re-read stored records</button>
          <IssueList issues={outcome.result.issues} label="Reported issues" />
        </Degraded>
      </div>
    );
  }
  if (outcome.phase !== 'complete' && outcome.phase !== 'partial' && outcome.phase !== 'unknown') {
    return null;
  }

  const { result, list } = outcome;
  const latest = list.last_sweep;
  const matchesReturnedSweep = latest?.sweep_id === result.sweep_id;
  const providerFailed = result.status === 'failed';
  const title = outcome.phase === 'complete'
    ? 'Docker inventory completed'
    : outcome.phase === 'partial'
      ? 'Docker inventory completed with gaps'
      : providerFailed
        ? 'Docker inventory reported provider failure'
        : `Docker inventory returned unrecognised status: ${result.status}`;
  return (
    <div id="docker-inventory-status" className={styles.outcome} role="status" aria-live="polite">
      <Degraded tone={outcome.phase === 'complete' ? 'unknown' : providerFailed ? 'bad' : 'warn'} title={title}>
        <AcknowledgedResult result={result} />
        {matchesReturnedSweep
          ? <> Stored records were re-read for this sweep{latest?.completed_at ? ` at ${formatTimestamp(latest.completed_at)}` : ''}.</>
          : <> Stored records were re-read, but their latest sweep differs from this returned response; the list is not claimed to represent the returned sweep.</>}
        <IssueList issues={result.issues} label="Reported issues" />
      </Degraded>
    </div>
  );
}

function StoredReadFailure({ error, onRetry }: { error: import('@/api/client').ApiError; onRetry: () => void }): JSX.Element {
  return (
    <div className={styles.outcome} role="alert">
      <Degraded tone="unknown" title="Stored container records could not be re-read">
        The last visible records remain on screen. Re-read stored records does not contact Docker.{' '}
        <ErrorFact error={error} /> <button type="button" className="btn sm" onClick={onRetry}>Re-read stored records</button>
      </Degraded>
    </div>
  );
}

function AcknowledgedResult({ result }: { result: import('@/api/types').ContainerObservationResult }): JSX.Element {
  return <>
    Docker returned status <code>{result.status}</code> · {result.container_count} container{result.container_count === 1 ? '' : 's'} · provider version <code>{result.provider_version}</code> · sweep <code>{result.sweep_id}</code>.
    {result.issues.length > 0 ? ` ${result.issues.length} issue${result.issues.length === 1 ? '' : 's'} reported.` : ''}
  </>;
}

function ErrorFact({ error }: { error: import('@/api/client').ApiError }): JSX.Element {
  const reason = typeof error.detail.reason === 'string' && error.detail.reason !== error.message
    ? ` Reason: ${error.detail.reason}`
    : '';
  return <span className={styles.errorFact}><code>{error.code ?? error.kind}</code>: {error.message}{reason}</span>;
}

function StoredInventoryContext({ sweep, fetchedAt }: { sweep: Sweep | null; fetchedAt: Date }): JSX.Element {
  if (!sweep) {
    return <div className={styles.observationContext}>
      <strong>No Docker inventory is recorded.</strong> Stored records were fetched at {fetchedAt.toLocaleString()}. No-sweep membership is unknown; an empty list does not show that Docker has no containers.
    </div>;
  }
  return <div className={styles.observationContext}>
    <span><strong>Stored Docker inventory</strong> completed {formatTimestamp(sweep.completed_at)} · status <code>{sweep.status}</code> · {sweep.provider} {sweep.provider_version} · {sweep.object_count} recorded.</span>
    <span>Stored records fetched at {fetchedAt.toLocaleString()}. {sweep.missing.length > 0 ? `${sweep.missing.length} provider-reported absent record${sweep.missing.length === 1 ? '' : 's'}. ` : ''}Not seen in latest inventory can include an unreadable inspection; it does not prove deletion.</span>
    <IssueList issues={sweep.issues} label="Inventory issues" />
  </div>;
}
