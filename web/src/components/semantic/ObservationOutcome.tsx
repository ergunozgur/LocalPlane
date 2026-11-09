/**
 * The outcome banner for an explicit observe → stored-read flow.
 *
 * States exactly
 * what the provider returned, whether the stored list matches it, and what recovery actions
 * are available — without ever claiming the list is current when it is not.
 */
import type { ReactNode } from 'react';
import type { BaseObservationResult, InventoryOutcome } from '@/hooks/useInventoryObservation';
import { ApiError } from '@/api/client';
import { Degraded } from '@/components/states/SurfaceState';
import { IssueList, ProviderList } from '@/dashboard/widgets/shared';
import { formatTimestamp } from '@/domain/format';
import styles from './ObservationOutcome.module.css';

interface ObservationOutcomeProps<Result extends BaseObservationResult> {
  outcome: InventoryOutcome<Result> | null;
  subject: string;
  resultSummary: (result: Result) => ReactNode;
  onRefresh: () => void;
  onRetryStoredRead: () => void;
  vocabulary?: { action: string; actor: string; records: string; observingTitle: string; uncertain: string };
  hasStoredRecords: boolean;
}

export function ObservationOutcome<Result extends BaseObservationResult>({
  outcome,
  subject,
  resultSummary,
  onRefresh,
  onRetryStoredRead,
  vocabulary,
  hasStoredRecords,
}: ObservationOutcomeProps<Result>): JSX.Element | null {
  if (!outcome) return null;
  const action = vocabulary?.action ?? 'observation';
  const actor = vocabulary?.actor ?? 'the agent';
  const records = vocabulary?.records ?? subject.toLowerCase();
  const retained = hasStoredRecords ? 'The prior stored records remain visible. ' : 'No stored records are currently visible. ';

  if (outcome.phase === 'observing') {
    return (
      <div className={styles.outcome} role="status" aria-live="polite">
        <Degraded title={vocabulary?.observingTitle ?? `Observing ${subject.toLowerCase()} now`}>
          {retained}LocalPlane is asking {actor} for one {action}.
        </Degraded>
      </div>
    );
  }
  if (outcome.phase === 'reading') {
    return (
      <div className={styles.outcome} role="status" aria-live="polite">
        <Degraded title={`Reading stored ${records} records`}>
          {actor} returned an {action} response. LocalPlane is re-reading stored records
          before presenting the list as updated.
        </Degraded>
      </div>
    );
  }
  if (outcome.phase === 'observation_failed') {
    return (
      <div className={styles.outcome} role="alert">
        <Degraded tone="warn" title={`${subject} ${action} request failed`}>
          The backend returned no completed {action} result. {retained}<ErrorFact error={outcome.error} />{' '}
          <button type="button" className="btn sm" onClick={onRefresh}>Retry {action}</button>
        </Degraded>
      </div>
    );
  }
  if (outcome.phase === 'observation_uncertain') {
    return (
      <div className={styles.outcome} role="alert">
        <Degraded tone="unknown" title={`${subject} ${action} outcome is uncertain`}>
          LocalPlane did not receive a readable {action} result, so {vocabulary?.uncertain ?? 'the agent may or may not have looked'}. A stored-record re-read may show different retained evidence, but cannot
          acknowledge this response. <ErrorFact error={outcome.error} />{' '}
          <button type="button" className="btn sm" onClick={onRefresh}>Retry {action}</button>
        </Degraded>
      </div>
    );
  }
  if (outcome.phase === 'reread_failed') {
    return (
      <div className={styles.outcome} role="alert">
        <Degraded tone="unknown" title={`${subject} responded, but stored records could not be re-read`}>
          {retained}Re-read stored records does not contact {actor} or repeat the {action}.{' '}
          {resultSummary(outcome.result)} <ErrorFact error={outcome.error} />{' '}
          <button type="button" className="btn sm" onClick={onRetryStoredRead}>Re-read stored records</button>
          <IssueList issues={outcome.result.issues} label="Reported issues" />
          <ProviderList providers={outcome.result.providers} label="Provider results" />
        </Degraded>
      </div>
    );
  }

  if (!('list' in outcome)) return null;
  const { result, list } = outcome;
  const latest = list.last_sweep;
  const matchesReturnedSweep = latest?.sweep_id === result.sweep_id;
  const providerFailed = result.status === 'failed';
  const title = outcome.phase === 'complete'
    ? `${subject} ${action} completed`
    : outcome.phase === 'partial'
      ? `${subject} ${action} completed with gaps`
      : providerFailed
        ? `${subject} ${action} reported provider failure`
        : `${subject} ${action} returned unrecognised status: ${result.status}`;

  return (
    <div className={styles.outcome} role="status" aria-live="polite">
      <Degraded tone={outcome.phase === 'complete' ? 'unknown' : providerFailed ? 'bad' : 'warn'} title={title}>
        {resultSummary(result)}
        {matchesReturnedSweep
          ? <> Stored records were re-read for this sweep{latest?.completed_at ? ` at ${formatTimestamp(latest.completed_at)}` : ''}.</>
          : <> Stored records were re-read, but their latest sweep differs from this returned response; the list is not claimed to represent the returned sweep.</>}
        <IssueList issues={result.issues} label="Reported issues" />
        <ProviderList providers={result.providers} label="Provider results" />
      </Degraded>
    </div>
  );
}

function ErrorFact({ error }: { error: ApiError }): JSX.Element {
  const reason = typeof error.detail.reason === 'string' && error.detail.reason !== error.message
    ? ` Reason: ${error.detail.reason}`
    : '';
  return <span className={styles.errorFact}><code>{error.code ?? error.kind}</code>: {error.message}{reason}</span>;
}
