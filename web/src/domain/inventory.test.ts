import { describe, expect, it } from 'vitest';
import { inventoryCoverage, inventoryCoverageLabel, membershipCounts } from './inventory';
import type { Resource } from '@/hooks/useResource';
import type { Sweep } from '@/api/types';

const sweep: Sweep = {
  sweep_id: 'swp_1',
  capability: 'test.observe',
  scope: 'inventory',
  provider: 'test',
  provider_version: '1',
  status: 'ok',
  started_at: '2026-08-27T21:47:00Z',
  completed_at: '2026-08-27T21:47:01Z',
  received_at: '2026-08-27T21:47:01Z',
  object_count: 1,
  missing: [],
  issues: [],
  agent_instance_id: 'agent_1',
  freshness: 'current',
  age_seconds: 0,
};

function successList<T extends { last_sweep: Sweep | null }>(
  data: T,
  refreshing = false,
): Resource<T> {
  return { status: 'success', data, fetchedAt: new Date('2026-08-27T21:47:02Z'), refreshing };
}

function item(latest: boolean | null, freshness = 'current', fidelity = 'complete', gaps: string[] = []): {
  observed_in_latest_sweep: boolean | null;
  observation: { freshness: string; observed_at: string; age_seconds: number | null; fidelity: string; gaps: string[] };
} {
  return {
    observed_in_latest_sweep: latest,
    observation: {
      freshness,
      observed_at: '2026-08-27T21:47:01Z',
      age_seconds: 3,
      fidelity,
      gaps,
    },
  };
}

describe('membershipCounts', () => {
  it('counts latest, retained and unknown membership', () => {
    expect(membershipCounts([item(true), item(false), item(null)])).toEqual({
      latestSighted: 1,
      retainedNotSeen: 1,
      membershipUnknown: 1,
    });
  });
});

describe('inventoryCoverage', () => {
  it('returns loading while the resource is loading', () => {
    const resource: Resource<{ last_sweep: Sweep | null }> = { status: 'loading' };
    expect(inventoryCoverage(resource, [item(true)]).status).toBe('loading');
  });

  it('returns failed when the read itself failed', () => {
    const resource: Resource<{ last_sweep: Sweep | null }> = {
      status: 'failed',
      error: new Error('network') as never,
      retry: () => {},
    };
    expect(inventoryCoverage(resource, [item(true)]).status).toBe('failed');
  });

  it('returns absent when there is no sweep', () => {
    const resource = successList({ last_sweep: null });
    expect(inventoryCoverage(resource, []).status).toBe('absent');
  });

  it('returns failed_sweep when the latest sweep failed', () => {
    const resource = successList({ last_sweep: { ...sweep, status: 'failed' } });
    expect(inventoryCoverage(resource, []).status).toBe('failed_sweep');
  });

  it('returns unknown_sweep for an unrecognized sweep status', () => {
    const resource = successList({ last_sweep: { ...sweep, status: 'provider_new_status' } });
    expect(inventoryCoverage(resource, [item(true)]).status).toBe('unknown_sweep');
  });

  it('returns partial_sweep when the envelope is partial', () => {
    const resource = successList({ last_sweep: { ...sweep, status: 'partial' } });
    expect(inventoryCoverage(resource, [item(true)]).status).toBe('partial_sweep');
  });

  it('returns partial_sweep when a latest-sighted observation has partial fidelity or gaps', () => {
    const resource = successList({ last_sweep: sweep });
    expect(inventoryCoverage(resource, [item(true, 'current', 'partial', [])]).status).toBe('partial_sweep');
    expect(inventoryCoverage(resource, [item(true, 'current', 'complete', ['field'])]).status).toBe('partial_sweep');
  });

  it('returns stale when the latest-sighted observation freshness is stale', () => {
    const resource = successList({ last_sweep: sweep });
    expect(inventoryCoverage(resource, [item(true, 'stale')]).status).toBe('stale');
  });

  it('returns unknown when the latest-sighted observation is missing or never_observed', () => {
    const resource = successList({ last_sweep: sweep });
    expect(inventoryCoverage(resource, [item(true, 'never_observed')]).status).toBe('unknown');
    expect(inventoryCoverage(resource, [{ observed_in_latest_sweep: true, observation: null }]).status).toBe('unknown');
  });

  it('returns unknown when some rows have unknown membership even on an ok sweep', () => {
    const resource = successList({ last_sweep: sweep });
    expect(inventoryCoverage(resource, [item(true), item(null)]).status).toBe('unknown');
  });

  it('returns ok for a complete current latest-sighted sweep', () => {
    const resource = successList({ last_sweep: sweep });
    const coverage = inventoryCoverage(resource, [item(true)]);
    expect(coverage.status).toBe('ok');
    expect(coverage.latestObservedAt).toBe('2026-08-27T21:47:01Z');
  });

  it('returns ok for a fresh empty sweep', () => {
    const resource = successList({ last_sweep: { ...sweep, freshness: 'current', age_seconds: 0 } });
    const coverage = inventoryCoverage(resource, []);
    expect(coverage.status).toBe('ok');
    expect(coverage.latestObservedAt).toBe('2026-08-27T21:47:01Z');
  });

  it('returns stale for a stale empty sweep', () => {
    const resource = successList({ last_sweep: { ...sweep, freshness: 'stale', age_seconds: 600 } });
    const coverage = inventoryCoverage(resource, []);
    expect(coverage.status).toBe('stale');
  });

  it('treats an old full inventory with a fresh targeted row as stale', () => {
    const resource = successList({ last_sweep: { ...sweep, freshness: 'stale', age_seconds: 600 } });
    const coverage = inventoryCoverage(resource, [item(true, 'current')]);
    expect(coverage.status).toBe('stale');
    expect(coverage.freshness).toBe('stale');
  });

  it('returns unknown currency when sweep freshness metadata is missing', () => {
    const { freshness: _f, age_seconds: _a, ...sweepWithoutCurrency } = sweep;
    const resource = successList({ last_sweep: sweepWithoutCurrency as unknown as Sweep });
    expect(inventoryCoverage(resource, []).status).toBe('unknown');
    expect(inventoryCoverage(resource, [item(true, 'current')]).status).toBe('unknown');
  });

  it('returns refreshing when a re-read is pending and no worse condition applies', () => {
    const resource = successList({ last_sweep: sweep }, true);
    const coverage = inventoryCoverage(resource, [item(true)]);
    expect(coverage.status).toBe('refreshing');
    expect(coverage.refreshing).toBe(true);
  });

  it('does not downgrade real problems to refreshing during a re-read', () => {
    const stale = successList({ last_sweep: { ...sweep, freshness: 'stale', age_seconds: 600 } }, true);
    expect(inventoryCoverage(stale, [item(true)]).status).toBe('stale');

    const partial = successList({ last_sweep: { ...sweep, status: 'partial' } }, true);
    expect(inventoryCoverage(partial, [item(true)]).status).toBe('partial_sweep');

    const unknownMembership = successList({ last_sweep: sweep }, true);
    expect(inventoryCoverage(unknownMembership, [item(null)]).status).toBe('unknown');
  });
});

describe('inventoryCoverageLabel', () => {
  it('covers every status', () => {
    const statuses: ReturnType<typeof inventoryCoverage>['status'][] = [
      'loading', 'failed', 'absent', 'failed_sweep', 'partial_sweep', 'unknown_sweep', 'stale', 'unknown', 'refreshing', 'ok',
    ];
    for (const status of statuses) {
      expect(inventoryCoverageLabel(status).length).toBeGreaterThan(0);
    }
  });
});
