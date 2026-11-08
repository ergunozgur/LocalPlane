/**
 * One change — the record of crossing the write boundary.
 *
 * The five outcomes stay five. `not_written`, `written` and `write_unknown` are three
 * different answers to "did this reach the host", and `verified` and its four failure modes
 * are a different question again: whether a fresh observation proved the end state. This page
 * lays them out as a ladder because that is what they are, and because collapsing them into a
 * success badge is the single most tempting mistake a UI could make here.
 *
 * `write_unknown` gets particular care. It is not a maybe-true and it is not resolved by
 * reading the value back — that answers what the host holds now, which is a different
 * question from whether this write occurred.
 */
import { Link, useParams } from 'react-router-dom';
import { useRecovery } from '@/hooks/useRecovery';
import { RecoveryPanel } from '@/components/composer/RecoveryPanel';
import { RequestFailure } from '@/components/composer/RunComposer';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { DataTable } from '@/components/primitives/DataTable';
import { Tag } from '@/components/primitives/Chip';
import { StatusPill } from '@/components/semantic/StatusPill';
import { Value } from '@/components/semantic/UnknownValue';
import { Conclusion } from '@/components/semantic/Evidence';
import { Empty } from '@/components/states/SurfaceState';
import { PageHeader } from '../PageHeader';
import {
  changeResult,
  hostEffect,
  mutationOutcome,
  verificationOutcome,
} from '@/domain/vocabulary';
import { formatTimestamp, formatTypedValue } from '@/domain/format';
import styles from './ChangeDetail.module.css';

export function ChangeDetail(): JSX.Element {
  const { changeId = '' } = useParams();
  const controller = useRecovery(changeId);
  const change = controller.change;
  if (!change) return <><PageHeader title="Change" back={{ to: '/operations', label: 'Operations' }} />{controller.readError ? <RequestFailure error={controller.readError} /> : <p role="status">Reading change…</p>}<button type="button" onClick={controller.refresh}>Refresh recorded status</button></>;

  return (
        <>
          <PageHeader
            title={<span className="mono">{change.operation}</span>}
            back={{ to: '/operations', label: 'Operations' }}
            annotation={`Change ${change.change_id} on ${change.object_name}.`}
          >
            <StatusPill semantic={changeResult(change.result)} token={change.result} />
            <StatusPill semantic={hostEffect(change.host_effect)} />
          </PageHeader>

          <RecoveryPanel key={change.change_id} change={change} controller={controller} />

          {/* The ladder. Four questions, asked in order, never merged. */}
          <Plate className={styles.ladder}>
            <PlateHead title="Outcome" level={3} meta="four questions, answered separately" />
            <PlateBody>
              <div className={styles.rungs}>
                <Rung
                  step="1"
                  question="Did LocalPlane write to the host?"
                  pill={
                    <StatusPill
                      semantic={mutationOutcome(change.mutation.outcome)}
                      token={change.mutation.outcome ?? 'unsettled'}
                    />
                  }
                  detail={change.mutation.reason}
                />
                <Rung
                  step="2"
                  question="Was the host effect proven?"
                  pill={<StatusPill semantic={hostEffect(change.host_effect)} token={change.host_effect} />}
                  detail={
                    change.host_mutated
                      ? 'host_mutated is true only for `written`; `write_unknown` is not a maybe-true here.'
                      : null
                  }
                />
                <Rung
                  step="3"
                  question="Did a fresh observation confirm the end state?"
                  pill={
                    <StatusPill
                      semantic={verificationOutcome(change.verification.outcome)}
                      token={change.verification.outcome}
                    />
                  }
                  detail={change.verification.reason}
                />
                <Rung
                  step="4"
                  question="How did the change end?"
                  pill={<StatusPill semantic={changeResult(change.result)} token={change.result} />}
                  detail={null}
                />
              </div>

              {change.mutation.outcome === 'write_unknown' ? (
                <p className={styles.caution}>
                  This change dispatched and nothing settled it. Whether the write occurred is
                  not established, and reading the value back would not settle it — that answers
                  what the host holds now, which is a different question from whether this write
                  happened.
                </p>
              ) : null}
            </PlateBody>
          </Plate>

          <div className={styles.columns}>
            <div className={styles.column}>
              <Plate>
                <PlateHead title="What was attempted" level={3} />
                <PlateBody>
                  <KeyValueList columns="auto">
                    <KeyValue label="Change kind"><Value value={change.change_kind} mono /></KeyValue>
                    <KeyValue label="Object"><Value value={change.object_name} mono /></KeyValue>
                    {change.change_kind === 'field' ? (
                      <>
                        <KeyValue label="Field"><Value value={change.field} mono /></KeyValue>
                        <KeyValue label="Before">
                          <Value value={formatTypedValue(change.before_value)} mono />
                        </KeyValue>
                        <KeyValue label="Desired">
                          <Value value={formatTypedValue(change.desired_value)} mono />
                        </KeyValue>
                      </>
                    ) : (
                      <>
                        <KeyValue label="Action"><Value value={change.action} mono /></KeyValue>
                        <KeyValue label="Observed state"><Value value={change.observed_state} mono /></KeyValue>
                        <KeyValue label="Expected state"><Value value={change.expected_state} mono /></KeyValue>
                      </>
                    )}
                    <KeyValue label="Created"><Value value={formatTimestamp(change.created_at)} /></KeyValue>
                    <KeyValue label="Finished"><Value value={formatTimestamp(change.finished_at)} /></KeyValue>
                    <KeyValue label="Run">
                      <Link to={`/operations/runs/${change.run_id}`} className="mono">
                        {change.run_id}
                      </Link>
                    </KeyValue>
                  </KeyValueList>
                </PlateBody>
              </Plate>

              <Plate>
                <PlateHead title="Mutation" level={3} />
                <PlateBody>
                  <Conclusion
                    semantic={mutationOutcome(change.mutation.outcome)}
                    token={change.mutation.outcome ?? 'unsettled'}
                    why={change.mutation.reason}
                  />
                  <KeyValueList>
                    <KeyValue label="Provider"><Value value={change.mutation.provider} mono /></KeyValue>
                    <KeyValue label="Method"><Value value={change.mutation.method} mono /></KeyValue>
                    <KeyValue label="Attempt"><Tag title={change.mutation.attempt_id}>{change.mutation.attempt_id}</Tag></KeyValue>
                    <KeyValue label="Dispatch began" hint="committed before the request was sent">
                      <Value value={formatTimestamp(change.mutation.dispatch_began_at)} />
                    </KeyValue>
                    <KeyValue label="Settled"><Value value={formatTimestamp(change.mutation.settled_at)} /></KeyValue>
                  </KeyValueList>
                </PlateBody>
              </Plate>

              <Plate>
                <PlateHead title="Verification" level={3} />
                <PlateBody>
                  <Conclusion
                    semantic={verificationOutcome(change.verification.outcome)}
                    token={change.verification.outcome}
                    why={change.verification.reason}
                  />
                  <KeyValueList>
                    <KeyValue label="Observation">
                      <Value value={change.verification.observation_id} mono reason="no observation proved this" />
                    </KeyValue>
                    <KeyValue label="Observed value">
                      <Value value={formatTypedValue(change.verification.observed_value)} mono />
                    </KeyValue>
                    <KeyValue label="Expected value">
                      <Value value={formatTypedValue(change.verification.expected_value)} mono />
                    </KeyValue>
                    <KeyValue label="Observed state"><Value value={change.verification.observed_state} mono /></KeyValue>
                    <KeyValue label="Expected state"><Value value={change.verification.expected_state} mono /></KeyValue>
                  </KeyValueList>
                </PlateBody>
              </Plate>

              <Plate>
                <PlateHead title="Transcript" level={3} meta={`${change.events.length} events`} />
                <DataTable
                  caption="Change events"
                  rows={change.events}
                  rowKey={(row) => String(row.sequence)}
                  emptyState={<Empty title="No events" explanation="Nothing was recorded." />}
                  columns={[
                    { key: 'seq', header: '#', align: 'right', render: (row) => <span className="mono">{row.sequence}</span> },
                    { key: 'event', header: 'Event', render: (row) => <span className="mono">{row.event}</span> },
                    { key: 'at', header: 'At', render: (row) => <Value value={formatTimestamp(row.occurred_at)} /> },
                  ]}
                />
              </Plate>
            </div>

            <div className={styles.column}>
              <Plate>
                <PlateHead title="Rollback" level={3} />
                <PlateBody>
                  <KeyValueList>
                    <KeyValue label="Required">{change.rollback.required ? 'yes' : 'no'}</KeyValue>
                    <KeyValue label="Outcome">
                      <StatusPill
                        semantic={mutationOutcome(change.rollback.outcome)}
                        size="sm"
                        token={change.rollback.outcome ?? 'none attempted'}
                      />
                    </KeyValue>
                    <KeyValue label="Restores value">
                      <Value
                        value={formatTypedValue(change.rollback.restores_value)}
                        mono
                        reason="an action has no inverse value to restore"
                      />
                    </KeyValue>
                    <KeyValue label="Reason"><Value value={change.rollback.reason} mono /></KeyValue>
                  </KeyValueList>
                </PlateBody>
              </Plate>
            </div>
          </div>
        </>
  );
}

function Rung({
  step,
  question,
  pill,
  detail,
}: {
  step: string;
  question: string;
  pill: JSX.Element;
  detail?: string | null;
}): JSX.Element {
  return (
    <div className={styles.rung}>
      <span className={styles.step} aria-hidden="true">{step}</span>
      <div className={styles.rungBody}>
        <div className={styles.question}>{question}</div>
        <div className={styles.answer}>{pill}</div>
        {detail ? <div className={styles.rungDetail}>{detail}</div> : null}
      </div>
    </div>
  );
}
