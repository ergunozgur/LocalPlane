import { endpoints } from '@/api/endpoints';
import { ApiError } from '@/api/client';
import type { Run } from '@/api/types';
import { durableOperation } from './durableOperation';
import { refreshAll } from './useResource';

const phase = (run: Run): number => ({ preview: 0, awaiting_confirmation: 1, applying: 2, verifying: 3 }[run.state] ?? 4);
const useDurableRun = durableOperation<Run>({
  key: (id) => `localplane.run-intents:${id}`,
  load: (id) => endpoints.run(id),
  recordNotFound: (cause, id) => cause instanceof ApiError && cause.kind === 'backend' && cause.status === 404 && cause.code === 'run_not_found' && cause.path === `/runs/${encodeURIComponent(id)}`,
  onActionSettled: refreshAll,
  compare: (next, held) => Math.max(0, ...next.events.map((event) => event.sequence)) - Math.max(0, ...held.events.map((event) => event.sequence)) || phase(next) - phase(held),
  watch: (view) => {
    const run = view.data;
    if (run && ['succeeded', 'failed', 'cancelled', 'recovery_required', 'rolled_back'].includes(run.state)) return false;
    return !!view.pending || ['applying', 'verifying', 'arming', 'rolling_back', 'rollback_verifying'].includes(run?.state ?? '') || view.intents.includes('Applying') || (view.intents.some((intent) => intent.startsWith('Confirming:')) && !run?.preview.confirmation.satisfied);
  },
});
export function useRun(id: string) {
  const state = useDurableRun(id);
  let legacyAttempted = false;
  try { legacyAttempted = sessionStorage.getItem(`localplane.apply-attempt:${id}`) === 'sent'; } catch { legacyAttempted = true; }
  return { ...state, run: state.data, applyAttempted: legacyAttempted || state.intents.includes('Applying') || state.intents.includes('retention-unavailable'), confirmationAttempted: state.intents.some((intent) => intent.startsWith('Confirming:')) };
}

export function runRequestError(error: Error): string {
  if (!(error instanceof ApiError)) return error.message;
  if (error.status === 401) return `Authentication required: ${error.message}`;
  if (error.status === 403) return `Authorization / Origin refusal (${error.code}): ${error.message}`;
  if (error.code && /stale|preview.*mismatch/.test(error.code)) return `Preview no longer valid (${error.code}): ${error.message}`;
  if (error.kind === 'backend') return `Backend refusal or error (${error.code}): ${error.message}`;
  return `Response unavailable (${error.kind}). Delivery and outcome are unknown: ${error.message}`;
}
