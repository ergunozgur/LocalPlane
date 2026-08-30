/**
 * The host indicator in the app bar.
 *
 * The host sits in the bar itself — a status mark, the name, and the address it is
 * reached at — rather than on a shelf below it. That is the right place for it: every number
 * on every screen is a claim about *this* host, read through *this* agent, and the bar is
 * where a claim's subject belongs.
 *
 * The agent's reachability rides alongside, because a page of interface data with an
 * unreachable agent is a page of history, and this is what says so.
 */
import { useCallback } from 'react';
import { Link } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { combine, useResource } from '@/hooks/useResource';
import { StatusDot } from '@/components/primitives/Plate';
import { freshness as freshnessOf, notKnown } from '@/domain/vocabulary';
import { Menu, MenuLabel, MenuSeparator } from './Menu';
import styles from './HostScope.module.css';

export function HostScope(): JSX.Element {
  const { resource: host } = useResource(
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

  const combined = combine(combine(host, agent), path);

  if (combined.status !== 'success') {
    return (
      <div className={styles.host} data-state="unknown">
        <StatusDot semantic={notKnown()} />
        <span className={styles.name}>
          {combined.status === 'failed' ? 'host unknown' : 'reading host…'}
        </span>
        {combined.status === 'failed' ? (
          <span className={styles.address}>{combined.error.code ?? combined.error.kind}</span>
        ) : null}
      </div>
    );
  }

  const [[hostData, agentData], pathData] = combined.data;

  // The mark is about whether this host can currently be seen at all — the single fact that
  // qualifies everything else on screen. It is never `good` when the agent is silent.
  const mark = !agentData.reachable
    ? {
        tone: 'bad' as const,
        label: 'agent unreachable',
        description:
          'The agent did not answer. Everything shown is what LocalPlane last recorded, not what the host is doing now.',
      }
    : hostData.freshness === 'current'
      ? {
          tone: 'good' as const,
          label: 'observed',
          description: 'The agent answered and this host was observed recently enough to rely on.',
        }
      : freshnessOf(hostData.freshness);

  return (
    <Menu
      label="Host"
      align="left"
      className={styles.trigger}
      trigger={
        <span className={styles.host} data-state={agentData.reachable ? 'ok' : 'bad'}>
          <StatusDot semantic={mark} />
          <span className={styles.name} title={hostData.host_id}>
            {hostData.hostname ?? hostData.host_id}
          </span>
          <span className={styles.address} title={pathData.transport.reason ?? undefined}>
            {pathData.transport.peer_address ?? '—'}
          </span>
          <span className={styles.caret} aria-hidden="true">
            ▾
          </span>
        </span>
      }
    >
      <MenuLabel>This host</MenuLabel>
      <Link to="/" className={styles.item}>
        <span className={styles.itemTitle}>
          <StatusDot semantic={mark} />
          {hostData.hostname ?? hostData.host_id}
          <span className={styles.itemTag}>local</span>
        </span>
        <span className={styles.itemDescription}>
          {[hostData.os_pretty_name, agentData.agent ? `agent ${agentData.agent.agent_version}` : null]
            .filter(Boolean)
            .join(' · ')}
          {agentData.reachable ? ' · observing' : ' · agent unreachable'}
        </span>
      </Link>

      <MenuSeparator />

      {/* The design direction carries a fleet seam here. LocalPlane manages one host in
          this build, and the entry says so rather than offering an action that would do
          nothing. */}
      <MenuLabel>Fleet</MenuLabel>
      <div className={styles.deferred}>
        <span className={styles.itemTitle}>
          <span className={styles.deferredMark} aria-hidden="true" />
          More than one host
          <span className={styles.itemTag}>not in this build</span>
        </span>
        <span className={styles.itemDescription}>
          This build observes and operates a single host. Cross-host inventory has no backend
          contract yet, so nothing here can add one.
        </span>
      </div>
    </Menu>
  );
}
