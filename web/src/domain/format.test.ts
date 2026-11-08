import { expect, it, describe } from 'vitest';
import { addressLifetimeState, formatAddressLifetime } from './format';

describe('addressLifetimeState', () => {
  it('trusts explicit state values when they are consistent', () => {
    expect(addressLifetimeState('infinite', null)).toBe('infinite');
    expect(addressLifetimeState('infinite', undefined)).toBe('infinite');
    expect(addressLifetimeState('finite', 3600)).toBe('finite');
    expect(addressLifetimeState('finite', 0)).toBe('finite');
    expect(addressLifetimeState('unknown', 3600)).toBe('unknown');
  });

  it('treats contradictory finite states without valid seconds as unknown', () => {
    expect(addressLifetimeState('finite', undefined)).toBe('unknown');
    expect(addressLifetimeState('finite', null)).toBe('unknown');
    expect(addressLifetimeState('finite', -1)).toBe('unknown');
    expect(addressLifetimeState('finite', 4294967295)).toBe('unknown');
    expect(addressLifetimeState('finite', 3.14)).toBe('unknown');
  });

  it('does not turn contradictory infinite lifetimes into forever', () => {
    expect(addressLifetimeState('infinite', 3600)).toBe('unknown');
    expect(addressLifetimeState('infinite', 0)).toBe('unknown');
    expect(formatAddressLifetime('infinite', 3600)).toBe('unknown');
  });

  it('treats valid integer seconds as finite', () => {
    expect(addressLifetimeState(undefined, 0)).toBe('finite');
    expect(addressLifetimeState(undefined, 3600)).toBe('finite');
    expect(addressLifetimeState(undefined, 4294967294)).toBe('finite');
  });

  it('treats missing, malformed or out-of-range values as unknown', () => {
    expect(addressLifetimeState(undefined, undefined)).toBe('unknown');
    expect(addressLifetimeState(undefined, null)).toBe('unknown');
    expect(addressLifetimeState(undefined, -1)).toBe('unknown');
    expect(addressLifetimeState(undefined, 4294967295)).toBe('unknown');
    expect(addressLifetimeState(undefined, 3.14)).toBe('unknown');
    expect(addressLifetimeState(undefined, Number.NaN)).toBe('unknown');
    expect(addressLifetimeState(undefined, Number.POSITIVE_INFINITY)).toBe('unknown');
    expect(addressLifetimeState('provider_new_state', 3600)).toBe('unknown');
  });
});

describe('formatAddressLifetime', () => {
  it('renders infinite, finite and unknown without truthiness loss', () => {
    expect(formatAddressLifetime('infinite', undefined)).toBe('forever');
    expect(formatAddressLifetime('finite', 0)).toBe('0s');
    expect(formatAddressLifetime('finite', 3600)).toBe('1h 0m');
    expect(formatAddressLifetime('unknown', undefined)).toBe('unknown');
  });

  it('falls back to unknown for malformed finite seconds', () => {
    expect(formatAddressLifetime('finite', -1)).toBe('unknown');
    expect(formatAddressLifetime('finite', 4294967295)).toBe('unknown');
    expect(formatAddressLifetime('finite', 3.14)).toBe('unknown');
  });
});
