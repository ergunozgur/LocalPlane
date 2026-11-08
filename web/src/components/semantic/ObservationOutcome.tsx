/**
 * The outcome banner for an explicit observe → stored-read flow.
 *
 * This is the generic form of the Docker-specific banner in WorkloadList. It states exactly
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
}

export function ObservationOutcome<Result extends BaseObservationResult>({
  outcome,
  subject,
  resultSummary,
  onRefresh,
  onRetryStoredRead,
}: ObservationOutcomeProps<Result>): JSX.Element | null {
  if (!outcome) return null;

  if (outcome.phase === 'observing') {
    return (
      <div className={styles.outcome} role="status" aria-live="polite">
        <Degraded title={`Observing ${subject.toLowerCase()} now`}>
          The prior stored records remain visible while LocalPlane asks the agent for one
          observation.
        </Degraded>
      </div>
    );
  }
  if (outcome.phase === 'reading') {
    return (
      <div className={styles.outcome} role="status" aria-live="polite">
        <Degraded title={`Reading stored ${subject.toLowerCase()} records`}>
          The agent returned an observation response. LocalPlane is re-reading stored records
          before presenting the list as updated.
        </Degraded>
      </div>
    );
  }
  if (outcome.phase === 'observation_failed') {
    return (
      <div className={styles.outcome} role="alert">
        <Degraded tone="warn" title={`${subject} observation request failed`}>
          The backend returned no completed observation result. The visible stored records were
          not replaced. <ErrorFact error={outcome.error} />{' '}
          <button type="button" className="btn sm" onClick={onRefresh}>Retry observation</button>
        </Degraded>
      </div>
    );
  }
  if (outcome.phase === 'observation_uncertain') {
    return (
      <div className={styles.outcome} role="alert">
        <Degraded tone="unknown" title={`${subject} observation outcome is uncertain`}>
          LocalPlane did not receive a readable observation result, so the agent may or may not
          have looked. A stored-record re-read may show different retained evidence, but cannot
          acknowledge this response. <ErrorFact error={outcome.error} />{' '}
          <button type="button" className="btn sm" onClick={onRefresh}>Retry observation</button>
        </Degraded>
      </div>
    );
  }
  if (outcome.phase === 'reread_failed') {
    return (
      <div className={styles.outcome} role="alert">
        <Degraded tone="unknown" title={`${subject} responded, but stored records could not be re-read`}>
          The prior stored records remain visible. Re-read stored records does not contact the
          agent or repeat the observation. <ErrorFact error={outcome.error} />{' '}
          <button type="button" className="btn sm" onClick={onRetryStoredRead}>Re-read stored records</button>
          <IssueList issues={outcome.result.issues} label="Reported issues" />
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
    ? `${subject} observation completed`
    : outcome.phase === 'partial'
      ? `${subject} observation completed with gaps`
      : providerFailed
        ? `${subject} observation reported provider failure`
        : `${subject} observation returned unrecognised status: ${result.status}`;

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
