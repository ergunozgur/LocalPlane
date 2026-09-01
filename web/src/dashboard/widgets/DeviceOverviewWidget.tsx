/**
 * Device overview — the machine, and everything it is proven to be attached to.
 *
 * This is the device overview widget: a lead plate whose head carries the host's name, its
 * facts and its state chips, over a four-column relationship plane. What it shows is
 * derived entirely from published backend evidence — see `Topology.tsx` for how each node is
 * justified, and for the node kinds (Internet, carrier, tailnet, relay) that are omitted
 * because no contract supplies them.
 *
 * Density follows the widget's **own** width, not the viewport's, because a dashboard will
 * later let an operator resize it independently of the window. The mode changes *scope*:
 *
 *   expanded  four columns, full facts, secondary facts inline
 *   compact   host and what it is attached to; upstream and path move to the fact list
 *   minimal   identity, state and the management-path verdict; the plane is not drawn
 *
 * Two rules hold in every mode. Safety state — health, agent reachability, the
 * management-path verdict — is always rendered as text plus glyph and is never hover-only.
 * And reducing density hides *fields*; it never shrinks type.
 */
import { useCallback, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource, combine } from '@/hooks/useResource';
import { useContainerDensity } from '@/hooks/useContainerDensity';
import { Plate, PlateBody, PlateFoot, PlateHead } from '@/components/primitives/Plate';
import { StatusPill } from '@/components/semantic/StatusPill';
import { Topology } from '@/components/semantic/Topology';
import { buildTopology } from '@/components/semantic/topology-model';
import { Gaps } from '@/components/semantic/Evidence';
import { ResourceView } from '@/components/states/ResourceView';
import {
  freshness as freshnessOf,
  managementPathState,
  humanise,
} from '@/domain/vocabulary';
import styles from './DeviceOverviewWidget.module.css';

/**
 * How many columns of the plane fit.
 *
 * Derived from the measured width rather than the density mode, because they answer
 * different questions: density decides which *fields* a node shows, and this decides how
 * many columns can be legible. Four columns crammed into a laptop-width panel truncates
 * every node name, which is worse than showing three.
 */
function columnsForWidth(width: number): number {
  // Thresholds are set by what stays legible, not by tidy round numbers: below these a node
  // name or its address starts ellipsising, and a truncated identifier is worth less than a
  // column.
  if (width >= 1150) return 4;
  if (width >= 900) return 3;
  if (width >= 560) return 2;
  return 1;
}

export function DeviceOverviewWidget(): JSX.Element {
  const [ref, density, width] = useContainerDensity<HTMLDivElement>();

  const { resource: host, refresh } = useResource(
    'host',
    useCallback((signal) => endpoints.host({ signal }), []),
  );
  const { resource: agent } = useResource(
    'agent',
    useCallback((signal) => endpoints.agent({ signal }), []),
  );
  const { resource: path } = useResource(
    'management-path',
    useCallback((signal) => endpoints.managementPath({ signal }), []),
  );
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

  // Host, agent and path are what this panel *is*; the estate reads only furnish its
  // Attached column. Requiring all five would let an unreadable Docker socket blank the
  // machine's identity, which is precisely backwards — so the estate is read separately and
  // its absence becomes a stated gap rather than an error screen.
  const core = combine(combine(host, agent), path);
  const estate = {
    interfaces: interfaces.status === 'success' ? interfaces.data.interfaces : null,
    containers: containers.status === 'success' ? containers.data.containers : null,
    units: units.status === 'success' ? units.data.count : null,
  };

  return (
    <div ref={ref} className={styles.container} data-density={density}>
      {/* The plate is rendered unconditionally. A failed read inside a bordered panel is a
          panel reporting a problem; the same text loose on the page background is a broken
          screen, and the difference matters most at the moment something is wrong. */}
      {core.status === 'success' ? (
        <DeviceOverview
          host={core.data[0][0]}
          agent={core.data[0][1]}
          path={core.data[1]}
          interfaces={estate.interfaces}
          containers={estate.containers}
          units={estate.units}
          density={density}
          columns={columnsForWidth(width)}
          fetchedAt={core.fetchedAt}
          onRefresh={refresh}
        />
      ) : (
        <Plate lead className={styles.plate}>
          <PlateHead title="This host" meta="the machine, and what is proven about it" />
          <PlateBody>
            <ResourceView resource={core} what="host" loadingLabel="Reading host…">
              {() => null}
            </ResourceView>
          </PlateBody>
        </Plate>
      )}
    </div>
  );
}

function DeviceOverview({
  host,
  agent,
  path,
  interfaces,
  containers,
  units,
  density,
  columns,
  fetchedAt,
  onRefresh,
}: {
  host: import('@/api/types').Host;
  agent: import('@/api/types').AgentStatus;
  path: import('@/api/types').ManagementPath;
  /** `null` when the estate could not be read — a stated gap, never an assumed emptiness. */
  interfaces: readonly import('@/api/types').NetworkInterface[] | null;
  containers: readonly import('@/api/types').DockerContainer[] | null;
  units: number | null;
  density: 'expanded' | 'compact' | 'minimal';
  columns: number;
  fetchedAt: Date;
  onRefresh: () => void;
}): JSX.Element {
  const nodes = useMemo(
    () =>
      buildTopology({
        host,
        agent,
        path,
        interfaces: interfaces ?? [],
        containers: containers ?? [],
        estateRead: { interfaces: interfaces !== null, containers: containers !== null },
        ...(units !== null ? { unitCount: units } : {}),
      }),
    [host, agent, path, interfaces, containers, units],
  );

  const managed = interfaces?.filter((item) => item.management.state === 'managed').length ?? 0;
  const drifted = interfaces?.filter((item) => item.reconciliation === 'drifted').length ?? 0;
  const running = containers?.filter((item) => item.runtime.state === 'running').length ?? 0;

  // Agent reachability says whether this request could observe the host. It is not a host
  // health verdict: this build has no such contract.
  const observability = agent.reachable
    ? {
        tone: 'good' as const,
        label: 'agent reachable',
        description: 'The agent answered this request. No overall host health is inferred.',
      }
    : {
        tone: 'unknown' as const,
        label: 'not currently observable',
        description:
          'The agent did not answer. What is shown is what LocalPlane last recorded, not what the host is doing now.',
      };

  return (
    <Plate lead className={styles.plate}>
      <PlateHead
        title={host.hostname ?? host.host_id}
        mark={observability}
        meta={
          density === 'minimal'
            ? undefined
            : [
                host.os_pretty_name,
                host.architecture,
                density === 'expanded' ? host.kernel_release : null,
              ]
                .filter(Boolean)
                .join(' · ')
        }
        asOf={fetchedAt.toLocaleTimeString()}
        chips={
          <>
            <StatusPill semantic={observability} size="sm" />
            <StatusPill semantic={freshnessOf(host.freshness)} size="sm" token={host.freshness} />
            {density !== 'minimal' && containers ? (
              <span className={styles.countChip}>
                <b>{running}</b>
                <span>/{containers.length} running</span>
              </span>
            ) : null}
            {density !== 'minimal' && managed > 0 ? (
              <span className={styles.countChip}>
                <b>{managed}</b>
                <span>&nbsp;managed{drifted > 0 ? ` · ${drifted} drifted` : ''}</span>
              </span>
            ) : null}
          </>
        }
      >
        <button type="button" className={styles.action} onClick={onRefresh}>
          Re-read
        </button>
        <Link className={styles.action} to="/network">
          Interfaces ›
        </Link>
      </PlateHead>

      {/* The relationship plane. Not drawn at minimal density — a single column of nodes is
          a list pretending to be a diagram, and the facts are in the list below anyway. */}
      {density !== 'minimal' ? <Topology nodes={nodes} columns={columns} /> : null}

      <PlateBody tight>
        {/* Safety state: present in every mode, as text plus glyph, never behind a
            disclosure and never hover-only. It shares a line with the path sentence, which
            is the thing it qualifies. */}
        <div className={styles.pathRow}>
          <div className={styles.safety}>
          <StatusPill
            semantic={
              agent.reachable
                ? { tone: 'good', label: 'agent reachable', description: 'The agent answered this request.' }
                : {
                    tone: 'bad',
                    label: 'agent unreachable',
                    description:
                      'The agent did not answer. Everything shown is what LocalPlane last recorded.',
                  }
            }
          />
            <StatusPill semantic={managementPathState(path.state)} token={path.state} />
          </div>

          <p className={styles.pathNote}>
          {path.state === 'confirmed' && path.object_name ? (
            <>
              This request arrives over <span className="mono">{path.object_name}</span>. Changing
              that interface is guarded.
            </>
          ) : (
            <>
              LocalPlane cannot tell which interface carries this connection —{' '}
              <code className={styles.code}>{path.reason}</code>. {humanise(path.reason)}.
            </>
            )}
          </p>
        </div>

        {host.identity_gaps.length > 0 || path.missing_evidence.length > 0 ? (
          <div className={styles.gapRow}>
            {host.identity_gaps.length > 0 ? (
              <Gaps items={host.identity_gaps} label="Identity evidence not readable" />
            ) : null}
            {path.missing_evidence.length > 0 ? (
              <Gaps items={path.missing_evidence} label="Management path — missing evidence" />
            ) : null}
          </div>
        ) : null}
      </PlateBody>

      <PlateFoot
        source={
          <>
            {host.identity_basis} · agent {agent.agent?.agent_version ?? 'unreachable'} ·{' '}
            {agent.source === 'live' ? 'probed for this request' : 'last recorded'}
          </>
        }
      />
    </Plate>
  );
}
