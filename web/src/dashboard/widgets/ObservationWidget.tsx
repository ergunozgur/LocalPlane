/**
 * Summary — this host's identity, its agent, and what has been read from it.
 *
 * The design direction pairs the device overview with a Summary plate carrying `Identity`,
 * `Resources` and `Operational` sections of key-value rows. This is that plate, with the
 * sections this build can fill: identity and agent facts come from `/host` and `/agent`;
 * observation from the sweep list. The `Resources` section (load, memory, disk,
 * temperature) has no backing contract and is omitted rather than faked.
 *
 * The `Resources` section is kept as a **shell**. There is no host-metric contract in
 * this build — no load, no memory, no disk, no temperature — and the honest options were to
 * drop the section or to render its frame and say so. Dropping it teaches an operator that
 * LocalPlane does not measure hosts; drawing it from nothing teaches them worse. The frame
 * says what is missing and what would fill it, and takes a real series the day one exists.
 *
 * The split matters compositionally as much as semantically: the overview next door is the
 * relationship plane, and the facts about the machine belong here rather than doubling its
 * height.
 *
 * The sweep list is also where "the estate is empty" and "nobody looked" become
 * distinguishable, which makes this the quietest load-bearing panel on the page.
 */
import { useCallback } from 'react';
import { endpoints } from '@/api/endpoints';
import { combine, useResource } from '@/hooks/useResource';
import { Plate, PlateFoot, PlateHead, PlateSection } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { StatusPill } from '@/components/semantic/StatusPill';
import { Disclosure } from '@/components/semantic/Evidence';
import { ResourceView } from '@/components/states/ResourceView';
import { Empty } from '@/components/states/SurfaceState';
import { Value } from '@/components/semantic/UnknownValue';
import { ChartShell } from '@/components/semantic/Metric';
import { capabilityStatus, freshness as freshnessOf, sweepStatus } from '@/domain/vocabulary';
import { formatRelative, formatTimestamp } from '@/domain/format';
import styles from './ObservationWidget.module.css';

export function ObservationWidget(): JSX.Element {
  const { resource: host } = useResource(
    'host',
    useCallback((signal) => endpoints.host({ signal }), []),
  );
  const { resource: agent } = useResource(
    'agent',
    useCallback((signal) => endpoints.agent({ signal }), []),
  );
  const { resource: sweeps } = useResource(
    'sweeps',
    useCallback((signal) => endpoints.sweeps(20, { signal }), []),
  );
  const { resource: capabilities } = useResource(
    'capabilities',
    useCallback((signal) => endpoints.capabilities({ signal }), []),
  );

  const combined = combine(combine(host, agent), combine(sweeps, capabilities));

  return (
    <Plate quiet className={styles.plate}>
      <ResourceView resource={combined} what="host summary" loadingLabel="Reading…">
        {([[hostData, agentData], [sweepList, capabilityList]], meta) => (
          <>
            <PlateHead title="Summary" asOf={meta.fetchedAt.toLocaleTimeString()} />

            <PlateSection title="Identity">
              <KeyValueList>
                <KeyValue label="Host id">
                  <Value value={hostData.host_id} mono />
                </KeyValue>
                <KeyValue label="Basis" hint={hostData.identity_confidence}>
                  <Value value={hostData.identity_basis} mono />
                </KeyValue>
                <KeyValue
                  label="Configured hostname"
                  hint={
                    hostData.configured_hostname && hostData.configured_hostname !== hostData.hostname
                      ? 'differs from the running hostname'
                      : undefined
                  }
                >
                  <Value value={hostData.configured_hostname} mono />
                </KeyValue>
                <KeyValue label="OS">
                  <Value value={hostData.os_pretty_name} />
                </KeyValue>
                <KeyValue label="Kernel">
                  <Value value={hostData.kernel_release} mono />
                </KeyValue>
                <KeyValue label="Arch">
                  <Value value={hostData.architecture} mono />
                </KeyValue>
                <KeyValue label="Boot id">
                  <Value value={hostData.boot_id} mono />
                </KeyValue>
                <KeyValue label="First seen">
                  <Value value={formatTimestamp(hostData.first_seen_at)} />
                </KeyValue>
                <KeyValue label="Last seen" hint={hostData.freshness}>
                  <Value value={formatRelative(hostData.last_seen_at)} />
                </KeyValue>
              </KeyValueList>
            </PlateSection>

            <PlateSection title="Agent">
              <KeyValueList>
                <KeyValue label="Version" hint={agentData.source === 'live' ? 'probed now' : 'last known'}>
                  <Value value={agentData.agent?.agent_version} mono reason="the agent has not been reached" />
                </KeyValue>
                <KeyValue label="Privilege">
                  <Value value={agentData.agent?.privilege} mono reason="the agent has not been reached" />
                </KeyValue>
                <KeyValue label="Transport">
                  <Value value={agentData.agent?.transport} mono reason="the agent has not been reached" />
                </KeyValue>
                <KeyValue label="Isolated">
                  <Value
                    value={agentData.agent ? (agentData.agent.process_isolated ? 'yes' : 'no') : null}
                    reason="the agent has not been reached"
                  />
                </KeyValue>
                <KeyValue label="Instance">
                  <Value
                    value={agentData.agent?.agent_instance_id}
                    mono
                    reason="the agent has not been reached"
                  />
                </KeyValue>
              </KeyValueList>
            </PlateSection>

            <PlateSection title="Resources">
              <div className={styles.charts}>
                <ChartShell
                  title="Load"
                  unit="1m · 5m · 15m"
                  series={null}
                  absence="Nothing reads this host's load, so there is no series to draw."
                  wouldFill="would need: host.metrics.observe"
                  height={72}
                />
                <ChartShell
                  title="Memory"
                  unit="used of total"
                  series={null}
                  absence="No memory reading is published for the host. Container memory is measured, on demand, per container."
                  wouldFill="would need: host.metrics.observe"
                  height={72}
                />
              </div>
              <p className={styles.caveat}>
                LocalPlane keeps no history of anything. Every number in this console is a
                reading taken at the moment shown beside it, and these frames stay empty until
                a host-metric capability exists to fill them.
              </p>
            </PlateSection>

            <PlateSection title="Observation">
              {sweepList.sweeps.length === 0 ? (
                <Empty
                  title="Nothing has been read"
                  explanation="No sweep has completed. Every list in this console will be empty because nobody looked, not because there is nothing to see."
                />
              ) : (
                <KeyValueList>
                  {sweepList.sweeps.slice(0, 5).map((sweep) => (
                    <KeyValue
                      key={sweep.sweep_id}
                      label={<span className="mono">{sweep.capability}</span>}
                      hint={formatRelative(sweep.completed_at) ?? undefined}
                    >
                      <span className={styles.sweepRow}>
                        <StatusPill semantic={sweepStatus(sweep.status)} size="sm" />
                        <span className={styles.count}>{sweep.object_count} objects</span>
                      </span>
                    </KeyValue>
                  ))}
                  <KeyValue label="Freshness">
                    <StatusPill
                      semantic={freshnessOf(hostData.freshness)}
                      size="sm"
                      token={hostData.freshness}
                    />
                  </KeyValue>
                </KeyValueList>
              )}

              <div className={styles.capabilities}>
                <Disclosure summary="Agent capabilities" count={capabilityList.capabilities.length}>
                  <p className={styles.caveat}>
                    A capability describes a <em>mechanism the agent probed</em>. It is not
                    permission to act and it is not evidence that LocalPlane can execute
                    anything — that answer only exists in a published plan.
                  </p>
                  <KeyValueList>
                    {capabilityList.capabilities.map((capability) => (
                      <KeyValue
                        key={capability.capability}
                        label={<span className="mono">{capability.capability}</span>}
                      >
                        <span className={styles.sweepRow}>
                          <StatusPill semantic={capabilityStatus(capability.status)} size="sm" />
                          {capability.mutating ? (
                            <span
                              className={styles.mutating}
                              title="Using this mechanism can change the host."
                            >
                              mutating
                            </span>
                          ) : null}
                          {capability.reason ? <Value value={capability.reason} mono /> : null}
                        </span>
                      </KeyValue>
                    ))}
                  </KeyValueList>
                </Disclosure>
              </div>
            </PlateSection>

            <PlateFoot
              source={
                <>
                  {sweepList.count} sweep{sweepList.count === 1 ? '' : 's'} recorded ·{' '}
                  {capabilityList.source === 'live'
                    ? 'capabilities probed for this request'
                    : 'capabilities last recorded'}
                </>
              }
            />
          </>
        )}
      </ResourceView>
    </Plate>
  );
}
