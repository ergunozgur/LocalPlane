/**
 * The execution gate. One function, one authority.
 *
 * **An agent capability is not permission to act, and this file exists so that no component
 * can mistake it for one.** Measured on a live host while this was written, when the backend
 * had no lifecycle executor:
 *
 *   GET /agent/capabilities  ->  systemd.service.lifecycle : available, mutating: true
 *   POST /runs {systemd.service.restart}
 *        preview.how.availability                 = "not_implemented"
 *        preview.how.capability_declared_by_agent = true
 *        preview.confirmation.satisfiable         = false
 *
 * The agent honestly reports that the *mechanism* exists — systemd does expose the job
 * contract — and the mechanism alone decided nothing. That is still the shape to read: the
 * executor landed on the backend, but a capability never made a plan executable and still
 * does not. A UI that gated a Start button on the capability list would render an enabled
 * control for whatever the plan does not allow. So the gate reads `PlanExecution`, which
 * only a published plan carries, and the absence of a plan is reported as what it is: not
 * assessed. This console exposes no lifecycle write controls at all, authenticated or not;
 * authentication does not exist yet.
 *
 * Nothing here decides whether an operation is *safe*. That judgement is the backend's and
 * arrives already made, in `availability`, `eligibility` and `blockers`. This file only
 * decides whether a control may be drawn.
 */
import type { PlanConfirmation, PlanExecution } from '@/api/types';

export type ExecutionGateState =
  /** The backend published a plan whose executor exists and whose apply path is open. */
  | 'executable'
  /** Ordinary execution is blocked; the connection-guarded path is the only one open. */
  | 'guarded'
  /** A plan exists and says it would be refused. `blockers` says why. */
  | 'blocked'
  /** A stored plan says its build had no executor — legacy evidence, not a misconfiguration. */
  | 'not_implemented'
  /** No plan has been published, so execution has not been assessed. An unknown. */
  | 'not_assessed';

export interface ExecutionGate {
  readonly state: ExecutionGateState;
  /** Whether a control that would perform this operation may be rendered *enabled*. */
  readonly mayOfferControl: boolean;
  /** Every reason execution is not open, verbatim from the backend. Never summarised away. */
  readonly blockers: readonly string[];
  /** The capability an execution would need, when a plan named one. */
  readonly requiredCapability: string | null;
  /** Whether the agent declared that capability. Informative only — never the gate. */
  readonly capabilityDeclaredByAgent: boolean | null;
  /** The backend's own sentence about this plan's executability. */
  readonly note: string | null;
}

const NOT_ASSESSED: ExecutionGate = {
  state: 'not_assessed',
  mayOfferControl: false,
  blockers: [],
  requiredCapability: null,
  capabilityDeclaredByAgent: null,
  note: null,
};

/**
 * Derive the gate from a published plan.
 *
 * `execution` absent means no plan exists, which is `not_assessed` — never `executable`.
 * There is deliberately no overload taking a capability list: if such a function existed,
 * something would eventually call it.
 */
export function executionGate(
  execution: PlanExecution | null | undefined,
  confirmation?: PlanConfirmation | null,
): ExecutionGate {
  if (!execution) return NOT_ASSESSED;

  const blockers = Object.freeze([...execution.blockers]);
  const base = {
    blockers,
    requiredCapability: execution.required_capability || null,
    capabilityDeclaredByAgent: execution.capability_declared_by_agent,
    note: execution.note || null,
  };

  if (execution.availability === 'not_implemented') {
    return { ...base, state: 'not_implemented', mayOfferControl: false };
  }
  if (execution.availability !== 'available') {
    return { ...base, state: 'blocked', mayOfferControl: false };
  }

  // `satisfiable: false` means no confirmation could ever be given for this plan, which
  // makes the apply path unreachable however open `eligibility` looks. Both are checked.
  const satisfiable = confirmation ? confirmation.satisfiable : true;

  switch (execution.eligibility) {
    case 'eligible':
      return { ...base, state: 'executable', mayOfferControl: satisfiable };
    case 'guarded':
      return { ...base, state: 'guarded', mayOfferControl: satisfiable };
    default:
      return { ...base, state: 'blocked', mayOfferControl: false };
  }
}

/**
 * Whether this build offers *any* control for an operation type.
 *
 * This foundation is read-only: it renders the write boundary in full and produces nothing
 * across it. The list is empty rather than absent so that the seam is visible — later
 * work adds a member here and a control, and the gate above still has the final word.
 */
export const OPERATIONS_WITH_UI_CONTROLS: readonly string[] = Object.freeze([]);

export function buildOffersControl(operationType: string): boolean {
  return OPERATIONS_WITH_UI_CONTROLS.includes(operationType);
}
