/**
 * The one place a backend token becomes a colour and a word.
 *
 * LocalPlane's backend owns every safety judgement. This file's whole job is to *present*
 * those judgements without re-deriving them, and it is written so that the failure mode the
 * product cannot tolerate is structurally hard:
 *
 *   backend says UNKNOWN  ->  UI shows UNKNOWN
 *
 * Three rules hold throughout, and the tests assert them:
 *
 *  1. **Every mapping is total.** A token this build has never seen maps to `unknown`, never
 *     to `good`. New backend vocabulary degrades to "we do not recognise this", which is
 *     honest, rather than to a reassuring default, which is not.
 *  2. **`unknown` is its own tone.** Not a shade of `warn` and not `neutral`. A missing fact
 *     and a bad fact are different claims and must not look alike.
 *  3. **Nothing here combines tokens into a verdict.** There is no function that reads three
 *     fields and concludes "probably fine". Where the backend publishes a conclusion, it is
 *     shown; where it does not, none is invented.
 */

/**
 * How a state should read. Never carried by colour alone — every consumer pairs a tone with
 * a glyph and a word, so the meaning survives greyscale and colour-blindness alike.
 */
export type SemanticTone = 'good' | 'warn' | 'bad' | 'attention' | 'unknown' | 'neutral';

export interface Semantic {
  readonly tone: SemanticTone;
  /** What an operator reads. Backend tokens are snake_case; these are words. */
  readonly label: string;
  /** One sentence of what it means. Shown on hover and in drill-downs, never truncated. */
  readonly description: string;
}

/** The answer for anything this build does not recognise. Never `good`. */
function unrecognised(token: string): Semantic {
  return {
    tone: 'unknown',
    label: token,
    description:
      'This build does not recognise this value. It is shown exactly as the backend sent it, ' +
      'and no meaning has been assumed for it.',
  };
}

/**
 * Look a token up in a table, falling back to `unrecognised`.
 *
 * `null` and `undefined` are *not* holes to be filled with a default — they are their own
 * answer, and they get the unknown tone with the word "unknown".
 */
function lookup(
  table: Readonly<Record<string, Semantic>>,
  token: string | null | undefined,
  absent: Semantic,
): Semantic {
  if (token === null || token === undefined || token === '') return absent;
  return table[token] ?? unrecognised(token);
}

const NOT_KNOWN: Semantic = {
  tone: 'unknown',
  label: 'unknown',
  description: 'The backend did not establish this. It is not a negative answer — it is the absence of one.',
};

const NOT_APPLICABLE: Semantic = {
  tone: 'neutral',
  label: 'not applicable',
  description: 'This question does not apply to this object.',
};

/* ============================================================================== health */

const HEALTH: Readonly<Record<string, Semantic>> = {
  healthy: { tone: 'good', label: 'healthy', description: 'Observed working.' },
  degraded: { tone: 'warn', label: 'degraded', description: 'Working, with something wrong.' },
  failed: { tone: 'bad', label: 'failed', description: 'Observed not working.' },
  inactive: { tone: 'neutral', label: 'inactive', description: 'Not running. Not the same as failed.' },
  unknown: {
    tone: 'unknown',
    label: 'unknown',
    description: 'Health could not be established from what was observed.',
  },
};
export const health = (t: string | null | undefined): Semantic => lookup(HEALTH, t, NOT_KNOWN);

/* ========================================================================== management */

const MANAGEMENT: Readonly<Record<string, Semantic>> = {
  managed: {
    tone: 'good',
    label: 'managed',
    description: 'LocalPlane retains intent for this object and answers for it.',
  },
  observed: {
    tone: 'neutral',
    label: 'observed',
    description: 'LocalPlane watches this object and holds no intent for it. It does not drift.',
  },
  observe_only: {
    tone: 'neutral',
    label: 'observe-only',
    description: 'This object cannot be managed by this build. LocalPlane only watches it.',
  },
};
export const management = (t: string | null | undefined): Semantic =>
  lookup(MANAGEMENT, t, NOT_KNOWN);

/* ====================================================================== reconciliation */

const RECONCILIATION: Readonly<Record<string, Semantic>> = {
  in_sync: {
    tone: 'good',
    label: 'in sync',
    description: 'Every controlled field matches the retained intent.',
  },
  drifted: {
    tone: 'attention',
    label: 'drifted',
    description: 'A controlled field no longer matches the retained intent.',
  },
  applying: { tone: 'warn', label: 'applying', description: 'A change is in flight.' },
  unknown: {
    tone: 'unknown',
    label: 'unknown',
    description:
      'A controlled value could not be read, so drift could not be computed. This is not in-sync and it is not drifted.',
  },
};
export const reconciliation = (t: string | null | undefined): Semantic =>
  lookup(RECONCILIATION, t, {
    tone: 'neutral',
    label: 'not tracked',
    description:
      'Only managed objects reconcile. An observed object has no retained intent, so there is nothing for it to drift from — which is not the same as being in sync.',
  });

/* ============================================================================ freshness */

const FRESHNESS: Readonly<Record<string, Semantic>> = {
  current: { tone: 'good', label: 'current', description: 'Observed recently enough to be relied on.' },
  stale: {
    tone: 'warn',
    label: 'stale',
    description: 'Last observed longer ago than the freshness horizon. It may no longer be true.',
  },
  never_observed: {
    tone: 'unknown',
    label: 'never observed',
    description: 'Nothing has ever read this. There is no value here to be out of date.',
  },
};
export const freshness = (t: string | null | undefined): Semantic =>
  lookup(FRESHNESS, t, NOT_KNOWN);

/* =========================================================================== protection */

const PROTECTION: Readonly<Record<string, Semantic>> = {
  protected: {
    tone: 'attention',
    label: 'protected',
    description: 'A protection reason was proven to apply. Changing this is guarded or refused.',
  },
  clear: {
    tone: 'good',
    label: 'clear',
    description:
      'Every protection reason this build implements was evaluated and none applied. `clear` is scoped to those reasons and is not a word for "safe".',
  },
  unknown: {
    tone: 'unknown',
    label: 'unknown',
    description:
      'At least one protection reason could not be settled. Unresolved protection blocks execution — it is not treated as clear.',
  },
};
export const protection = (t: string | null | undefined): Semantic =>
  lookup(PROTECTION, t, NOT_KNOWN);

const MANAGEMENT_PATH_RELATION: Readonly<Record<string, Semantic>> = {
  on_management_path: {
    tone: 'attention',
    label: 'on the management path',
    description: 'This object carries the connection this request arrived over.',
  },
  not_on_management_path: {
    tone: 'good',
    label: 'not on the management path',
    description: 'Proven not to carry this request’s connection.',
  },
  unknown: {
    tone: 'unknown',
    label: 'relation unknown',
    description:
      'Whether this object carries the operator’s connection could not be established. Ordinary execution stays blocked, and no guarded path opens either.',
  },
};
export const managementPathRelation = (t: string | null | undefined): Semantic =>
  lookup(MANAGEMENT_PATH_RELATION, t, NOT_KNOWN);

const MANAGEMENT_PATH_STATE: Readonly<Record<string, Semantic>> = {
  confirmed: {
    tone: 'good',
    label: 'confirmed',
    description: 'LocalPlane knows which object carries this connection.',
  },
  unresolved: {
    tone: 'unknown',
    label: 'unresolved',
    description:
      'LocalPlane cannot tell which object carries this connection. Nothing is assumed from the absence.',
  },
};
export const managementPathState = (t: string | null | undefined): Semantic =>
  lookup(MANAGEMENT_PATH_STATE, t, NOT_KNOWN);

/* ============================================================ execution and capability */

const EXECUTION_AVAILABILITY: Readonly<Record<string, Semantic>> = {
  available: {
    tone: 'good',
    label: 'executor available',
    description: 'This build has code that would execute this operation.',
  },
  unavailable: {
    tone: 'warn',
    label: 'executor unavailable',
    description:
      'Implemented, but not usable on this host — no provider, no capability or no privilege.',
  },
  not_implemented: {
    tone: 'neutral',
    label: 'not implemented',
    description:
      'The plan says its publishing build had no code that would execute this operation — a value that now survives only on stored previews. The agent may report the underlying mechanism as available; that is a different question and does not make this plan executable.',
  },
};
export const executionAvailability = (t: string | null | undefined): Semantic =>
  lookup(EXECUTION_AVAILABILITY, t, NOT_KNOWN);

const EXECUTION_ELIGIBILITY: Readonly<Record<string, Semantic>> = {
  eligible: {
    tone: 'good',
    label: 'eligible',
    description: 'The ordinary apply path is open for this plan.',
  },
  guarded: {
    tone: 'attention',
    label: 'guarded only',
    description:
      'Ordinary execution is blocked and the connection-guarded path is the only write path that exists for this plan.',
  },
  blocked: {
    tone: 'bad',
    label: 'blocked',
    description: 'This plan would not be allowed to execute. Every reason is listed.',
  },
};
export const executionEligibility = (t: string | null | undefined): Semantic =>
  lookup(EXECUTION_ELIGIBILITY, t, NOT_KNOWN);

const CAPABILITY_STATUS: Readonly<Record<string, Semantic>> = {
  available: {
    tone: 'good',
    label: 'available',
    description: 'Every method behind this capability probed successfully.',
  },
  degraded: {
    tone: 'warn',
    label: 'degraded',
    description: 'The capability works, with less evidence than its definition promises.',
  },
  unavailable: {
    tone: 'bad',
    label: 'unavailable',
    description: 'The capability cannot be used on this host.',
  },
};
export const capabilityStatus = (t: string | null | undefined): Semantic =>
  lookup(CAPABILITY_STATUS, t, NOT_KNOWN);

/* ================================================================== the write boundary */

const MUTATION_OUTCOME: Readonly<Record<string, Semantic>> = {
  not_written: {
    tone: 'neutral',
    label: 'not written',
    description: 'The host was not written. LocalPlane knows this.',
  },
  written: {
    tone: 'good',
    label: 'written',
    description: 'The write occurred and LocalPlane can prove it.',
  },
  write_unknown: {
    tone: 'attention',
    label: 'write unknown',
    description:
      'Dispatch began and nothing settled it. Whether this write occurred is not established, and reading the value back would answer a different question — what the host holds now, not whether this write happened.',
  },
};
export const mutationOutcome = (t: string | null | undefined): Semantic =>
  lookup(MUTATION_OUTCOME, t, {
    tone: 'unknown',
    label: 'unsettled',
    description: 'A dispatch is in flight and has recorded no outcome yet.',
  });

const HOST_EFFECT: Readonly<Record<string, Semantic>> = {
  none: {
    tone: 'neutral',
    label: 'no host effect',
    description: 'This never crossed the write boundary. Nothing on the host was touched.',
  },
  written: { tone: 'good', label: 'host written', description: 'The host was written and proven.' },
  write_unknown: {
    tone: 'attention',
    label: 'host effect unknown',
    description: 'The host may have been written. LocalPlane cannot say.',
  },
};
export const hostEffect = (t: string | null | undefined): Semantic =>
  lookup(HOST_EFFECT, t, NOT_KNOWN);

const VERIFICATION_OUTCOME: Readonly<Record<string, Semantic>> = {
  not_attempted: {
    tone: 'neutral',
    label: 'not attempted',
    description: 'No verification was run.',
  },
  verified: {
    tone: 'good',
    label: 'verified',
    description: 'A fresh observation through the ordinary path proved the intended end state.',
  },
  mismatch: {
    tone: 'bad',
    label: 'mismatch',
    description: 'The host was read and does not hold what was intended.',
  },
  value_unreadable: {
    tone: 'unknown',
    label: 'value unreadable',
    description: 'The value could not be read, so nothing was proved either way.',
  },
  observation_unavailable: {
    tone: 'unknown',
    label: 'observation unavailable',
    description: 'No observation could be taken, so nothing was proved either way.',
  },
  source_unavailable: {
    tone: 'unknown',
    label: 'source unavailable',
    description: 'The source that would prove this could not be reached.',
  },
};
export const verificationOutcome = (t: string | null | undefined): Semantic =>
  lookup(VERIFICATION_OUTCOME, t, NOT_KNOWN);

const CHANGE_RESULT: Readonly<Record<string, Semantic>> = {
  in_flight: { tone: 'warn', label: 'in flight', description: 'Not yet settled.' },
  succeeded: {
    tone: 'good',
    label: 'succeeded',
    description: 'The host was written and the intended end state was verified.',
  },
  failed: {
    tone: 'bad',
    label: 'failed',
    description: 'The change did not achieve its end state, and LocalPlane knows what happened.',
  },
  rolled_back: {
    tone: 'attention',
    label: 'rolled back',
    description: 'The previous value was restored.',
  },
  recovery_required: {
    tone: 'attention',
    label: 'recovery required',
    description:
      'A truthful ending, not an error: this change could not be proved safe, and it holds the object’s write lock until somebody looks.',
  },
};
export const changeResult = (t: string | null | undefined): Semantic =>
  lookup(CHANGE_RESULT, t, NOT_KNOWN);

const RECOVERY_STATE: Readonly<Record<string, Semantic>> = {
  not_required: { tone: 'neutral', label: 'not required', description: 'Nothing is held.' },
  unresolved: {
    tone: 'attention',
    label: 'unresolved',
    description: 'The hold is still in place. Only a person releases it.',
  },
  resolved: {
    tone: 'good',
    label: 'resolved',
    description:
      'The hold was released — by a retry or by an operator’s decision. It does not say the host is in any particular state.',
  },
};
export const recoveryState = (t: string | null | undefined): Semantic =>
  lookup(RECOVERY_STATE, t, NOT_KNOWN);

const RUN_STATE: Readonly<Record<string, Semantic>> = {
  preview: { tone: 'neutral', label: 'preview', description: 'A plan is published. Nothing has been confirmed.' },
  awaiting_confirmation: {
    tone: 'warn',
    label: 'awaiting confirmation',
    description: 'The plan requires a confirmation that has not been given.',
  },
  arming: { tone: 'warn', label: 'arming', description: 'Recovery material is being written.' },
  applying: { tone: 'attention', label: 'applying', description: 'Past the write boundary.' },
  verifying: { tone: 'warn', label: 'verifying', description: 'Reading the host back.' },
  guarded: {
    tone: 'attention',
    label: 'guarded',
    description: 'Written and proven, waiting for the operator’s connection to prove itself.',
  },
  succeeded: { tone: 'good', label: 'succeeded', description: 'Finished, and proven.' },
  failed: { tone: 'bad', label: 'failed', description: 'Finished without achieving the end state.' },
  cancelled: { tone: 'neutral', label: 'cancelled', description: 'Abandoned before execution.' },
  recovery_required: {
    tone: 'attention',
    label: 'recovery required',
    description: 'Ended holding a write lock that only a person can release.',
  },
};
export const runState = (t: string | null | undefined): Semantic => lookup(RUN_STATE, t, NOT_KNOWN);

/* ============================================================ observation and ownership */

const SWEEP_STATUS: Readonly<Record<string, Semantic>> = {
  ok: { tone: 'good', label: 'ok', description: 'Every source answered.' },
  partial: {
    tone: 'warn',
    label: 'partial',
    description: 'Some sources answered and some did not. What is missing is named.',
  },
  failed: {
    tone: 'bad',
    label: 'failed',
    description: 'The sweep did not complete. An empty list below means nobody looked, not that there is nothing.',
  },
};
export const sweepStatus = (t: string | null | undefined): Semantic =>
  lookup(SWEEP_STATUS, t, NOT_KNOWN);

const FIDELITY: Readonly<Record<string, Semantic>> = {
  complete: { tone: 'good', label: 'complete', description: 'Every field the source promises was supplied.' },
  partial: {
    tone: 'warn',
    label: 'partial',
    description: 'Some fields were not supplied. They are null, not zero.',
  },
  degraded: {
    tone: 'warn',
    label: 'degraded',
    description: 'The reading was taken with less evidence than usual.',
  },
};
export const fidelity = (t: string | null | undefined): Semantic => lookup(FIDELITY, t, NOT_KNOWN);

const OWNERSHIP_STATE: Readonly<Record<string, Semantic>> = {
  attributed: {
    tone: 'good',
    label: 'attributed',
    description: 'Evidence names who made this object and who configures it.',
  },
  unknown: {
    tone: 'unknown',
    label: 'unknown',
    description: 'No source could attribute this object. The reason names which source left it open.',
  },
};
export const ownershipState = (t: string | null | undefined): Semantic =>
  lookup(OWNERSHIP_STATE, t, NOT_KNOWN);

const SOURCE_STATUS: Readonly<Record<string, Semantic>> = {
  ok: { tone: 'good', label: 'answered', description: 'This source was consulted and answered.' },
  absent: {
    tone: 'neutral',
    label: 'not present',
    description: 'The provider is not installed on this host. Not a failure.',
  },
  unavailable: {
    tone: 'warn',
    label: 'unavailable',
    description: 'The provider is present but could not be reached.',
  },
  error: { tone: 'bad', label: 'error', description: 'The provider was reached and failed.' },
  never_consulted: {
    tone: 'unknown',
    label: 'never consulted',
    description: 'This source was not asked.',
  },
};
export const sourceStatus = (t: string | null | undefined): Semantic =>
  lookup(SOURCE_STATUS, t, NOT_KNOWN);

/* ============================================================================ findings */

const FINDING_STATUS: Readonly<Record<string, Semantic>> = {
  open: {
    tone: 'attention',
    label: 'open',
    description: 'LocalPlane is still making this claim, and the evidence still supports it.',
  },
  resolved: {
    tone: 'good',
    label: 'resolved',
    description:
      'The claim ended. `resolution` says how — and an intent revision is one of the ways, which is not the same as the host having been put right.',
  },
};
export const findingStatus = (t: string | null | undefined): Semantic =>
  lookup(FINDING_STATUS, t, NOT_KNOWN);

const COMPARISON: Readonly<Record<string, Semantic>> = {
  differs: {
    tone: 'attention',
    label: 'differs',
    description: 'The observed value and the intended value were both read, and they disagree.',
  },
  unknown: {
    tone: 'unknown',
    label: 'not comparable',
    description:
      'One side could not be read, so no comparison was made. This is not agreement and it is not disagreement.',
  },
};
export const findingComparison = (t: string | null | undefined): Semantic =>
  lookup(COMPARISON, t, NOT_KNOWN);

/** A single controlled field's comparison, which is a narrower question than the object's. */
const FIELD_COMPARISON: Readonly<Record<string, Semantic>> = {
  matches: {
    tone: 'good',
    label: 'matches',
    description: 'The observed value equals the intended one.',
  },
  differs: {
    tone: 'attention',
    label: 'differs',
    description: 'Both values were read and they disagree.',
  },
  unknown: {
    tone: 'unknown',
    label: 'not comparable',
    description:
      'The observed value could not be read, so no comparison was made. That is unknown, never drift.',
  },
};
export const fieldComparison = (t: string | null | undefined): Semantic =>
  lookup(FIELD_COMPARISON, t, NOT_KNOWN);

/* ================================================================= systemd and docker */

const UNIT_ACTIVE: Readonly<Record<string, Semantic>> = {
  active: { tone: 'good', label: 'active', description: 'systemd reports this unit active.' },
  reloading: { tone: 'warn', label: 'reloading', description: 'Reloading its configuration.' },
  inactive: { tone: 'neutral', label: 'inactive', description: 'Not running. Not a failure.' },
  failed: { tone: 'bad', label: 'failed', description: 'systemd reports this unit failed.' },
  activating: { tone: 'warn', label: 'activating', description: 'Starting.' },
  deactivating: { tone: 'warn', label: 'deactivating', description: 'Stopping.' },
  maintenance: { tone: 'warn', label: 'maintenance', description: 'Held in maintenance.' },
};
export const unitActiveState = (t: string | null | undefined): Semantic =>
  lookup(UNIT_ACTIVE, t, NOT_KNOWN);

const UNIT_LOAD: Readonly<Record<string, Semantic>> = {
  loaded: { tone: 'good', label: 'loaded', description: 'The unit file was read successfully.' },
  'not-found': { tone: 'warn', label: 'not found', description: 'No unit file of this name was found.' },
  'bad-setting': { tone: 'bad', label: 'bad setting', description: 'The unit file has a setting systemd rejected.' },
  error: { tone: 'bad', label: 'error', description: 'The unit file could not be read.' },
  masked: { tone: 'attention', label: 'masked', description: 'Deliberately made unstartable.' },
  merged: { tone: 'neutral', label: 'merged', description: 'Merged into another unit.' },
  stub: { tone: 'neutral', label: 'stub', description: 'Known of, with no unit file loaded.' },
};
export const unitLoadState = (t: string | null | undefined): Semantic =>
  lookup(UNIT_LOAD, t, NOT_KNOWN);

const UNIT_FILE: Readonly<Record<string, Semantic>> = {
  enabled: { tone: 'good', label: 'enabled', description: 'Starts at boot.' },
  'enabled-runtime': { tone: 'good', label: 'enabled (runtime)', description: 'Enabled until reboot.' },
  disabled: { tone: 'neutral', label: 'disabled', description: 'Does not start at boot.' },
  static: { tone: 'neutral', label: 'static', description: 'Has no install section; cannot be enabled.' },
  masked: { tone: 'attention', label: 'masked', description: 'Cannot be started at all.' },
  generated: { tone: 'neutral', label: 'generated', description: 'Produced by a generator, not a file on disk.' },
  transient: { tone: 'neutral', label: 'transient', description: 'Created at runtime; does not survive a reboot.' },
  indirect: { tone: 'neutral', label: 'indirect', description: 'Enabled through another unit.' },
  alias: { tone: 'neutral', label: 'alias', description: 'Another name for a unit.' },
  linked: { tone: 'neutral', label: 'linked', description: 'Symlinked into the unit path.' },
};
export const unitFileState = (t: string | null | undefined): Semantic =>
  lookup(UNIT_FILE, t, NOT_KNOWN);

const CONTAINER_STATE: Readonly<Record<string, Semantic>> = {
  running: { tone: 'good', label: 'running', description: 'The daemon reports this container running.' },
  created: { tone: 'neutral', label: 'created', description: 'Created and never started.' },
  paused: { tone: 'warn', label: 'paused', description: 'Processes frozen.' },
  restarting: { tone: 'warn', label: 'restarting', description: 'The daemon is restarting it.' },
  removing: { tone: 'warn', label: 'removing', description: 'Being removed.' },
  exited: { tone: 'neutral', label: 'exited', description: 'Stopped. The exit code says how.' },
  dead: { tone: 'bad', label: 'dead', description: 'The daemon could not clean it up.' },
};
export const containerState = (t: string | null | undefined): Semantic =>
  lookup(CONTAINER_STATE, t, NOT_KNOWN);

const CONTAINER_HEALTH: Readonly<Record<string, Semantic>> = {
  healthy: { tone: 'good', label: 'healthy', description: 'The container’s own health check passes.' },
  unhealthy: { tone: 'bad', label: 'unhealthy', description: 'The container’s own health check fails.' },
  starting: { tone: 'warn', label: 'starting', description: 'The health check has not settled yet.' },
};
export const containerHealth = (t: string | null | undefined): Semantic =>
  lookup(CONTAINER_HEALTH, t, {
    tone: 'neutral',
    label: 'no health check',
    description: 'This container declares no health check. That is not an unhealthy container.',
  });

/* ============================================================================== helpers */

export const notKnown = (): Semantic => NOT_KNOWN;
export const notApplicable = (): Semantic => NOT_APPLICABLE;

/**
 * Turn a backend token into something readable without pretending to interpret it.
 *
 * Used for the long tail of typed codes — `transport_peer_local`, `externally_configured`,
 * `execution_not_implemented` — that have no curated sentence. The raw token is always shown
 * alongside, in mono, because that is the thing an operator would search for.
 */
export function humanise(token: string): string {
  return token.replace(/[_.]/g, ' ').replace(/:/g, ': ');
}
