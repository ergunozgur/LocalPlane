/**
 * The execution gate.
 *
 * The case that matters is the one measured on a live host: the agent declares
 * `systemd.service.lifecycle` as `available` and `mutating`, and the plan says
 * `not_implemented`. The gate must side with the plan. The fixture is that exact response,
 * captured from a running backend rather than written by hand.
 */
import { describe, expect, it } from 'vitest';
import { buildOffersControl, executionGate, OPERATIONS_WITH_UI_CONTROLS } from './execution';
import type { PlanConfirmation, PlanExecution } from '@/api/types';
import runFixture from '@/test/fixtures/systemd-run.json';

const preview = (runFixture as unknown as { preview: { how: PlanExecution; confirmation: PlanConfirmation } }).preview;

function execution(overrides: Partial<PlanExecution> = {}): PlanExecution {
  return {
    availability: 'available',
    eligibility: 'eligible',
    blockers: [],
    provider: 'linux_network',
    required_capability: 'network.interface.set_mtu',
    capability_declared_by_agent: true,
    note: '',
    ...overrides,
  };
}

describe('a capability is not the gate', () => {
  it('refuses a control for the real systemd plan, whose capability is declared available', () => {
    // Guard the premise: if the fixture stopped exhibiting the trap, this test would be
    // asserting nothing.
    expect(preview.how.capability_declared_by_agent).toBe(true);
    expect(preview.how.availability).toBe('not_implemented');

    const gate = executionGate(preview.how, preview.confirmation);

    expect(gate.state).toBe('not_implemented');
    expect(gate.mayOfferControl).toBe(false);
    expect(gate.capabilityDeclaredByAgent).toBe(true);
  });

  it('surfaces every blocker rather than the first', () => {
    const gate = executionGate(preview.how, preview.confirmation);
    expect(gate.blockers).toContain('execution_not_implemented');
    expect(gate.blockers.length).toBeGreaterThan(1);
  });

  it('reports the required capability without treating it as permission', () => {
    const gate = executionGate(preview.how, preview.confirmation);
    expect(gate.requiredCapability).toBe('systemd.service.lifecycle');
    expect(gate.mayOfferControl).toBe(false);
  });
});

describe('no plan means not assessed', () => {
  it('treats a missing plan as unknown, never as executable', () => {
    for (const value of [null, undefined]) {
      const gate = executionGate(value);
      expect(gate.state).toBe('not_assessed');
      expect(gate.mayOfferControl).toBe(false);
      expect(gate.capabilityDeclaredByAgent).toBeNull();
    }
  });
});

describe('gate states', () => {
  it('opens only for an available, eligible, satisfiable plan', () => {
    const gate = executionGate(execution());
    expect(gate.state).toBe('executable');
    expect(gate.mayOfferControl).toBe(true);
  });

  it('keeps guarded as its own answer, neither eligible nor blocked', () => {
    const gate = executionGate(execution({ eligibility: 'guarded' }));
    expect(gate.state).toBe('guarded');
    expect(gate.mayOfferControl).toBe(true);
  });

  it('refuses a control when the plan is blocked', () => {
    const gate = executionGate(execution({ eligibility: 'blocked', blockers: ['preview_stale'] }));
    expect(gate.state).toBe('blocked');
    expect(gate.mayOfferControl).toBe(false);
  });

  it('refuses a control when no confirmation could ever be given', () => {
    // An eligible plan whose confirmation is unsatisfiable still cannot be applied.
    const gate = executionGate(execution(), {
      required: true,
      method: 'typed',
      source: 'policy',
      reasons: [],
      policy: '',
      token_issued: false,
      satisfied: false,
      satisfiable: false,
      unsatisfiable_reason: 'execution_not_implemented',
    });
    expect(gate.mayOfferControl).toBe(false);
  });

  it('refuses a control when the executor exists but is unusable here', () => {
    const gate = executionGate(execution({ availability: 'unavailable' }));
    expect(gate.state).toBe('blocked');
    expect(gate.mayOfferControl).toBe(false);
  });

  it('treats an unrecognised eligibility as blocked rather than open', () => {
    const gate = executionGate(execution({ eligibility: 'something_new' }));
    expect(gate.mayOfferControl).toBe(false);
  });
});

describe('this build offers no write controls', () => {
  it('declares an empty control set', () => {
    expect(OPERATIONS_WITH_UI_CONTROLS).toHaveLength(0);
  });

  it('offers no control for any operation in the closed vocabulary', () => {
    for (const operation of [
      'network.interface.reconcile_mtu',
      'docker.container.start',
      'docker.container.stop',
      'docker.container.restart',
      'systemd.service.start',
      'systemd.service.stop',
      'systemd.service.restart',
    ]) {
      expect(buildOffersControl(operation)).toBe(false);
    }
  });
});
