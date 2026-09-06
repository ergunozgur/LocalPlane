/**
 * One interface, in the shared object workspace.
 *
 * Seven independent reads, each with its own state, because they fail independently:
 * provenance can be unavailable while the interface itself reads fine, and an operator is
 * better served by three good panels and one honest error than by a page that refuses to
 * render.
 *
 * The tab strip is assembled from what this interface *is*. A
 * Traffic tab exists only where the kernel supplied counters; it is not rendered empty and
 * it is not rendered disabled. The safety tabs (intent and drift, protection) are always
 * present, because "nothing is retained" and "protection unknown" are answers an operator
 * must be able to reach, not absences to be assembled away.
 */
import { useCallback } from 'react';
import { useParams } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { optional, useResource } from '@/hooks/useResource';
import { Plate, PlateBody, PlateHead, PlateSection } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { DataTable } from '@/components/primitives/DataTable';
import { Tag } from '@/components/primitives/Chip';
import { StatusPill } from '@/components/semantic/StatusPill';
import { ConfidenceLadder, ManagementChip, ReconciliationChip } from '@/components/semantic/SemanticGlyph';
import { Value } from '@/components/semantic/UnknownValue';
import { Conclusion, Disclosure, Gaps, RawEvidence } from '@/components/semantic/Evidence';
import { ResourceView } from '@/components/states/ResourceView';
import { Empty } from '@/components/states/SurfaceState';
import { ObjectWorkspace, ObjectColumns, type ObjectTab } from '@/components/object/ObjectWorkspace';
import { ObjectRelationships } from '@/components/object/ObjectRelationships';
import { ProtectionPanel } from '@/components/semantic/ProtectionPanel';
import { IntentPanel } from '@/components/semantic/IntentPanel';
import { ScopeBar } from '@/components/layout/ScopeBar';
import {
  fidelity as fidelityOf,
  freshness as freshnessOf,
  health as healthOf,
  ownershipState,
  protection as protectionOf,
  sourceStatus,
} from '@/domain/vocabulary';
import { formatBytes, formatCount, formatTimestamp } from '@/domain/format';
import styles from './InterfaceDetail.module.css';

export function InterfaceDetail(): JSX.Element {
  const { objectId = '' } = useParams();

  const { resource: iface } = useResource(
    `interface:${objectId}`,
    useCallback((signal) => endpoints.interfaceDetail(objectId, { signal }), [objectId]),
  );
  const { resource: protection } = useResource(
    `interface-protection:${objectId}`,
    useCallback((signal) => endpoints.interfaceProtection(objectId, { signal }), [objectId]),
  );
  const { resource: provenance } = useResource(
    `interface-provenance:${objectId}`,
    useCallback((signal) => endpoints.interfaceProvenance(objectId, { signal }), [objectId]),
  );
  const { resource: evidence } = useResource(
    `interface-evidence:${objectId}`,
    useCallback((signal) => endpoints.interfaceEvidence(objectId, { signal }), [objectId]),
  );
  // An observed object has no intent, and the backend answers 404 for it. That is the
  // absence of a thing rather than a broken read, so it is treated as an optional resource.
  const { resource: intent } = useResource(
    `interface-intent:${objectId}`,
    useCallback((signal) => endpoints.interfaceIntent(objectId, { signal }), [objectId]),
  );
  const { resource: reconciliation } = useResource(
    `interface-reconciliation:${objectId}`,
    useCallback((signal) => endpoints.interfaceReconciliation(objectId, { signal }), [objectId]),
  );
  const { resource: intentHistory } = useResource(
    `interface-intent-history:${objectId}`,
    useCallback((signal) => endpoints.interfaceIntentHistory(objectId, { signal }), [objectId]),
  );
  const { resource: interfaces } = useResource(
    'interfaces',
    useCallback((signal) => endpoints.interfaces({ signal }), []),
  );
  const { resource: containers } = useResource(
    'containers',
    useCallback((signal) => endpoints.containers({ signal }), []),
  );
  const { resource: managementPath } = useResource(
    'management-path',
    useCallback((signal) => endpoints.managementPath({ signal }), []),
  );

  return (
    <ResourceView resource={iface} what="interface" loadingLabel="Reading interface…">
      {(data, meta) => {
        // The protection verdict decides whether the tab is flagged and whether the head
        // carries it. It is read here rather than only inside the panel because a guarded
        // object must say so before an operator opens anything.
        const guarded = protection.status === 'success' ? protection.data : null;

        const tabs: ObjectTab[] = [
          {
            id: 'overview',
            label: 'Overview',
            render: () => (
              <ObjectColumns
                main={
                  <>
                    <Plate>
                      <PlateHead title="Link" level={3} meta="what the kernel reports" />
                      <PlateBody>
                        <KeyValueList columns="auto">
                          <KeyValue label="Operstate"><Value value={data.link?.operstate} mono /></KeyValue>
                          <KeyValue label="Admin up">
                            <Value
                              value={data.link?.admin_up === null || data.link?.admin_up === undefined ? null : data.link.admin_up ? 'up' : 'down'}
                              mono
                            />
                          </KeyValue>
                          <KeyValue label="Carrier">
                            <Value
                              value={data.link?.carrier === null || data.link?.carrier === undefined ? null : data.link.carrier ? 'yes' : 'no'}
                              mono
                              reason="the kernel refuses this read while the link is administratively down"
                            />
                          </KeyValue>
                          <KeyValue label="MTU"><Value value={data.link?.mtu} mono /></KeyValue>
                          <KeyValue label="MAC"><Value value={data.link?.mac_address} mono /></KeyValue>
                          <KeyValue label="Speed" hint={data.link?.speed_mbps ? 'Mbps' : undefined}>
                            <Value value={data.link?.speed_mbps} mono reason="the kernel does not know it" />
                          </KeyValue>
                          <KeyValue label="Duplex"><Value value={data.link?.duplex} mono /></KeyValue>
                          <KeyValue label="Link kind"><Value value={data.link?.link_kind} mono /></KeyValue>
                          <KeyValue label="ifindex"><Value value={data.link?.ifindex} mono /></KeyValue>
                          <KeyValue label="Master"><Value value={data.link?.master} mono /></KeyValue>
                        </KeyValueList>
                      </PlateBody>
                    </Plate>

                    <Plate>
                      <PlateHead title="Identity" level={3} meta="how LocalPlane knows this is the same interface" />
                      <PlateBody>
                        <KeyValueList columns="auto">
                          <KeyValue label="Object id">
                            <Tag title={data.object_id}>{data.object_id}</Tag>
                          </KeyValue>
                          <KeyValue label="Kind"><Value value={data.interface_kind} mono /></KeyValue>
                          <KeyValue label="Identity basis" hint={data.identity.confidence}>
                            <span className={styles.confidenceRow}>
                              <Value value={data.identity.basis} mono />
                              {/* Confidence is published, so it is drawn as well as named:
                                  three rising bars, and none at all where none was stated. */}
                              <ConfidenceLadder level={data.identity.confidence} />
                            </span>
                          </KeyValue>
                          <KeyValue label="Identity value">
                            <Value value={data.identity.value} mono />
                          </KeyValue>
                          <KeyValue label="First seen">
                            <Value value={formatTimestamp(data.first_seen_at)} />
                          </KeyValue>
                          <KeyValue label="Last seen">
                            <Value value={formatTimestamp(data.last_seen_at)} />
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
            id: 'addressing',
            label: 'Addressing',
            count: data.addresses === null ? null : data.addresses.length,
            render: () => (
              <ObjectColumns
                main={
                  <Plate>
                    <PlateHead title="Addresses" level={3} />
                    {data.addresses === null ? (
                      <PlateBody>
                        <Empty
                          title="No address source was available"
                          explanation="This is not an interface with no addresses — it is an interface whose addresses were not read."
                        />
                      </PlateBody>
                    ) : (
                      <DataTable
                        caption="Addresses"
                        rows={data.addresses}
                        rowKey={(row) => `${row.family}-${row.address}-${row.prefix_length}`}
                        emptyState={
                          <PlateBody>
                            <Empty
                              title="No addresses"
                              explanation="Observed, and it genuinely has none."
                            />
                          </PlateBody>
                        }
                        columns={[
                          { key: 'family', header: 'Family', render: (r) => <span className="mono">{r.family}</span> },
                          {
                            key: 'address',
                            header: 'Address',
                            render: (r) => <span className="mono">{r.address}/{r.prefix_length}</span>,
                          },
                          { key: 'scope', header: 'Scope', render: (r) => <Value value={r.scope} mono /> },
                          {
                            key: 'dynamic',
                            header: 'Dynamic',
                            render: (r) => (
                              <Value value={r.dynamic === null ? null : r.dynamic ? 'yes' : 'no'} mono />
                            ),
                          },
                        ]}
                      />
                    )}
                  </Plate>
                }
              />
            ),
          },
          {
            id: 'intent',
            label: 'Intent and drift',
            warn: data.reconciliation === 'drifted',
            render: () => (
              <ObjectColumns
                main={
                  <IntentPanel
                    object={data}
                    intent={optional(intent)}
                    reconciliation={optional(reconciliation)}
                    history={optional(intentHistory)}
                  />
                }
              />
            ),
          },
          {
            id: 'protection',
            label: 'Protection',
            warn: guarded !== null && guarded.status !== 'clear',
            render: () => <ObjectColumns main={<ProtectionPanel resource={protection} />} />,
          },
          {
            id: 'provenance',
            label: 'Provenance',
            render: () => (
              <ObjectColumns
                main={
                  <Plate>
                    <PlateHead title="Provenance" level={3} meta="who made this, and who configures it" />
                    <PlateBody>
                      <ResourceView resource={provenance} what="provenance">
                        {(prov) => (
                          <>
                            <Conclusion
                              semantic={ownershipState(prov.state)}
                              token={prov.state}
                              why={prov.reason}
                            />

                            <PlateSection title="Claims">
                              {prov.claims.length === 0 ? (
                                <Empty title="No claim was made" explanation="No source attributed this object." />
                              ) : (
                                <KeyValueList>
                                  {prov.claims.map((claim) => (
                                    <KeyValue
                                      key={`${claim.relation}-${claim.owner.provider}`}
                                      label={claim.relation.replace(/_/g, ' ')}
                                    >
                                      <span className="mono">{claim.owner.provider}</span>
                                      {claim.owner.label ? (
                                        <span className={styles.ownerLabel}>{claim.owner.label}</span>
                                      ) : null}
                                      <ConfidenceLadder level={claim.confidence} />
                                      <span className={styles.confidenceWord}>{claim.confidence}</span>
                                    </KeyValue>
                                  ))}
                                </KeyValueList>
                              )}
                            </PlateSection>

                            <PlateSection title="Sources consulted">
                              <KeyValueList>
                                {prov.sources.map((source) => (
                                  <KeyValue
                                    key={source.source}
                                    label={<span className="mono">{source.source}</span>}
                                  >
                                    <span className={styles.sourceRow}>
                                      <StatusPill semantic={sourceStatus(source.status)} size="sm" />
                                      <span className={styles.outcome}>{source.outcome}</span>
                                      {source.gap ? <span className={styles.gapFlag}>left a gap</span> : null}
                                    </span>
                                  </KeyValue>
                                ))}
                              </KeyValueList>
                            </PlateSection>

                            <PlateSection title="Adoption">
                              <KeyValueList>
                                <KeyValue label="Eligible">
                                  {prov.adoption.eligible ? 'yes' : 'no'}
                                </KeyValue>
                                <KeyValue label="Reason">
                                  <Value value={prov.adoption.reason} mono />
                                </KeyValue>
                                {prov.adoption.blocked_by ? (
                                  <KeyValue label="Blocked by">
                                    <Value value={prov.adoption.blocked_by.provider} mono />
                                  </KeyValue>
                                ) : null}
                              </KeyValueList>
                              <Gaps items={prov.adoption.evidence_gaps ?? []} label="Evidence gaps" />
                            </PlateSection>
                          </>
                        )}
                      </ResourceView>
                    </PlateBody>
                  </Plate>
                }
              />
            ),
          },
          {
            // Assembled away, not rendered empty: an interface whose source supplied no
            // counters has no Traffic tab, and the Overview does not pretend otherwise.
            id: 'traffic',
            label: 'Traffic',
            hidden: !data.statistics,
            render: () => (
              <ObjectColumns
                main={
                  <Plate>
                    <PlateHead
                      title="Statistics"
                      level={3}
                      meta="cumulative since the interface appeared"
                    />
                    <PlateBody>
                      {data.statistics ? (
                        <KeyValueList columns="auto">
                          <KeyValue label="Received"><Value value={formatBytes(data.statistics.rx_bytes)} mono /></KeyValue>
                          <KeyValue label="Transmitted"><Value value={formatBytes(data.statistics.tx_bytes)} mono /></KeyValue>
                          <KeyValue label="RX packets"><Value value={formatCount(data.statistics.rx_packets)} mono /></KeyValue>
                          <KeyValue label="TX packets"><Value value={formatCount(data.statistics.tx_packets)} mono /></KeyValue>
                          <KeyValue label="RX errors"><Value value={formatCount(data.statistics.rx_errors)} mono /></KeyValue>
                          <KeyValue label="TX errors"><Value value={formatCount(data.statistics.tx_errors)} mono /></KeyValue>
                          <KeyValue label="RX dropped"><Value value={formatCount(data.statistics.rx_dropped)} mono /></KeyValue>
                          <KeyValue label="TX dropped"><Value value={formatCount(data.statistics.tx_dropped)} mono /></KeyValue>
                        </KeyValueList>
                      ) : null}
                      <p className={styles.rawNote}>
                        These are lifetime counters. LocalPlane keeps no history of them, so
                        there is no rate here and no chart — a throughput line would have to be
                        invented.
                      </p>
                    </PlateBody>
                  </Plate>
                }
              />
            ),
          },
          {
            id: 'relationships',
            label: 'Relationships',
            render: () => (
              <ObjectColumns
                main={
                  <ObjectRelationships
                    subject={data}
                    interfaces={interfaces}
                    containers={containers}
                    managementPath={managementPath}
                  />
                }
              />
            ),
          },
          {
            id: 'evidence',
            label: 'Evidence',
            render: () => (
              <ObjectColumns
                main={
                  <Plate>
                    <PlateHead title="Raw evidence" level={3} meta="what the provider returned" />
                    <PlateBody>
                      <ResourceView resource={evidence} what="evidence">
                        {(ev) => (
                          <>
                            <p className={styles.rawNote}>
                              A developer view. The panels on the other tabs are the
                              authoritative reading of this.
                            </p>
                            <Disclosure summary="Raw evidence">
                              <RawEvidence value={ev.evidence} />
                            </Disclosure>
                          </>
                        )}
                      </ResourceView>
                    </PlateBody>
                  </Plate>
                }
                side={<ObservationPlate data={data} />}
              />
            ),
          },
        ];

        return (
          <>
            <ScopeBar
              crumbs={[
                { label: 'network', to: '/network' },
                { label: data.interface_kind },
                { label: data.name, mono: true },
              ]}
              observedAt={meta.fetchedAt}
            />

            <ObjectWorkspace
              objectId={data.object_id}
              name={data.name}
              kind={data.interface_kind}
              mark={healthOf(data.health?.state)}
              observedAt={meta.fetchedAt}
              tone={data.reconciliation === 'drifted' ? 'attention' : undefined}
              headline={
                data.management.state === 'managed'
                  ? 'LocalPlane retains an intended state for this interface and compares the host against it.'
                  : 'Read and recorded. Nothing is retained for this interface, so it has a health but no reconciliation state.'
              }
              path={[
                { label: 'network', to: '/network' },
                { prefix: data.interface_kind, label: data.name },
              ]}
              chips={
                <>
                  {/* The head carries the mark twice — standalone before the name, and
                      inside the health chip. Not three times: the pill draws its own. */}
                  <StatusPill semantic={healthOf(data.health?.state)} size="sm" token={data.health?.reason} />
                  <ManagementChip state={data.management.state} />
                  <ReconciliationChip state={data.reconciliation} />
                  {guarded ? (
                    <StatusPill semantic={protectionOf(guarded.status)} size="sm" token={guarded.status} />
                  ) : null}
                </>
              }
              contextFact={
                data.link?.mtu === null || data.link?.mtu === undefined ? null : `mtu ${data.link.mtu}`
              }
              tabs={tabs}
            />
          </>
        );
      }}
    </ResourceView>
  );
}

/** Observation is context for whatever tab is open, so it is rendered beside more than one. */
function ObservationPlate({ data }: { data: import('@/api/types').NetworkInterface }): JSX.Element {
  return (
    <Plate>
      <PlateHead title="Observation" level={3} meta="who read this, and how well" />
      <PlateBody>
        {data.observation ? (
          <>
            <KeyValueList>
              <KeyValue label="Freshness">
                <StatusPill
                  semantic={freshnessOf(data.observation.freshness)}
                  size="sm"
                  token={data.observation.freshness}
                />
              </KeyValue>
              <KeyValue label="Fidelity">
                <StatusPill
                  semantic={fidelityOf(data.observation.fidelity)}
                  size="sm"
                  token={data.observation.fidelity}
                />
              </KeyValue>
              <KeyValue label="Provider" hint={data.observation.provider_version}>
                <Value value={data.observation.provider} mono />
              </KeyValue>
              <KeyValue label="Method"><Value value={data.observation.method} mono /></KeyValue>
              <KeyValue label="Observed at">
                <Value value={formatTimestamp(data.observation.observed_at)} />
              </KeyValue>
              <KeyValue label="In latest sweep">
                <Value
                  value={
                    data.observed_in_latest_sweep === null
                      ? null
                      : data.observed_in_latest_sweep
                        ? 'yes'
                        : 'no'
                  }
                />
              </KeyValue>
            </KeyValueList>
            <Gaps items={data.observation.gaps} label="Fields the source did not supply" />
          </>
        ) : (
          <Empty
            title="Never observed"
            explanation="This object exists in LocalPlane's records but nothing has read it."
          />
        )}
      </PlateBody>
    </Plate>
  );
}
