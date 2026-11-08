/**
 * Generic inventory coverage vocabulary.
 *
 * Every inventory list (network interfaces, Docker containers, systemd units) carries the
 * same pattern: a `last_sweep` envelope and rows that each say whether they were sighted in
 * the latest sweep and how fresh that sighting is. The UI must not mix those scopes up:
 * latest-sighted, retained, and unknown-membership are different claims, and the envelope
 * itself can be missing, partial, failed, stale, or unknown.
 */
import type { Observation, Sweep } from '@/api/types';
import type { Resource } from '@/hooks/useResource';
import type { Semantic } from './vocabulary';

export interface MembershipItem {
  observed_in_latest_sweep: boolean | null;
  observation?: { freshness: string; observed_at: string; age_seconds: number | null; fidelity?: string; gaps?: string[] } | null;
}

export interface MembershipCounts {
  /** Sighted in the latest recorded inventory sweep. */
  readonly latestSighted: number;
  /** Retained from an earlier sweep but not sighted in the latest one. */
  readonly retainedNotSeen: number;
  /** No sweep membership information is recorded. */
  readonly membershipUnknown: number;
}

/**
 * Membership is an inventory-coverage fact, not runtime state. A retained record that was
 * not seen can be an inspection gap; it does not establish deletion.
 */
export function inventoryMembership(value: boolean | null | undefined): Semantic {
  if (value === true) {
    return {
      tone: 'good',
      label: 'seen in latest inventory',
      description:
        'This record was included in the latest recorded inventory sweep. That is membership, not a claim that it is still present right now.',
    };
  }
  if (value === false) {
    return {
      tone: 'warn',
      label: 'not seen in latest inventory',
      description:
        'This retained record was not included in the latest inventory sweep. This can include an unreadable inspection and does not prove deletion.',
    };
  }
  return {
    tone: 'unknown',
    label: 'membership unknown',
    description:
      'No recorded inventory establishes whether this retained record was included in the latest sweep.',
  };
}

export function membershipCounts<T extends MembershipItem>(items: readonly T[]): MembershipCounts {
  let latestSighted = 0;
  let retainedNotSeen = 0;
  let membershipUnknown = 0;
  for (const item of items) {
    if (item.observed_in_latest_sweep === true) {
      latestSighted += 1;
    } else if (item.observed_in_latest_sweep === false) {
      retainedNotSeen += 1;
    } else {
      membershipUnknown += 1;
    }
  }
  return { latestSighted, retainedNotSeen, membershipUnknown };
}

export type InventoryCoverageStatus =
  | 'loading'
  | 'failed'
  | 'absent'
  | 'failed_sweep'
  | 'partial_sweep'
  | 'unknown_sweep'
  | 'stale'
  | 'unknown'
  | 'refreshing'
  | 'ok';

export interface InventoryCoverage {
  readonly status: InventoryCoverageStatus;
  readonly sweep: Sweep | null;
  readonly counts: MembershipCounts;
  /** The freshest `observed_at` among latest-sighted items, when one exists. */
  readonly latestObservedAt: string | null;
  /** The worst freshness among the sweep and latest-sighted items. */
  readonly freshness: string | null;
  /** True while a re-read of this resource is in flight; prior evidence is still shown. */
  readonly refreshing: boolean;
}

function itemObservation(item: MembershipItem): Observation | null {
  const o = item.observation;
  if (!o) return null;
  return o as Observation;
}

function worstFreshness(a: string | null, b: string | null): string | null {
  if (a === 'never_observed' || b === 'never_observed') return 'never_observed';
  if (a === 'stale' || b === 'stale') return 'stale';
  if (a === 'current' || b === 'current') return 'current';
  return a ?? b;
}

function isKnownSweepStatus(status: string): boolean {
  return status === 'ok' || status === 'partial' || status === 'failed';
}

function observationFreshness(obs: Observation | null): string | null {
  if (!obs) return 'never_observed';
  const freshness = obs.freshness;
  if (freshness === 'current' || freshness === 'stale' || freshness === 'never_observed') {
    return freshness;
  }
  return 'never_observed';
}

/**
 * The sweep's own freshness is authoritative. It is computed by the backend from
 * completed_at and the configured TTL, so the client must not apply its own TTL guess.
 * Missing sweep currency metadata is reported as unknown currency.
 */
function sweepFreshness(sweep: Sweep): string | null {
  const freshness = sweep.freshness;
  if (freshness === 'current' || freshness === 'stale' || freshness === 'never_observed') {
    return freshness;
  }
  return null;
}

/**
 * Summarise one inventory stream: what the envelope says, how many rows are latest-sighted
 * vs retained, and whether the latest sighting is stale. A list with no `last_sweep` means
 * nobody has looked; a sweep with status `failed` means the look itself failed.
 *
 * Sweep freshness is authoritative and independent of row freshness: an old full inventory
 * with one fresh targeted row is still stale, and a fresh empty sweep is ok while a stale
 * empty sweep is stale. While a re-read is pending the status is never `ok`, so the UI does
 * not present a new all-clear over still-visible prior evidence.
 */
export function inventoryCoverage<T extends MembershipItem>(
  resource: Resource<{ last_sweep: Sweep | null }>,
  items: readonly T[],
): InventoryCoverage {
  const counts = membershipCounts(items);
  const refreshing = resource.status === 'success' && resource.refreshing;

  if (resource.status === 'loading') {
    return { status: 'loading', sweep: null, counts, latestObservedAt: null, freshness: null, refreshing: false };
  }
  if (resource.status === 'failed') {
    return { status: 'failed', sweep: null, counts, latestObservedAt: null, freshness: null, refreshing: false };
  }
  if (resource.status !== 'success') {
    return { status: 'absent', sweep: null, counts, latestObservedAt: null, freshness: null, refreshing: false };
  }

  const sweep = resource.data.last_sweep;
  if (!sweep) {
    return { status: 'absent', sweep: null, counts, latestObservedAt: null, freshness: null, refreshing };
  }
  if (sweep.status === 'failed') {
    return { status: 'failed_sweep', sweep, counts, latestObservedAt: null, freshness: null, refreshing };
  }

  const envelopeFreshness = sweepFreshness(sweep);
  const envelopeKnown = envelopeFreshness !== null;

  let latestObservedAt: string | null = null;
  let rowFreshness: string | null = null;
  let partialEvidence = false;
  for (const item of items) {
    if (item.observed_in_latest_sweep !== true) continue;
    const o = itemObservation(item);
    rowFreshness = worstFreshness(rowFreshness, observationFreshness(o));
    if (o && (!latestObservedAt || o.observed_at > latestObservedAt)) {
      latestObservedAt = o.observed_at;
    }
    if (o && (o.fidelity !== 'complete' || (o.gaps && o.gaps.length > 0))) {
      partialEvidence = true;
    }
  }

  // The sweep's own freshness is authoritative. If the envelope is stale, every row it
  // produced is stale, even a row that reports its own freshness as current.
  const freshness = worstFreshness(envelopeFreshness, rowFreshness);

  // If no latest-sighted items carried their own observation, fall back to the sweep time.
  if (!latestObservedAt && sweep.completed_at) {
    latestObservedAt = sweep.completed_at;
  }

  if (!isKnownSweepStatus(sweep.status)) {
    return { status: 'unknown_sweep', sweep, counts, latestObservedAt, freshness, refreshing };
  }
  if (sweep.status === 'partial' || partialEvidence) {
    return { status: 'partial_sweep', sweep, counts, latestObservedAt, freshness, refreshing };
  }
  if (!envelopeKnown) {
    // The backend did not provide sweep currency metadata, so currency is unknown.
    // A fresh targeted row cannot make an old inventory current, and the client must not
    // guess a TTL from completed_at.
    return { status: 'unknown', sweep, counts, latestObservedAt, freshness: null, refreshing };
  }
  if (freshness === null) {
    return { status: 'unknown', sweep, counts, latestObservedAt, freshness, refreshing };
  }
  if (freshness === 'never_observed') {
    return { status: 'unknown', sweep, counts, latestObservedAt, freshness, refreshing };
  }
  if (freshness === 'stale') {
    return { status: 'stale', sweep, counts, latestObservedAt, freshness, refreshing };
  }
  if (counts.membershipUnknown > 0) {
    return { status: 'unknown', sweep, counts, latestObservedAt, freshness, refreshing };
  }
  if (refreshing) {
    return { status: 'refreshing', sweep, counts, latestObservedAt, freshness, refreshing };
  }
  return { status: 'ok', sweep, counts, latestObservedAt, freshness, refreshing };
}

/**
 * A short phrase for the coverage status, suitable for an attention summary.
 */
export function inventoryCoverageLabel(status: InventoryCoverageStatus): string {
  switch (status) {
    case 'loading':
      return 'reading inventory';
    case 'failed':
      return 'inventory read failed';
    case 'absent':
      return 'no inventory recorded';
    case 'failed_sweep':
      return 'latest inventory failed';
    case 'partial_sweep':
      return 'latest inventory partial';
    case 'unknown_sweep':
      return 'latest inventory status unknown';
    case 'stale':
      return 'latest inventory stale';
    case 'unknown':
      return 'latest inventory unknown';
    case 'refreshing':
      return 'latest inventory refreshing';
    case 'ok':
      return 'latest inventory ok';
  }
}
