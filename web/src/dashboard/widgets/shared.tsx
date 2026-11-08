/**
 * Pieces shared by the list-shaped widgets.
 *
 * Each of these widgets is a small window onto a full surface: a few rows, a count, and a
 * link to the page that holds the rest. They share this file so the shape stays consistent
 * and so a change to how "and N more" reads happens once.
 */
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { ProviderIssue, ProviderReadingSummary, Sweep } from '@/api/types';
import { StatusPill } from '@/components/semantic/StatusPill';
import { PlateFoot } from '@/components/primitives/Plate';
import { sweepStatus } from '@/domain/vocabulary';
import { formatRelative } from '@/domain/format';
import { Degraded } from '@/components/states/SurfaceState';
import styles from './shared.module.css';

/** The "All 32 ›" affordance in a widget head. */
export function WidgetAction({ to, children }: { to: string; children: ReactNode }): JSX.Element {
  return (
    <Link to={to} className={styles.action}>
      {children}
    </Link>
  );
}

/**
 * The evidence footer for a list widget.
 *
 * Names the sweep the rows came from. A partial or failed sweep is the difference between
 * "there are three of these" and "three is what could be read", and a list without that
 * caption invites the first reading — so a sweep that did not complete is stated as a
 * caveat rather than as a footnote.
 */
export function SweepFoot({ sweep, subject }: { sweep: Sweep | null; subject: string }): JSX.Element {
  if (!sweep) {
    return (
      <PlateFoot label="evidence">
        <span className={styles.noSweep}>
          nothing has read {subject} — an empty list above means nobody looked
        </span>
      </PlateFoot>
    );
  }
  return (
    <PlateFoot
      source={
        <>
          {sweep.provider} {sweep.provider_version} · {sweep.capability} ·{' '}
          {formatRelative(sweep.completed_at) ?? 'time unknown'}
          {sweep.status !== 'ok' ? ` · ${sweep.status}` : ''}
          {sweep.issues.length > 0 ? ` · ${sweep.issues.length} issue(s)` : ''}
        </>
      }
    />
  );
}

/** A caveat shown in the body when the sweep behind a list did not complete. */
export function SweepCaveat({ sweep }: { sweep: Sweep | null }): ReactNode {
  if (!sweep || (sweep.status === 'ok' && sweep.issues.length === 0)) return null;
  return (
    <Degraded title="Observation was not complete">
      <StatusPill semantic={sweepStatus(sweep.status)} size="sm" token={sweep.status} />{' '}
      {sweep.issues.length > 0 ? `${sweep.issues.length} issue(s). ` : ''}
      {sweep.missing.length > 0 ? `${sweep.missing.length} reported absent. ` : ''}
      What is shown is what could be read.
    </Degraded>
  );
}

/** "and N more", stated where a widget shows a window onto a longer list. */
export function Remainder({ count }: { count: number }): JSX.Element | null {
  if (count <= 0) return null;
  return <span className={styles.remainder}>and {count} more</span>;
}

/** A collapsible list of provider issues shared by observation outcome banners. */
export function IssueList({ issues, label }: { issues: ProviderIssue[]; label: string }): JSX.Element | null {
  if (issues.length === 0) return null;
  const visible = issues.slice(0, 3);
  return (
    <details className={styles.issues}>
      <summary>{label} ({issues.length})</summary>
      <ul>
        {visible.map((issue, index) => (
          <li key={`${issue.source}-${issue.code}-${index}`}>
            <code>{issue.source}/{issue.code}</code> {issue.message}
          </li>
        ))}
      </ul>
      {issues.length > visible.length ? <span>Only the first {visible.length} issues are shown.</span> : null}
    </details>
  );
}

/** A collapsible list of provider-read summaries, so ownership provider failures are visible. */
export function ProviderList({ providers, label }: { providers: ProviderReadingSummary[] | undefined; label: string }): JSX.Element | null {
  if (!providers || providers.length === 0) return null;
  const visible = providers.slice(0, 5);
  return (
    <details className={styles.issues}>
      <summary>{label} ({providers.length})</summary>
      <ul>
        {visible.map((provider, index) => (
          <li key={`${provider.provider}-${provider.source}-${index}`}>
            <code>{provider.provider}</code> · {provider.source} ·{' '}
            <span className={provider.status === 'ok' ? styles.okProvider : styles.badProvider}>{provider.status}</span>
            {provider.reason ? ` — ${provider.reason}` : ''}
          </li>
        ))}
      </ul>
      {providers.length > visible.length ? <span>Only the first {visible.length} providers are shown.</span> : null}
    </details>
  );
}
