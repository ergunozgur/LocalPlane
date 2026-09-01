/**
 * Runtime — the engine behind these workloads, the images they run from, and which runtimes
 * are actually observed.
 *
 * The design direction puts Engine, Images and Runtimes beside the workload list. They are
 * kept here as their own surface rather than crowding the list, and each states plainly
 * what this build can and cannot see:
 *
 *  - **Engine**: the agent reads containers and networks. It does not call
 *    `docker system info`, so storage driver, cgroup version, image and volume counts and
 *    live-restore have no contract. They are named as unavailable rather than omitted,
 *    because the shape of what is missing is itself useful.
 *  - **Images**: derived from the containers, so this is honestly *images in use* — an image
 *    pulled but not running cannot appear, and no size is reported by anything.
 *  - **Runtimes**: compose-backed and standalone Docker are distinguishable from evidence
 *    already published. The rest carry the Engine plate's point — that Workloads is the
 *    domain and Docker is only what is present today — as explicitly undetectable, not as
 *    absent.
 */
import { useCallback } from 'react';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { useEstateCounts, useHostName } from '@/hooks/useEstateCounts';
import { Plate, PlateBody, PlateFoot, PlateHead } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { DataTable } from '@/components/primitives/DataTable';
import { Value } from '@/components/semantic/UnknownValue';
import { ResourceView } from '@/components/states/ResourceView';
import { Empty } from '@/components/states/SurfaceState';
import { PageHeader } from '../PageHeader';
import { ScopeBar } from '@/components/layout/ScopeBar';
import {
  engineFacts,
  groupContainers,
  imagesInUse,
  runtimeRows,
  type RuntimeRow,
} from '@/domain/workloads';
import { formatRelative } from '@/domain/format';
import styles from './WorkloadRuntime.module.css';

/** A fact the design shows that no contract in this build supplies. */
function Unavailable({ why }: { why: string }): JSX.Element {
  return (
    <span className={styles.unavailable} title={why}>
      not observed by this build
    </span>
  );
}

export function WorkloadRuntime(): JSX.Element {
  const { resource } = useResource(
    'containers',
    useCallback((signal) => endpoints.containers({ signal }), []),
  );
  const counts = useEstateCounts();
  const hostName = useHostName();

  return (
    <ResourceView resource={resource} what="container list" loadingLabel="Reading runtime…">
      {(list, meta) => {
        const groups = groupContainers(list.containers);
        const engine = engineFacts(list.last_sweep, list.containers);
        const images = imagesInUse(list.containers);
        const rows = runtimeRows(groups);

        return (
          <>
            <ScopeBar
              crumbs={[{ label: hostName, to: '/' }, { label: 'workloads', to: '/workloads' }, { label: 'runtime' }]}
              tabs={[
                { to: '/workloads', label: 'Container groups', count: counts.containerGroups, end: true },
                { to: '/workloads/runtime', label: 'Runtime' },
              ]}
              observedAt={meta.fetchedAt}
            />

            <PageHeader
              title="Runtime"
              annotation="Current runtime evidence comes from Docker containers. Docker is the only runtime this build observes; unsupported detectors do not establish that other runtimes are absent."
            />

            <div className={styles.columns}>
              <div className={styles.column}>
                <Plate quiet>
                  <PlateHead
                    title="Engine"
                    meta="the container runtime this build currently observes"
                    asOf={meta.fetchedAt.toLocaleTimeString()}
                  />
                  <PlateBody>
                    <KeyValueList>
                      <KeyValue label="Provider">
                        <Value value={engine.provider} mono />
                      </KeyValue>
                      <KeyValue label="Version">
                        <Value value={engine.version} mono />
                      </KeyValue>
                      <KeyValue label="Containers">
                        {engine.containersTotal === null ? (
                          <Value value={null} reason="the container list could not be read" />
                        ) : (
                          <span className="mono">
                            {engine.containersTotal}{' '}
                            <span className={styles.sub}>
                              {engine.containersRunning} running ·{' '}
                              {engine.containersTotal - (engine.containersRunning ?? 0)} not running
                            </span>
                          </span>
                        )}
                      </KeyValue>
                      <KeyValue label="Last read">
                        <Value value={formatRelative(engine.observedAt)} />
                      </KeyValue>
                      <KeyValue label="Storage driver">
                        <Unavailable why="The agent reads containers and networks; it does not call docker system info." />
                      </KeyValue>
                      <KeyValue label="Cgroup">
                        <Unavailable why="No engine-info contract exists in this build." />
                      </KeyValue>
                      <KeyValue label="Images">
                        <Unavailable why="No image endpoint exists. Images in use are listed below, derived from the containers." />
                      </KeyValue>
                      <KeyValue label="Volumes">
                        <Unavailable why="No volume endpoint exists. Container mounts are on each workload's detail page." />
                      </KeyValue>
                      <KeyValue label="Live restore">
                        <Unavailable why="No engine-info contract exists in this build." />
                      </KeyValue>
                    </KeyValueList>
                  </PlateBody>
                  <PlateFoot
                    source={
                      list.last_sweep
                        ? `${list.last_sweep.provider} ${list.last_sweep.provider_version} · ${list.last_sweep.capability}`
                        : 'no sweep'
                    }
                  />
                </Plate>

                <Plate quiet>
                  <PlateHead
                    title="Runtimes"
                    meta="what the current provider evidence can establish"
                    asOf={meta.fetchedAt.toLocaleTimeString()}
                  />
                  <DataTable
                    caption="Runtimes"
                    rows={rows}
                    rowKey={(row) => row.name}
                    columns={[
                      {
                        key: 'name',
                        header: 'Runtime',
                        render: (row) => <span className="mono">{row.name}</span>,
                      },
                      {
                        key: 'detected',
                        header: 'Detected',
                        render: (row) => <Detection row={row} />,
                      },
                      {
                        key: 'groups',
                        header: 'Observed groups',
                        align: 'right',
                        render: (row) =>
                          row.observedGroups === null ? (
                            <Value value={null} reason="this build has no detector for it" />
                          ) : (
                            <span className="mono">{row.observedGroups}</span>
                          ),
                      },
                      {
                        key: 'note',
                        header: 'Notes',
                        render: (row) => <span className={styles.note}>{row.note}</span>,
                      },
                    ]}
                  />
                  <PlateFoot source="compose labels on the observed containers">
                    <span className={styles.aside}>
                      a workload is a thing being run; the runtime is only how it is being run
                    </span>
                  </PlateFoot>
                </Plate>
              </div>

              <div className={styles.column}>
                <Plate quiet>
                  <PlateHead
                    title="Images in use"
                    meta="what the observed containers run from"
                    asOf={meta.fetchedAt.toLocaleTimeString()}
                    chips={<span className={styles.countChip}>{images.length}</span>}
                  />
                  <DataTable
                    caption="Images in use"
                    rows={images}
                    rowKey={(row) => row.reference}
                    emptyState={
                      <Empty
                        title="No images"
                        explanation="No observed container reports an image reference."
                      />
                    }
                    columns={[
                      {
                        key: 'reference',
                        header: 'Reference',
                        render: (row) => <span className="mono">{row.reference}</span>,
                      },
                      {
                        key: 'digest',
                        header: 'Digest',
                        render: (row) => (
                          <span className={styles.digest} title={row.imageId ?? undefined}>
                            <Value value={row.imageId} mono />
                          </span>
                        ),
                      },
                      {
                        key: 'used',
                        header: 'Used by',
                        render: (row) => <span className="mono">{row.usedBy.join(', ')}</span>,
                      },
                    ]}
                  />
                  <PlateFoot source="derived from the observed containers">
                    <span className={styles.aside}>
                      images in use, not the daemon's image list — an image that is pulled but
                      not running cannot appear here, and nothing reports a size
                    </span>
                  </PlateFoot>
                </Plate>
              </div>
            </div>
          </>
        );
      }}
    </ResourceView>
  );
}

function Detection({ row }: { row: RuntimeRow }): JSX.Element {
  if (row.detection === 'observed') {
    return <span className={styles.observed}>yes</span>;
  }
  if (row.detection === 'absent') {
    return <span className={styles.absent}>no</span>;
  }
  return (
    <span
      className={styles.undetectable}
      title="This build has no detector for this runtime, so its absence here is not evidence that it is absent."
    >
      not detected by this build
    </span>
  );
}
