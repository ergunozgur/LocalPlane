/**
 * One container, in the shared object workspace.
 *
 * `docker.container.lifecycle` is genuinely executable on this host — unlike systemd's, the
 * backend has an executor for it. This build still renders no start/stop/restart control,
 * because a correct one is the whole plan → confirm → apply → verify vertical and a button
 * without it would be exactly the fake success the product forbids.
 *
 * The Lifecycle tab exists anyway, and that is a rule of this interface rather than an
 * oversight: *a tunnel owned by another daemon still gets a Configure tab, because "there
 * is nothing to set, and here is why" is an answer worth showing*. A missing tab would
 * read as "Docker cannot do this", which is false.
 *
 * Two reads are deliberately kept off the Overview tab and off the list: the on-demand
 * resource sample and the log tail. Both are per-container calls, and both happen only when
 * an operator opens the tab that needs them.
 */
import { useCallback } from 'react';
import { useParams } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { DataTable } from '@/components/primitives/DataTable';
import { Tag } from '@/components/primitives/Chip';
import { StatusPill } from '@/components/semantic/StatusPill';
import { ManagementChip } from '@/components/semantic/SemanticGlyph';
import { Value } from '@/components/semantic/UnknownValue';
import { NotAssessed, Empty } from '@/components/states/SurfaceState';
import { ResourceView } from '@/components/states/ResourceView';
import { ObjectWorkspace, ObjectColumns, type ObjectTab } from '@/components/object/ObjectWorkspace';
import { ScopeBar } from '@/components/layout/ScopeBar';
import { ResourceUsage } from '@/components/semantic/ResourceUsage';
import { ContainerLogPanel } from '@/components/semantic/ContainerLogPanel';
import { Disclosure } from '@/components/semantic/Evidence';
import {
  containerGroupOf,
  LABEL_CONFIG_FILES,
  LABEL_CONFIG_HASH,
  LABEL_CONTAINER_NUMBER,
  LABEL_PROJECT,
  LABEL_SERVICE,
  LABEL_WORKING_DIR,
} from '@/domain/workloads';
import {
  humanise,
  containerHealth,
  containerState,
  freshness as freshnessOf,
  health as healthOf,
  ownershipState,
} from '@/domain/vocabulary';
import { formatTimestamp } from '@/domain/format';
import styles from './WorkloadDetail.module.css';

export function WorkloadDetail(): JSX.Element {
  const { objectId = '' } = useParams();
  const { resource } = useResource(
    `container:${objectId}`,
    useCallback((signal) => endpoints.container(objectId, { signal }), [objectId]),
  );

  return (
    <ResourceView resource={resource} what="container" loadingLabel="Reading container…">
      {(data, meta) => {
        const group = containerGroupOf(data);

        const tabs: ObjectTab[] = [
          {
            id: 'overview',
            label: 'Overview',
            render: () => (
              <ObjectColumns
                main={
                  <>
                    <Plate>
                      <PlateHead title="Runtime" level={3} meta="what the daemon reports now" />
                      <PlateBody>
                        <KeyValueList columns="auto">
                          <KeyValue label="State"><Value value={data.runtime.state} mono /></KeyValue>
                          <KeyValue label="PID"><Value value={data.runtime.pid} mono /></KeyValue>
                          <KeyValue label="Exit code">
                            <Value value={data.runtime.exit_code} mono reason="the container has not exited" />
                          </KeyValue>
                          <KeyValue label="Started"><Value value={formatTimestamp(data.runtime.started_at)} /></KeyValue>
                          <KeyValue label="Finished"><Value value={formatTimestamp(data.runtime.finished_at)} /></KeyValue>
                          <KeyValue label="Restart count"><Value value={data.runtime.restart_count} mono /></KeyValue>
                          <KeyValue label="OOM killed">
                            <Value
                              value={data.runtime.oom_killed === null ? null : data.runtime.oom_killed ? 'yes' : 'no'}
                              mono
                            />
                          </KeyValue>
                          <KeyValue label="Restart policy">
                            <Value value={data.restart_policy.name} mono />
                          </KeyValue>
                          <KeyValue label="Error"><Value value={data.runtime.error} mono /></KeyValue>
                          <KeyValue label="Log driver"><Value value={data.log_driver} mono /></KeyValue>
                        </KeyValueList>
                      </PlateBody>
                    </Plate>

                    <Plate>
                      <PlateHead title="Identity" level={3} meta="what this container is, and who says so" />
                      <PlateBody>
                        <KeyValueList columns="auto">
                          <KeyValue label="Container id">
                            <Tag title={data.container_id}>{data.short_id}</Tag>
                          </KeyValue>
                          <KeyValue label="Object id">
                            <Tag title={data.object_id}>{data.object_id}</Tag>
                          </KeyValue>
                          <KeyValue label="Image"><Value value={data.image.reference} mono /></KeyValue>
                          <KeyValue label="Image digest"><Value value={data.image.image_id} mono /></KeyValue>
                          <KeyValue label="Platform"><Value value={data.platform} mono /></KeyValue>
                          <KeyValue label="Created"><Value value={formatTimestamp(data.created_at)} /></KeyValue>
                          <KeyValue label="Ownership" hint={humanise(data.ownership.reason)}>
                            {/* The reason is a long typed code; it belongs beside the pill rather
                                than inside it, where it was being ellipsised into nonsense. */}
                            <StatusPill semantic={ownershipState(data.ownership.state)} size="sm" />
                          </KeyValue>
                        </KeyValueList>
                      </PlateBody>
                    </Plate>
                  </>
                }
                side={<ObservationPlate data={data} />}
              />
            ),
          },
          {
            id: 'lifecycle',
            label: 'Lifecycle',
            render: () => (
              <ObjectColumns
                main={
                  <Plate>
                    <PlateHead title="Lifecycle" level={3} meta="start, stop and restart" />
                    <PlateBody>
                      <NotAssessed title="No lifecycle control is offered in this console">
                        LocalPlane can start, stop and restart containers through Docker&apos;s
                        own API — a real capability, as systemd service lifecycle is now too.
                        What this frontend does not yet have is the plan → confirm → apply →
                        verify path that a lifecycle action must go through, and a control
                        without it would be a button that claims an outcome it cannot prove. It
                        is deferred deliberately, not missing by oversight.
                      </NotAssessed>
                    </PlateBody>
                  </Plate>
                }
              />
            ),
          },
          {
            id: 'declaration',
            label: group.origin === 'compose' ? 'Declaration' : 'Labels',
            render: () => (
              <ObjectColumns
                main={
                  <Plate>
                    <PlateHead
                      title={group.origin === 'compose' ? 'Project' : 'Declaration'}
                      level={3}
                      meta={
                        group.origin === 'compose'
                          ? 'what declared this container'
                          : 'nothing declares this container'
                      }
                    />
                    <PlateBody>
                      {group.origin === 'compose' ? (
                        <KeyValueList>
                          <KeyValue label="Project">
                            <Value value={data.labels[LABEL_PROJECT]} mono />
                          </KeyValue>
                          <KeyValue label="Service">
                            <Value value={data.labels[LABEL_SERVICE]} mono />
                          </KeyValue>
                          <KeyValue label="Replica">
                            <Value value={data.labels[LABEL_CONTAINER_NUMBER]} mono />
                          </KeyValue>
                          <KeyValue label="File">
                            <Value value={data.labels[LABEL_CONFIG_FILES]} mono />
                          </KeyValue>
                          <KeyValue label="Working dir">
                            <Value value={data.labels[LABEL_WORKING_DIR]} mono />
                          </KeyValue>
                          <KeyValue label="Config hash" hint="the declaration applied">
                            <span className={styles.hash}>
                              <Value value={data.labels[LABEL_CONFIG_HASH]} mono />
                            </span>
                          </KeyValue>
                        </KeyValueList>
                      ) : (
                        <Empty
                          title="Not declared"
                          explanation="This container carries no compose labels, so LocalPlane has no declaration to compare it against. It is observed, not managed."
                        />
                      )}

                      {/* The compose file itself is not readable from here, so there is no
                          Observed ‖ Intended comparison to draw: LocalPlane holds the hash of
                          a declaration, not its contents. Rendering the comparator with an
                          empty right-hand column would state a comparison nobody made. */}
                      <p className={styles.labelNote}>
                        {group.origin === 'compose'
                          ? 'LocalPlane records which declaration produced this container, not what that declaration said. There is no field-by-field comparison to show, because the file was never read.'
                          : 'With nothing declared, nothing can disagree — this container has a health, but no reconciliation state.'}
                      </p>

                      <Disclosure summary="All kept labels" count={Object.keys(data.labels).length}>
                        <p className={styles.labelNote}>
                          The backend keeps a deliberate allowlist — compose metadata, OCI image
                          metadata and a few named keys.
                          {data.labels_dropped > 0
                            ? ` ${data.labels_dropped} other label${data.labels_dropped === 1 ? ' was' : 's were'} dropped before this record was written.`
                            : ''}
                        </p>
                        <KeyValueList>
                          {Object.entries(data.labels).map(([key, value]) => (
                            <KeyValue key={key} label={<span className="mono">{key}</span>}>
                              <span className="mono">{value}</span>
                            </KeyValue>
                          ))}
                        </KeyValueList>
                      </Disclosure>
                    </PlateBody>
                  </Plate>
                }
              />
            ),
          },
          {
            id: 'networking',
            label: 'Networking',
            count: data.networks.length,
            render: () => (
              <ObjectColumns
                main={
                  <>
                    <Plate>
                      <PlateHead title="Networks" level={3} meta="what this container is attached to" />
                      <PlateBody>
                        <KeyValueList>
                          <KeyValue label="Network mode">
                            <Value value={data.network_mode} mono />
                          </KeyValue>
                        </KeyValueList>
                        {data.networks.length === 0 ? (
                          <Empty title="No networks" explanation="This container is attached to none." />
                        ) : (
                          <KeyValueList>
                            {data.networks.map((network) => (
                              <KeyValue key={network.name} label={<span className="mono">{network.name}</span>}>
                                <Value value={network.ip_address} mono reason="no address was reported" />
                              </KeyValue>
                            ))}
                          </KeyValueList>
                        )}
                      </PlateBody>
                    </Plate>

                    <Plate>
                      <PlateHead title="Ports" level={3} />
                      <DataTable
                        caption="Published and exposed ports"
                        rows={data.ports}
                        rowKey={(row) => `${row.container_port}-${row.protocol}-${row.host_port ?? 'x'}`}
                        emptyState={<Empty title="No ports" explanation="This container exposes none." />}
                        columns={[
                          {
                            key: 'container',
                            header: 'Container',
                            render: (row) => (
                              <span className="mono">{row.container_port}/{row.protocol}</span>
                            ),
                          },
                          {
                            key: 'host',
                            header: 'Host',
                            render: (row) =>
                              row.published ? (
                                <span className="mono">
                                  {row.host_ip ?? '*'}:{row.host_port}
                                </span>
                              ) : (
                                <Value value={null} reason="exposed but not published" />
                              ),
                          },
                        ]}
                      />
                    </Plate>
                  </>
                }
              />
            ),
          },
          {
            id: 'mounts',
            label: 'Mounts',
            count: data.mounts.length,
            render: () => (
              <ObjectColumns
                main={
                  <Plate>
                    <PlateHead title="Mounts" level={3} meta="what this container can see of the host" />
                    <DataTable
                      caption="Mounts"
                      rows={data.mounts}
                      rowKey={(row) => `${row.type}-${row.destination}`}
                      emptyState={<Empty title="No mounts" explanation="This container mounts nothing." />}
                      columns={[
                        { key: 'type', header: 'Type', render: (row) => <Value value={row.type} mono /> },
                        { key: 'source', header: 'Source', render: (row) => <Value value={row.source ?? row.name} mono /> },
                        { key: 'destination', header: 'Destination', render: (row) => <Value value={row.destination} mono /> },
                        {
                          key: 'rw',
                          header: 'Mode',
                          render: (row) => (
                            <Value value={row.read_write === null ? null : row.read_write ? 'rw' : 'ro'} mono />
                          ),
                        },
                      ]}
                    />
                  </Plate>
                }
              />
            ),
          },
          {
            id: 'resources',
            label: 'Resources',
            render: () => (
              <ObjectColumns
                main={
                  <Plate>
                    <PlateHead title="Resource usage" level={3} meta="one sample, taken on demand" />
                    <PlateBody>
                      <ResourceUsagePanel objectId={data.object_id} />
                    </PlateBody>
                  </Plate>
                }
              />
            ),
          },
          {
            id: 'logs',
            label: 'Logs',
            render: () => (
              <ObjectColumns
                main={
                  <Plate>
                    <PlateHead title="Logs" level={3} meta="what this container has written" />
                    <PlateBody>
                      <ContainerLogPanel objectId={data.object_id} />
                    </PlateBody>
                  </Plate>
                }
              />
            ),
          },
        ];

        return (
          <>
            <ScopeBar
              crumbs={[
                { label: data.identity.value.slice(0, 12), to: '/' },
                { label: 'workloads', to: '/workloads' },
                { label: group.name, mono: true },
                // A one-service project names its service the same as itself. Saying it
                // twice looks like a bug rather than like a hierarchy.
                ...(group.service && group.service !== group.name
                  ? [{ label: group.service, mono: true }]
                  : []),
                { label: data.short_id, mono: true },
              ]}
              observedAt={meta.fetchedAt}
            />

            <ObjectWorkspace
              objectId={data.object_id}
              name={data.name}
              kind="container"
              mark={healthOf(data.health?.state)}
              observedAt={meta.fetchedAt}
              headline={
                group.origin === 'compose'
                  ? `Compose labels place this container in project ${group.name}${group.service ? ` as service ${group.service}` : ''}. This is provider grouping evidence, not LocalPlane Application identity.`
                  : 'A standalone container. Nothing declares it to LocalPlane, so it is observed only.'
              }
              path={[
                { label: 'workloads', to: '/workloads' },
                ...(group.origin === 'compose'
                  ? [{ label: group.name, to: `/workloads?group=${encodeURIComponent(group.name)}` }]
                  : []),
                ...(group.service && group.service !== group.name
                  ? [{ label: group.service }]
                  : []),
                { prefix: 'container', label: data.short_id },
              ]}
              chips={
                <>
                  <StatusPill semantic={containerState(data.runtime.state)} size="sm" token={data.runtime.state} />
                  <StatusPill semantic={healthOf(data.health?.state)} size="sm" token="object health" />
                  <StatusPill
                    semantic={containerHealth(data.container_health.status)}
                    size="sm"
                    token="health check"
                  />
                  <ManagementChip state={data.management.state} />
                </>
              }
              contextFact={<span className="mono">{data.image.reference}</span>}
              tabs={tabs}
            />
          </>
        );
      }}
    </ResourceView>
  );
}

/** Observation is context for whatever tab is open, so it sits beside the body. */
function ObservationPlate({ data }: { data: import('@/api/types').DockerContainer }): JSX.Element {
  return (
    <Plate>
      <PlateHead title="Observation" level={3} meta="who read this, and when" />
      <PlateBody>
        {data.observation ? (
          <KeyValueList>
            <KeyValue label="Freshness">
              <StatusPill semantic={freshnessOf(data.observation.freshness)} size="sm" />
            </KeyValue>
            <KeyValue label="Provider" hint={data.observation.provider_version}>
              <Value value={data.observation.provider} mono />
            </KeyValue>
            <KeyValue label="Observed at">
              <Value value={formatTimestamp(data.observation.observed_at)} />
            </KeyValue>
          </KeyValueList>
        ) : (
          <Empty title="Never observed" explanation="Nothing has read this container." />
        )}
      </PlateBody>
    </Plate>
  );
}

/**
 * Resource usage, fetched only when this tab is open.
 *
 * Deliberately not a list-row concern: one stats call per container would turn the Workloads
 * list into N requests against a backend with a known concurrency defect.
 */
function ResourceUsagePanel({ objectId }: { objectId: string }): JSX.Element {
  const { resource, refresh } = useResource(
    `container-stats:${objectId}`,
    useCallback((signal) => endpoints.containerStats(objectId, { signal }), [objectId]),
  );

  return (
    <ResourceView resource={resource} what="resource sample" loadingLabel="Sampling…">
      {(stats) => (
        <>
          <ResourceUsage stats={stats} />
          <button type="button" className={styles.resample} onClick={refresh}>
            Sample again
          </button>
        </>
      )}
    </ResourceView>
  );
}
