/**
 * Overview.
 *
 * The home view reads top to bottom: **attention → the machine → supporting depth.** The
 * rail comes first because what needs looking at is read before the machine is; the widget
 * grid follows.
 *
 * Attention is assessed independently per source: a recovery hold remains visible even when
 * the interface or systemd read behind it fails. The quiet statement is limited to what was
 * actually checked, and the quiet claim is guarded until every required source has
 * been successfully assessed.
 */
import { useCallback } from 'react';
import { Link } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { DashboardGrid } from '@/dashboard/DashboardGrid';
import { DEFAULT_TEMPLATE } from '@/dashboard/registry';
import { AttentionRail, type AttentionItem } from '@/components/semantic/AttentionRail';
import {
  inventoryCoverage,
  inventoryCoverageLabel,
  type InventoryCoverage,
} from '@/domain/inventory';
import { formatRelative } from '@/domain/format';

const FINDINGS_LIMIT = 50;

interface DomainCoverage {
  label: string;
  to: string;
  coverage: InventoryCoverage;
}

function coveragePhrase(item: DomainCoverage): string {
  const { coverage } = item;
  const counts = coverage.counts;
  const age = coverage.latestObservedAt ? formatRelative(coverage.latestObservedAt) : null;
  const parts: string[] = [];
  if (coverage.status === 'ok') {
    parts.push(`${counts.latestSighted} latest-sighted`);
    if (counts.retainedNotSeen > 0) parts.push(`${counts.retainedNotSeen} retained`);
    if (counts.membershipUnknown > 0) parts.push(`${counts.membershipUnknown} unknown membership`);
  }
  const base = inventoryCoverageLabel(coverage.status);
  const detail = parts.length > 0 ? ` (${parts.join(', ')})` : '';
  const ageClause = age ? ` · ${age}` : '';
  return `${base}${detail}${ageClause}`;
}

function CoverageLine({ items }: { items: readonly DomainCoverage[] }): JSX.Element {
  return (
    <>
      {items.map((item, index) => (
        <span key={item.label}>
          {index > 0 ? ' · ' : ''}
          <Link to={item.to}>{item.label}</Link>
          {' '}
          {coveragePhrase(item)}
        </span>
      ))}
    </>
  );
}

export function Overview(): JSX.Element {
  const layout = DEFAULT_TEMPLATE.layout;

  const { resource: interfaces } = useResource(
    'interfaces',
    useCallback((signal) => endpoints.interfaces({ signal }), []),
  );
  const { resource: findings } = useResource(
    'findings:open',
    useCallback(
      (signal) => endpoints.findings({ status: 'open', limit: FINDINGS_LIMIT }, { signal }),
      [],
    ),
  );
  const { resource: containers } = useResource(
    'containers',
    useCallback((signal) => endpoints.containers({ signal }), []),
  );
  const { resource: units } = useResource(
    'systemd-units',
    useCallback((signal) => endpoints.systemdUnits({ signal }), []),
  );
  const { resource: recoveryHolds } = useResource(
    'changes:recovery-holds',
    useCallback((signal) => endpoints.recoveryHolds({ signal }), []),
  );

  const interfaceList = interfaces.status === 'success' ? interfaces.data.interfaces : [];
  const findingList = findings.status === 'success' ? findings.data.findings : [];
  const containerList = containers.status === 'success' ? containers.data.containers : [];
  const unitList = units.status === 'success' ? units.data.units : [];
  const holdList = recoveryHolds.status === 'success' ? (recoveryHolds.data?.changes ?? []) : [];

  const interfaceCoverage = inventoryCoverage(interfaces, interfaceList);
  const containerCoverage = inventoryCoverage(containers, containerList);
  const unitCoverage = inventoryCoverage(units, unitList);

  const domainCoverage: DomainCoverage[] = [
    { label: 'Network', to: '/network', coverage: interfaceCoverage },
    { label: 'Workloads', to: '/workloads', coverage: containerCoverage },
    { label: 'System', to: '/system', coverage: unitCoverage },
  ];

  const drifted: AttentionItem[] = interfaceList
    .filter((item) => item.reconciliation === 'drifted')
    .map((item) => ({
      id: item.object_id,
      name: item.name,
      detail: 'drifted',
      to: `/network/${item.object_id}`,
    }));

  const findingItems: AttentionItem[] = findingList.map((finding) => ({
    id: finding.finding_id,
    name: finding.object_name,
    detail: finding.finding_type,
    to: `/operations/findings/${finding.finding_id}`,
  }));

  const holds: AttentionItem[] = holdList.map((change) => ({
    id: change.change_id,
    name: change.object_name,
    detail: change.operation,
    to: `/operations/changes/${change.change_id}`,
  }));

  // Only latest-sighted failed units are current attention. Retained historical failures
  // stay reachable from System; they are not proof of current state.
  const serviceFailures: AttentionItem[] = unitList
    .filter((unit) => unit.active_state === 'failed' && unit.observed_in_latest_sweep === true)
    .map((unit) => ({
      id: unit.object_id,
      name: unit.canonical_id,
      detail: `last recorded failed · ${unit.observation?.freshness ?? 'unknown'}`,
      to: `/system/${unit.object_id}`,
    }));
  const retainedFailures = unitList.filter(
    (unit) => unit.active_state === 'failed' && unit.observed_in_latest_sweep === false,
  );

  const observedCount = interfaceList.length + containerList.length + unitList.length;
  const managed = interfaceList.filter((item) => item.management.state === 'managed');
  const unsettledManaged = managed.filter(
    (item) => item.reconciliation !== 'in_sync' && item.reconciliation !== 'drifted',
  );

  const holdTotal = recoveryHolds.status === 'success' ? (recoveryHolds.data?.total_count ?? null) : null;
  const holdTruncated = recoveryHolds.status === 'success' ? (recoveryHolds.data?.truncated ?? false) : false;

  const readFailures: string[] = [];
  if (interfaces.status === 'failed') readFailures.push('interfaces');
  if (findings.status === 'failed') readFailures.push('findings');
  if (containers.status === 'failed') readFailures.push('containers');
  if (units.status === 'failed') readFailures.push('units');
  if (recoveryHolds.status === 'failed') readFailures.push('recovery holds');

  const inventoryAssessed =
    interfaceCoverage.status === 'ok' &&
    containerCoverage.status === 'ok' &&
    unitCoverage.status === 'ok';
  const findingsAssessed = findings.status === 'success' && !findings.refreshing;
  const holdsAssessed = recoveryHolds.status === 'success' && !recoveryHolds.refreshing && holdTotal !== null;
  const allAssessed = inventoryAssessed && findingsAssessed && holdsAssessed;

  const unresolved = !allAssessed || unsettledManaged.length > 0;

  const findingsPhrase =
    findings.status === 'loading' || (findings.status === 'success' && findings.refreshing)
      ? 'findings reading'
      : findings.status === 'failed'
        ? 'findings read failed'
        : findingItems.length === 0
          ? `no open findings${findingList.length === FINDINGS_LIMIT ? ` (first ${FINDINGS_LIMIT} checked)` : ''}`
          : `${findingList.length === FINDINGS_LIMIT ? 'at least ' : ''}${findingItems.length} open finding${findingItems.length === 1 ? '' : 's'}${findingList.length === FINDINGS_LIMIT ? ` (first ${FINDINGS_LIMIT} shown)` : ''}`;

  const holdsPhrase =
    recoveryHolds.status === 'loading' || (recoveryHolds.status === 'success' && recoveryHolds.refreshing)
      ? 'holds reading'
      : recoveryHolds.status === 'failed'
        ? 'holds read failed'
        : holdTotal !== null
          ? `${holdTotal} unresolved hold${holdTotal === 1 ? '' : 's'}${
              holdTruncated ? ` (${holds.length} shown, truncated)` : ''
            }`
          : 'holds not read';

  const unitsPhrase =
    unitCoverage.status === 'ok'
      ? serviceFailures.length > 0
        ? `${serviceFailures.length} failed unit${serviceFailures.length === 1 ? '' : 's'} in latest inventory`
        : 'no failed units in latest inventory'
      : `units ${inventoryCoverageLabel(unitCoverage.status)}`;

  const managedPhrase =
    managed.length > 0
      ? `${managed.length} managed${
          unsettledManaged.length > 0
            ? `, ${unsettledManaged.length} unsettled`
            : interfaceCoverage.status === 'ok'
              ? ', all in sync'
              : ''
        }`
      : interfaceCoverage.status === 'ok'
        ? 'none managed, so nothing can drift'
        : 'management state not assessed';

  const quietSummary =
    !allAssessed && readFailures.length === 0 ? (
      <>
        <CoverageLine items={domainCoverage} />
        {' · '}
        {findingsPhrase}
        {' · '}
        {holdsPhrase}
      </>
    ) : (
      <>
        {observedCount.toLocaleString()} stored object records · Latest inventory:{' '}
        <CoverageLine items={domainCoverage} />
        {' · '}
        {managedPhrase}
        {' · '}
        {holdsPhrase}
        {' · '}
        {findingsPhrase}
        {' · '}
        {unitsPhrase}
        {retainedFailures.length > 0
          ? ` · ${retainedFailures.length} retained failed unit${retainedFailures.length === 1 ? '' : 's'} (historical)`
          : null}
        {readFailures.length > 0 ? ` · could not read ${readFailures.join(', ')}` : null}
      </>
    );

  return (
    <>
      <h1 className="visually-hidden">Overview</h1>

      <AttentionRail
        drifted={drifted}
        findings={findingItems}
        holds={holds}
        serviceFailures={serviceFailures}
        unresolved={unresolved}
        unresolvedSummary={
          unsettledManaged.length > 0 ? (
            <>
              {unsettledManaged.length} managed object
              {unsettledManaged.length === 1 ? ' has' : 's have'} an unknown or in-progress
              reconciliation result. That is not an in-sync result.
              {readFailures.length > 0 ? <> Also could not read {readFailures.join(', ')}.</> : null}
            </>
          ) : readFailures.length > 0 ? (
            <>Could not read {readFailures.join(', ')}. What is shown above is still real.</>
          ) : (
            <>
              Recorded coverage:{' '}
              <CoverageLine items={domainCoverage} />
              {' · '}
              {findingsPhrase}
              {' · '}
              {holdsPhrase}
            </>
          )
        }
        quietSummary={quietSummary}
        holdTotal={holdTotal}
        holdTruncated={holdTruncated}
        retainedFailures={retainedFailures.map((unit) => ({
          id: unit.object_id,
          name: unit.canonical_id,
          detail: 'failed',
          to: `/system/${unit.object_id}`,
        }))}
      />

      <DashboardGrid layout={layout} />
    </>
  );
}
