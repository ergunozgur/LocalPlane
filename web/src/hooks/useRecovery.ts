import { endpoints } from '@/api/endpoints';
import { ApiError } from '@/api/client';
import type { Change, RecoveryHolds, Run } from '@/api/types';
import { durableOperation } from './durableOperation';
import { refreshAll } from './useResource';
import { recoveryConfirmationIntent } from '@/domain/recovery';

interface RecoveryRecord { change: Change; holds: RecoveryHolds | null; run: Run | null; relatedError: Error | null; }
const useRecord = durableOperation<RecoveryRecord>({
  key: (id) => `localplane.recovery-intents:${id}`,
  // Other mounted hold summaries also reread server truth, never an optimistic count.
  onActionSettled: refreshAll,
  recordNotFound: (cause, id) => cause instanceof ApiError && cause.kind === 'backend' && cause.status === 404 && cause.code === 'change_not_found' && cause.path === `/changes/${encodeURIComponent(id)}`,
  // changes.py checks revision before confirmation insertion/attempt admission/release
  // commit, and exact grant mismatch before consumption. No generic conflict qualifies.
  preDispatchRefusal: (cause, id) => cause instanceof ApiError && cause.kind === 'backend' && cause.status === 409 && (
    (cause.code === 'recovery_revision_mismatch' && ['assess', 'confirm', 'retry', 'resolve'].some((action) => cause.path === `/changes/${encodeURIComponent(id)}/recovery/${action}`)) ||
    (cause.code === 'recovery_confirmation_mismatch' && cause.path === `/changes/${encodeURIComponent(id)}/recovery/retry`)
  ),
  load: async (id) => {
    const change = await endpoints.change(id);
    const [holds, run] = await Promise.allSettled([endpoints.recoveryHolds(), endpoints.run(change.run_id)]);
    for (const result of [holds, run]) if (result.status === 'rejected' && result.reason instanceof ApiError && result.reason.status === 401) throw result.reason;
    return { change, holds: holds.status === 'fulfilled' ? holds.value : null, run: run.status === 'fulfilled' ? run.value : null,
      relatedError: holds.status === 'rejected' ? holds.reason as Error : run.status === 'rejected' ? run.reason as Error : null };
  },
  compare: (next, held) => Number.isSafeInteger(next.change.recovery.revision) && Number.isSafeInteger(held.change.recovery.revision) ? next.change.recovery.revision - held.change.recovery.revision : 0,
  watch: (view) => {
    const recovery = view.data?.change.recovery;
    if (recovery && !recovery.object_write_locked) return false;
    const uncertainGrant = !!recovery && view.intents.includes(recoveryConfirmationIntent(recovery)) && !recovery.authority;
    const uncertainAssessment = view.intents.includes(`assess:${recovery?.revision}`);
    const uncertainRelease = view.intents.includes(`resolve:${recovery?.revision}`);
    const uncertainRetry = !!recovery?.authority && view.intents.includes(`retry:${recovery.authority.confirmation_id}`);
    return !!view.pending || recovery?.attempts.some((attempt) => attempt.outcome === 'in_flight') === true || (!!view.actionError && view.intents.length > 0) || uncertainGrant || uncertainAssessment || uncertainRelease || uncertainRetry;
  },
});

export function useRecovery(id: string) {
  const state = useRecord(id);
  return { ...state, change: state.data?.change ?? null,
    act: (name: string, request: () => Promise<Change>, intent: string) => state.act(name, async () => ({ change: await request(), holds: null, run: null, relatedError: null }), intent),
  };
}
