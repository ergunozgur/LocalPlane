/**
 * The safety guarantees, asserted.
 *
 * These are not coverage tests. Each one names a way the UI could quietly lie about the
 * host, and fails if it becomes possible.
 */
import { describe, expect, it } from 'vitest';
import * as v from './vocabulary';

/** Every mapper in the module, so a new one cannot skip these rules by existing. */
const MAPPERS: ReadonlyArray<readonly [string, (t: string | null | undefined) => v.Semantic]> = [
  ['health', v.health],
  ['management', v.management],
  ['reconciliation', v.reconciliation],
  ['freshness', v.freshness],
  ['protection', v.protection],
  ['managementPathRelation', v.managementPathRelation],
  ['managementPathState', v.managementPathState],
  ['executionAvailability', v.executionAvailability],
  ['executionEligibility', v.executionEligibility],
  ['capabilityStatus', v.capabilityStatus],
  ['mutationOutcome', v.mutationOutcome],
  ['hostEffect', v.hostEffect],
  ['verificationOutcome', v.verificationOutcome],
  ['changeResult', v.changeResult],
  ['recoveryState', v.recoveryState],
  ['runState', v.runState],
  ['sweepStatus', v.sweepStatus],
  ['fidelity', v.fidelity],
  ['ownershipState', v.ownershipState],
  ['sourceStatus', v.sourceStatus],
  ['unitActiveState', v.unitActiveState],
  ['unitLoadState', v.unitLoadState],
  ['unitFileState', v.unitFileState],
  ['containerState', v.containerState],
];

describe('unknown never becomes reassurance', () => {
  it.each(MAPPERS)('%s maps an unrecognised token to the unknown tone', (_name, mapper) => {
    const result = mapper('a_token_this_build_has_never_seen');
    expect(result.tone).toBe('unknown');
    expect(result.tone).not.toBe('good');
  });

  it.each(MAPPERS)('%s never reports good for null', (_name, mapper) => {
    expect(mapper(null).tone).not.toBe('good');
  });

  it.each(MAPPERS)('%s never reports good for undefined', (_name, mapper) => {
    expect(mapper(undefined).tone).not.toBe('good');
  });

  it.each(MAPPERS)('%s never reports good for an empty string', (_name, mapper) => {
    expect(mapper('').tone).not.toBe('good');
  });

  it('maps the literal token "unknown" to the unknown tone wherever the backend uses it', () => {
    for (const mapper of [
      v.health,
      v.reconciliation,
      v.protection,
      v.managementPathRelation,
      v.ownershipState,
    ]) {
      expect(mapper('unknown').tone).toBe('unknown');
    }
  });
});

describe('protection', () => {
  it('keeps unknown distinct from clear', () => {
    expect(v.protection('unknown').tone).toBe('unknown');
    expect(v.protection('clear').tone).toBe('good');
    expect(v.protection('unknown').tone).not.toBe(v.protection('clear').tone);
  });

  it('describes clear as scoped rather than as "safe"', () => {
    // The backend is explicit that `clear` covers only the reasons this build implements.
    // If that caveat is ever dropped from the copy, this fails.
    expect(v.protection('clear').description).toMatch(/not a word for/i);
    expect(v.protection('clear').description.toLowerCase()).not.toMatch(/\bis safe\b/);
  });

  it('treats an unresolved management-path relation as unknown, not as not-on-path', () => {
    expect(v.managementPathRelation('unknown').tone).toBe('unknown');
    expect(v.managementPathRelation('not_on_management_path').tone).toBe('good');
  });
});

describe('the write boundary keeps its three answers', () => {
  it('separates not_written, written and write_unknown', () => {
    const notWritten = v.mutationOutcome('not_written');
    const written = v.mutationOutcome('written');
    const unknownWrite = v.mutationOutcome('write_unknown');

    const tones = new Set([notWritten.tone, written.tone, unknownWrite.tone]);
    expect(tones.size).toBe(3);

    expect(written.tone).toBe('good');
    expect(unknownWrite.tone).not.toBe('good');
    expect(unknownWrite.tone).not.toBe(notWritten.tone);
  });

  it('does not describe write_unknown as a failure', () => {
    // `write_unknown` is not `failed`: the write may well have happened.
    expect(v.mutationOutcome('write_unknown').label).not.toMatch(/fail/i);
    expect(v.mutationOutcome('write_unknown').tone).not.toBe('bad');
  });

  it('says reading the value back does not settle write_unknown', () => {
    expect(v.mutationOutcome('write_unknown').description).toMatch(/different question/i);
  });

  it('reports an unsettled dispatch as unknown rather than not_written', () => {
    expect(v.mutationOutcome(null).tone).toBe('unknown');
    expect(v.mutationOutcome(null).label).not.toBe(v.mutationOutcome('not_written').label);
  });
});

describe('verification', () => {
  it('keeps the three unprovable outcomes unknown rather than bad', () => {
    for (const token of ['value_unreadable', 'observation_unavailable', 'source_unavailable']) {
      expect(v.verificationOutcome(token).tone).toBe('unknown');
    }
    // A mismatch is a read that disagreed — a real negative, and different from the above.
    expect(v.verificationOutcome('mismatch').tone).toBe('bad');
  });
});

describe('change results', () => {
  it('treats recovery_required as a truthful ending rather than an error', () => {
    const result = v.changeResult('recovery_required');
    expect(result.tone).toBe('attention');
    expect(result.tone).not.toBe('bad');
    expect(result.description).toMatch(/truthful ending/i);
  });

  it('does not let resolved recovery imply the host is in a known state', () => {
    expect(v.recoveryState('resolved').description).toMatch(/does not say|not say/i);
  });
});

describe('execution availability', () => {
  it('does not report not_implemented as good', () => {
    expect(v.executionAvailability('not_implemented').tone).not.toBe('good');
  });

  it('warns that a probed mechanism is not an executor', () => {
    expect(v.executionAvailability('not_implemented').description).toMatch(/agent may report/i);
  });
});

describe('reconciliation', () => {
  it('reports an untracked object as untracked, not as in sync', () => {
    const untracked = v.reconciliation(null);
    expect(untracked.tone).not.toBe('good');
    expect(untracked.label).not.toMatch(/in sync/i);
    expect(untracked.description).toMatch(/not the same as being in sync/i);
  });
});

describe('humanise', () => {
  it('keeps the token readable without inventing meaning', () => {
    expect(v.humanise('transport_peer_local')).toBe('transport peer local');
    expect(v.humanise('protection_unresolved:management_path')).toBe(
      'protection unresolved: management path',
    );
  });
});
