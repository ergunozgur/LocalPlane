/**
 * One systemd unit, in the shared object workspace.
 *
 * The relationship list is the reason this page is worth having. `Requires`, `After`,
 * `BoundBy` and the rest are what turn "restart this" from a button into a question about
 * what else moves, and a unit on this host carries up to 187 of them. It gets its own tab
 * rather than a scroll position.
 *
 * This is where the assembly rule earns its keep: a `.service` has no Socket tab and a
 * `.socket` has no Timer tab, because those are properties of one unit rather than sections
 * every unit owns. The Lifecycle tab is present on all of them — *"there is nothing to set,
 * and here is why" is an answer worth showing* — and it is the panel, not this route, that
 * reads the execution gate.
 */
import { useCallback, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { DataTable } from '@/components/primitives/DataTable';
import { Tag } from '@/components/primitives/Chip';
import { StatusPill } from '@/components/semantic/StatusPill';
import { Value } from '@/components/semantic/UnknownValue';
import { Gaps } from '@/components/semantic/Evidence';
import { LifecyclePanel } from '@/components/semantic/LifecyclePanel';
import { ResourceView } from '@/components/states/ResourceView';
import { Empty } from '@/components/states/SurfaceState';
import { ObjectWorkspace, ObjectColumns, type ObjectTab } from '@/components/object/ObjectWorkspace';
import { ScopeBar } from '@/components/layout/ScopeBar';
import { systemdTime, unitNote } from '@/domain/systemd';
import {
  freshness as freshnessOf,
  health as healthOf,
  unitActiveState,
  unitFileState,
  unitLoadState,
} from '@/domain/vocabulary';
import { formatSystemdTimestamp, formatTimestamp } from '@/domain/format';
import styles from './UnitDetail.module.css';

export function UnitDetail(): JSX.Element {
  const { objectId = '' } = useParams();
  const [relationFilter, setRelationFilter] = useState('');

  const { resource: unit } = useResource(
    `systemd-unit:${objectId}`,
    useCallback((signal) => endpoints.systemdUnit(objectId, { signal }), [objectId]),
  );
  const { resource: list } = useResource(
    'systemd-units',
    useCallback((signal) => endpoints.systemdUnits({ signal }), []),
  );

  const capability = list.status === 'success' ? list.data.capability : null;

  return (
    <ResourceView resource={unit} what="unit" loadingLabel="Reading unit…">
      {(data, meta) => {
        const tabs: ObjectTab[] = [
          {
            id: 'overview',
            label: 'Overview',
            render: () => (
              <ObjectColumns
                main={
                  <Plate>
                    <PlateHead title="State" level={3} meta="as the manager reports it" />
                    <PlateBody>
                      <KeyValueList columns="auto">
                        <KeyValue label="Load state">
                          <StatusPill semantic={unitLoadState(data.load_state)} size="sm" token={data.load_state} />
                        </KeyValue>
                        <KeyValue label="Active state">
                          <StatusPill semantic={unitActiveState(data.active_state)} size="sm" token={data.active_state} />
                        </KeyValue>
                        <KeyValue label="Sub state"><Value value={data.sub_state} mono /></KeyValue>
                        <KeyValue label="Unit file">
                          <StatusPill semantic={unitFileState(data.unit_file_state)} size="sm" token={data.unit_file_state} />
                        </KeyValue>
                        <KeyValue label="Preset"><Value value={data.unit_file_preset} mono /></KeyValue>
                        <KeyValue label="Unit type"><Value value={data.unit_type} mono /></KeyValue>
                        <KeyValue label="Transient">
                          <Value value={data.transient === null ? null : data.transient ? 'yes' : 'no'} mono />
                        </KeyValue>
                        <KeyValue label="Invocation id">
                          <Value value={data.invocation_id} mono reason="the unit has no current execution instance" />
                        </KeyValue>
                        <KeyValue label="Fragment path"><Value value={data.fragment_path} mono /></KeyValue>
                        <KeyValue label="Current job">
                          <Value value={data.current_job?.id} mono reason="no job is queued for this unit" />
                        </KeyValue>
                      </KeyValueList>

                      {data.drop_in_paths && data.drop_in_paths.length > 0 ? (
                        <div className={styles.dropins}>
                          <div className="label">Drop-ins</div>
                          <ul className={styles.dropinList}>
                            {data.drop_in_paths.map((path) => (
                              <li key={path}><Tag title={path}>{path}</Tag></li>
                            ))}
                          </ul>
                        </div>
                      ) : null}
                    </PlateBody>
                  </Plate>
                }
                side={<ObservationPlate data={data} />}
              />
            ),
          },
          {
            id: 'lifecycle',
            label: 'Lifecycle',
            render: () => (
              <ObjectColumns main={<LifecyclePanel unit={data} capability={capability} />} />
            ),
          },
          {
            id: 'relationships',
            label: 'Relationships',
            count: data.relationships.length,
            render: () => (
              <ObjectColumns
                main={
                  <Plate>
                    <PlateHead
                      title="Relationships"
                      level={3}
                      meta="what else moves when this one does"
                    />
                    <PlateBody>
                      <label className={styles.filterRow}>
                        <span className="visually-hidden">Filter relationships</span>
                        <input
                          type="search"
                          className={styles.input}
                          value={relationFilter}
                          onChange={(event) => setRelationFilter(event.target.value)}
                          placeholder="Filter by kind or unit…"
                        />
                      </label>
                      <RelationshipTable relationships={data.relationships} filter={relationFilter} />
                    </PlateBody>
                  </Plate>
                }
              />
            ),
          },
          {
            // Assembled from the unit's type: a service has no socket facts, and a Socket tab
            // sitting there empty would suggest the manager withheld them.
            id: 'socket',
            label: 'Socket',
            hidden: !data.socket,
            render: () => (
              <ObjectColumns
                main={
                  data.socket ? (
                    <Plate>
                      <PlateHead
                        title="Socket"
                        level={3}
                        meta="what this socket listens on, and what it has accepted"
                      />
                      <PlateBody>
                        <KeyValueList columns="auto">
                          <KeyValue label="Accepts connections">
                            <Value
                              value={data.socket.accept === null ? null : data.socket.accept ? 'yes' : 'no'}
                              mono
                            />
                          </KeyValue>
                          <KeyValue label="Accepted"><Value value={data.socket.accepted} mono /></KeyValue>
                          <KeyValue label="Connections"><Value value={data.socket.connections} mono /></KeyValue>
                          <KeyValue label="Refused"><Value value={data.socket.refused} mono /></KeyValue>
                          <KeyValue label="Result"><Value value={data.socket.result} mono /></KeyValue>
                        </KeyValueList>
                        {data.socket.listen && data.socket.listen.length > 0 ? (
                          <div className={styles.listen}>
                            <div className="label">Listening</div>
                            <ul className={styles.listenList}>
                              {data.socket.listen.map((entry, index) => (
                                <li key={`${entry.kind}-${entry.address}-${index}`}>
                                  <Tag title={`${entry.kind} ${entry.address}`}>
                                    {entry.kind} {entry.address}
                                  </Tag>
                                </li>
                              ))}
                            </ul>
                          </div>
                        ) : null}
                      </PlateBody>
                    </Plate>
                  ) : null
                }
              />
            ),
          },
          {
            id: 'timer',
            label: 'Timer',
            hidden: !data.timer,
            render: () => (
              <ObjectColumns
                main={
                  data.timer ? (
                    <Plate>
                      <PlateHead title="Timer" level={3} meta="when this timer last fired, and when it fires next" />
                      <PlateBody>
                        <KeyValueList columns="auto">
                          <KeyValue label="Triggers"><Value value={data.timer.unit} mono /></KeyValue>
                          <KeyValue label="Last trigger">
                            <Value
                              value={formatTimestamp(systemdTime(data.timer.last_trigger_usec))}
                              reason="this timer has not fired"
                            />
                          </KeyValue>
                          <KeyValue label="Next elapse">
                            <Value
                              value={formatTimestamp(systemdTime(data.timer.next_elapse_usec_realtime))}
                              reason="no wall-clock elapse is scheduled"
                            />
                          </KeyValue>
                          <KeyValue label="Persistent">
                            <Value
                              value={
                                data.timer.persistent === null ? null : data.timer.persistent ? 'yes' : 'no'
                              }
                              mono
                            />
                          </KeyValue>
                          <KeyValue label="Result"><Value value={data.timer.result} mono /></KeyValue>
                        </KeyValueList>
                      </PlateBody>
                    </Plate>
                  ) : null
                }
              />
            ),
          },
          {
            id: 'service',
            label: 'Service facts',
            hidden: !data.service,
            render: () => (
              <ObjectColumns
                main={
                  data.service ? (
                    <Plate>
                      <PlateHead title="Service" level={3} meta="every service property the manager returned" />
                      <PlateBody>
                        <KeyValueList columns="auto">
                          {Object.entries(data.service).map(([key, value]) => (
                            <KeyValue key={key} label={<span className="mono">{key}</span>}>
                              <Value
                                value={
                                  value === null || value === undefined
                                    ? null
                                    : typeof value === 'object'
                                      ? JSON.stringify(value)
                                      : String(value)
                                }
                                mono
                              />
                            </KeyValue>
                          ))}
                        </KeyValueList>
                      </PlateBody>
                    </Plate>
                  ) : null
                }
              />
            ),
          },
          {
            // Present only where the backend proved this unit contains LocalPlane's own
            // agent. It carries the warn dot because an operator reading any other tab of
            // this unit needs to know before they act on it.
            id: 'agent',
            label: 'This unit runs the agent',
            hidden: !data.agent_process_containment,
            warn: Boolean(data.agent_process_containment),
            render: () => (
              <ObjectColumns
                main={
                  data.agent_process_containment ? (
                    <Plate tone="attention">
                      <PlateHead
                        title="LocalPlane's own agent"
                        level={3}
                        meta="this unit contains the process observing this host"
                      />
                      <PlateBody>
                        <KeyValueList>
                          <KeyValue label="Status">
                            <Value value={data.agent_process_containment.status} mono />
                          </KeyValue>
                          <KeyValue label="Method">
                            <Value value={data.agent_process_containment.method} mono />
                          </KeyValue>
                          <KeyValue label="Cgroup">
                            <Value value={data.agent_process_containment.cgroup} mono />
                          </KeyValue>
                          <KeyValue label="Invocation">
                            <Value value={data.agent_process_containment.invocation_id} mono />
                          </KeyValue>
                        </KeyValueList>
                        <Gaps items={data.agent_process_containment.gaps} label="Correlation gaps" />
                        <p className={styles.agentNote}>
                          Stopping this unit would stop the agent that observes this host. That is
                          why the backend correlates it, and why a lifecycle action against it would
                          be protected.
                        </p>
                      </PlateBody>
                    </Plate>
                  ) : null
                }
              />
            ),
          },
          {
            id: 'timestamps',
            label: 'Timestamps',
            render: () => (
              <ObjectColumns
                main={
                  <Plate>
                    <PlateHead title="Timestamps" level={3} meta="as the manager reports them" />
                    <PlateBody>
                      <KeyValueList columns="auto">
                        {Object.entries(data.timestamps).map(([key, value]) => (
                          <KeyValue key={key} label={<span className="mono">{key}</span>}>
                            <Value
                              value={formatSystemdTimestamp(value)}
                              reason="the manager reports zero, meaning this has never happened"
                            />
                          </KeyValue>
                        ))}
                      </KeyValueList>
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
                { label: 'system', to: '/system' },
                { label: data.unit_type, to: `/system?type=${data.unit_type}` },
                { label: data.canonical_id, mono: true },
              ]}
              observedAt={meta.fetchedAt}
            />

            <ObjectWorkspace
              objectId={data.object_id}
              name={data.canonical_id}
              kind={data.unit_type}
              mark={healthOf(data.health?.state)}
              observedAt={meta.fetchedAt}
              tone={data.agent_process_containment ? 'attention' : undefined}
              headline={[data.description, unitNote(data)].filter(Boolean).join(' · ') || undefined}
              path={[
                { label: 'system', to: '/system' },
                { label: data.unit_type, to: `/system?type=${data.unit_type}` },
                { label: data.canonical_id },
              ]}
              chips={
                <>
                  <StatusPill semantic={unitActiveState(data.active_state)} size="sm" token={data.active_state} />
                  <StatusPill semantic={unitLoadState(data.load_state)} size="sm" />
                  <StatusPill semantic={healthOf(data.health?.state)} size="sm" />
                </>
              }
              contextFact={<span className="mono">{data.sub_state}</span>}
              tabs={tabs}
            />
          </>
        );
      }}
    </ResourceView>
  );
}

/** Observation is context for whatever tab is open, so it sits beside the body. */
function ObservationPlate({ data }: { data: import('@/api/types').SystemdUnit }): JSX.Element {
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
            <KeyValue label="Method"><Value value={data.observation.method} mono /></KeyValue>
            <KeyValue label="Observed at">
              <Value value={formatTimestamp(data.observation.observed_at)} />
            </KeyValue>
            <KeyValue label="In latest sweep">
              <Value
                value={
                  data.observed_in_latest_sweep === null
                    ? null
                    : data.observed_in_latest_sweep ? 'yes' : 'no'
                }
              />
            </KeyValue>
          </KeyValueList>
        ) : (
          <Empty title="Never observed" explanation="Nothing has read this unit." />
        )}
      </PlateBody>
    </Plate>
  );
}

function RelationshipTable({
  relationships,
  filter,
}: {
  relationships: import('@/api/types').SystemdRelationship[];
  filter: string;
}): JSX.Element {
  const needle = filter.trim().toLowerCase();
  const rows = useMemo(
    () =>
      relationships.filter(
        (relation) =>
          !needle ||
          relation.kind.toLowerCase().includes(needle) ||
          relation.group.toLowerCase().includes(needle) ||
          relation.target_unit.toLowerCase().includes(needle),
      ),
    [relationships, needle],
  );

  return (
    <div className={styles.relationScroll}>
      <DataTable
        caption="Unit relationships"
        rows={rows}
        rowKey={(row) => `${row.kind}:${row.target_unit}`}
        emptyState={
          <Empty
            title={needle ? 'No relationship matches' : 'No relationships'}
            explanation={
              needle
                ? 'No declared relationship matches this filter.'
                : 'The manager reported no dependency relationships for this unit.'
            }
          />
        }
        columns={[
          {
            key: 'kind',
            header: 'Relation',
            render: (row) => <span className="mono">{row.kind}</span>,
          },
          {
            key: 'group',
            header: 'Group',
            render: (row) => <Value value={row.group} mono />,
          },
          {
            key: 'target',
            header: 'Target unit',
            render: (row) =>
              row.target_object_id ? (
                <Link to={`/system/${row.target_object_id}`} className="mono">
                  {row.canonical_target ?? row.target_unit}
                </Link>
              ) : (
                <span className="mono">{row.canonical_target ?? row.target_unit}</span>
              ),
          },
          {
            key: 'resolution',
            header: 'Resolution',
            render: (row) => (
              <StatusPill semantic={relationResolution(row.resolution)} size="sm" />
            ),
          },
          {
            key: 'estate',
            header: 'In estate',
            render: (row) => (
              <Value
                value={row.estate_state}
                mono
                reason="this target is outside the observed estate"
              />
            ),
          },
        ]}
      />
    </div>
  );
}

/**
 * How a declared relationship was resolved against the observed estate.
 *
 * `referenced` is not a failure — a unit may legitimately name a target systemd has not
 * loaded — so it reads neutral rather than as a warning.
 */
function relationResolution(token: string): import('@/domain/vocabulary').Semantic {
  switch (token) {
    case 'resolved':
      return {
        tone: 'good',
        label: 'resolved',
        description: 'The target was matched to a unit in the observed estate.',
      };
    case 'referenced':
      return {
        tone: 'neutral',
        label: 'referenced',
        description:
          'The unit names this target, and it was not matched to an observed unit. Not a failure — systemd need not have loaded it.',
      };
    case 'external':
      return {
        tone: 'neutral',
        label: 'external',
        description: 'The target lies outside the estate LocalPlane observes.',
      };
    default:
      return {
        tone: 'unknown',
        label: token,
        description: 'This build does not recognise this resolution value.',
      };
  }
}
