import { describe, expect, it } from 'vitest';
import type { SystemdServiceFacts } from '@/api/types';
import { automaticRestartCount, executionOutcome, formatSystemdDuration } from './systemd';

const service = (facts: Partial<SystemdServiceFacts>): SystemdServiceFacts => facts as SystemdServiceFacts;

describe('systemd service execution readings', () => {
  it.each([
    [0, 0, 'no recorded main-process termination (status 0)'],
    [0, 23, 'no recorded main-process termination (status 23)'],
    [1, 0, 'exited (status 0)'],
    [1, 23, 'exited (status 23)'],
    [2, 11, 'terminated by a signal (status 11)'],
    [3, 6, 'core-dumped (status 6)'],
    [9, 42, 'unrecognised execution code 9 (status 42)'],
  ])('reads manager code %s without guessing a cause', (code, status, expected) => {
    expect(executionOutcome(service({ exec_main_code: code, exec_main_status: status }))).toBe(expected);
  });

  it('keeps missing execution fields unknown and renders zero delay/count explicitly', () => {
    expect(executionOutcome(service({ exec_main_code: null, exec_main_status: null }))).toBeNull();
    expect(executionOutcome(service({ exec_main_code: null, exec_main_status: 9 }))).toBe('execution code unavailable; manager status 9');
    expect(formatSystemdDuration(null)).toBeNull();
    expect(formatSystemdDuration(0)).toBe('0 µs');
    expect(formatSystemdDuration(1_500_000)).toBe('1.5 s');
    expect(automaticRestartCount(null)).toBeNull();
    expect(automaticRestartCount(0)).toBe('no automatic restarts reported');
    expect(automaticRestartCount(2)).toBe('2 automatic restarts reported');
  });
});
