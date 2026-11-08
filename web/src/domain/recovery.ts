import type { ChangeRecovery } from '@/api/types';

/**
 * A new revision (including an assessment) is not proof that a missing grant was
 * consumed. Backend dispatch commits consumption, confirmation_id and the mutation
 * marker together. Only a completed retry carrying those facts creates a new boundary.
 */
export function recoveryConfirmationIntent(recovery: ChangeRecovery): string {
  const consumed = recovery.attempts.filter((attempt) =>
    attempt.kind === 'retry' && attempt.finished_at && attempt.outcome !== 'in_flight' &&
    !attempt.releases_hold && attempt.confirmation_id && attempt.mutation?.attempt_id &&
    attempt.mutation.dispatch_began_at,
  ).sort((a, b) => b.sequence - a.sequence)[0];
  // Keep the original key compatible with already-retained lost-confirmation intent.
  return consumed ? `confirm:after:${consumed.confirmation_id}:${consumed.attempt_id}` : 'confirm';
}
