import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { Change } from '@/api/types';
import { endpoints } from '@/api/endpoints';
import type { useRecovery } from '@/hooks/useRecovery';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { Disclosure, Gaps, RawEvidence } from '@/components/semantic/Evidence';
import { formatTimestamp } from '@/domain/format';
import { buildOffersControl } from '@/domain/execution';
import { recoveryConfirmationIntent } from '@/domain/recovery';
import { RequestFailure } from './RunComposer';
import styles from './RecoveryPanel.module.css';

type Controller = ReturnType<typeof useRecovery>;
export function RecoveryPanel({ change, controller }: { change: Change; controller: Controller }): JSX.Element {
  const recovery = change.recovery;
  const [grantReview, setGrantReview] = useState({ intent: '', typed: '', ack: false });
  const [releaseTyped, setReleaseTyped] = useState('');
  const [releaseAck, setReleaseAck] = useState(false);
  const [note, setNote] = useState('');
  const requirement = recovery.confirmation_requirement;
  const grant = recovery.authority;
  const revision = recovery.revision;
  const inFlight = recovery.attempts.some((attempt) => attempt.outcome === 'in_flight');
  const metadata = Number.isSafeInteger(revision) && revision >= 0;
  const idle = !controller.pending && !controller.reading && !controller.readError && !controller.actionError && !controller.data?.relatedError && !inFlight;
  const supported = buildOffersControl(change.operation);
  const held = recovery.object_write_locked && recovery.state === 'unresolved';
  const available = (action: string): boolean => supported && held && metadata && recovery.available_actions.includes(action);
  const spent = (intent: string): boolean => controller.intents.includes(intent) || controller.intents.includes('retention-unavailable');
  const confirmIntent = recoveryConfirmationIntent(recovery);
  // Only grant inputs are scoped to consumption proof. Reads/revisions preserve them,
  // and changing this boundary cannot erase manual-release inputs or move focus.
  const typed = grantReview.intent === confirmIntent ? grantReview.typed : '';
  const ack = grantReview.intent === confirmIntent && grantReview.ack;
  const retryIntent = `retry:${grant?.confirmation_id ?? 'missing'}`;
  const assessIntent = `assess:${revision}`;
  const releaseIntent = `resolve:${revision}`;
  const supportedRequirement = !!requirement && ['typed', 'acknowledge'].includes(requirement.method) && !!requirement.policy && requirement.object_name === change.object_name;
  const validGrant = !!grant?.confirmation_id && supportedRequirement && grant.method === requirement.method && grant.required_method === requirement.method && grant.policy === requirement.policy;
  const canConfirm = available('confirm_retry') && idle && !grant && supportedRequirement && ack && (requirement.method !== 'typed' || typed === requirement.object_name) && !spent(confirmIntent);
  const canRetry = available('retry') && idle && validGrant && !spent(retryIntent);
  const canResolve = available('resolve') && idle && releaseAck && releaseTyped === change.object_name && note.length <= 2000 && !spent(releaseIntent);
  const latest = recovery.attempts.at(-1);
  return <Plate tone={held ? 'attention' : undefined} className={styles.panel} aria-label="Recovery console">
    <PlateHead title="Recovery" meta={held ? 'object-only hold' : recovery.state} />
    <PlateBody>
      <div className={styles.identity}><strong>{change.object_name}</strong><span>{change.operation}</span><Link to={`/operations/runs/${encodeURIComponent(change.run_id)}`}>Original Run · {change.run_id}</Link></div>
      <div className={styles.summary}>
        <div><span>Original outcome</span><strong>{change.result}</strong><small>Mutation: {change.mutation.outcome ?? 'unsettled'} · Verification: {change.verification.outcome}</small></div>
        <div><span>Latest recovery attempt</span><strong>{latest ? `${latest.kind} · ${latest.outcome}` : 'None recorded'}</strong><small>{latest ? formatTimestamp(latest.started_at) : 'No assessment or recovery recorded'}</small></div>
        <div><span>Current object write lock</span><strong>{recovery.object_write_locked ? 'Held' : 'Released / not held'}</strong><small>This object only, not the whole host.</small></div>
      </div>
      <p className={styles.note}>Original history is immutable. Proving an end state later does not establish who caused the original uncertain write. Manual release is neither verification nor repair.</p>
      <p className={styles.note}>Current recovery reason: <code>{recovery.reason ?? 'unknown'}</code> · Recorded revision: {metadata ? revision : 'unavailable'}</p>
      <Gaps items={recovery.unknown} label="What remains unknown" limit={recovery.unknown.length} />
      <div className={styles.actions}>
        <button type="button" disabled={controller.reading} onClick={controller.refresh}>Refresh recorded status</button>
        {supported && held ? <button type="button" disabled={!available('assess') || !idle || spent(assessIntent)} onClick={() => { if (available('assess') && idle && !spent(assessIntent)) void controller.act('Assessing current service state', () => endpoints.assessRecovery(change.change_id, revision), assessIntent); }}>Assess current service state</button> : null}
      </div>
      <p className={styles.note}>Refresh uses GET only. Assessment explicitly records fresh evidence without host mutation, and cannot consume an outstanding retry grant. Restart proof needs the original invocation baseline; running alone is not restart proof.</p>
      <div role="status" aria-live="polite" className={styles.note}>{controller.pending ? `${controller.pending}… Requests are not replayed.` : controller.reading ? 'Reading the same Change, original Run and authoritative holds…' : 'Showing recorded recovery evidence.'}{controller.watchStopped ? ' Automatic read budget reached. Refresh manually to continue.' : ''}</div>
      {controller.data?.holds ? <p className={styles.note}>Authoritative unresolved holds on this host: {controller.data.holds.total_count}. This is a server reread, not a count derived from original outcomes.</p> : <p className={styles.note}>Authoritative host hold count is unavailable or awaiting reread.</p>}
      {controller.readError ? <><p className={styles.note}>Retained data may be stale. Write controls are disabled.</p><RequestFailure error={controller.readError} /></> : null}
      {controller.actionError ? <RequestFailure error={controller.actionError} /> : null}
      {controller.data?.relatedError ? <RequestFailure error={controller.data.relatedError} /> : null}
      {!metadata || (supported && held && !supportedRequirement) ? <p className={styles.note}>Required recovery revision or confirmation metadata is missing or unsupported. Recovery writes remain disabled.</p> : null}
      {supported && held ? <div className={styles.forms}>
        <section aria-label="Authorize exact recovery retry">
          <h4>Authorize, then retry</h4>
          <p className={styles.note}>Immutable original action: <strong>{change.action ?? change.operation}</strong> on <strong>{change.object_name}</strong>. No substitute target, command or rollback is offered.</p>
          <p className={styles.note}>Confirmation: {requirement?.method ?? 'unavailable'} · {requirement?.policy ?? 'policy unavailable'}</p>
          {grant ? <KeyValueList>
            <KeyValue label="Outstanding single-use grant">{grant.confirmation_id}</KeyValue>
            <KeyValue label="Granted / source">{formatTimestamp(grant.satisfied_at)} · {grant.source}</KeyValue>
            <KeyValue label="Method / policy">{grant.method} · {grant.policy}</KeyValue>
          </KeyValueList> : <>
            <label className={styles.field}><input type="checkbox" checked={ack} disabled={!idle} onChange={(e) => setGrantReview({ intent: confirmIntent, typed, ack: e.target.checked })} />I authorize one recovery retry of this exact original action.</label>
            {requirement?.method === 'typed' ? <label className={styles.field}>Type {requirement.object_name} to authorize retry<input value={typed} autoComplete="off" spellCheck={false} disabled={!idle} onChange={(e) => setGrantReview({ intent: confirmIntent, typed: e.target.value, ack })} /></label> : null}
            <button type="button" disabled={!canConfirm} onClick={() => { if (canConfirm) void controller.act('Granting recovery authority', () => endpoints.confirmRecovery(change.change_id, revision, { acknowledge: true, expected_recovery_reason: recovery.reason, ...(requirement.method === 'typed' ? { acknowledge_object: typed } : {}) }), confirmIntent); }}>Grant one recovery retry</button>
          </>}
          {spent(confirmIntent) && !grant ? <p className={styles.note}>A grant request was attempted in this tab. Its absence in a read is not proof it was never delivered. This console will not renew it; refresh and inspect the durable record.</p> : null}
          {confirmIntent !== 'confirm' && !spent(confirmIntent) && !grant ? <p className={styles.note}>Durable retry history records consumption of the prior grant. Review the current revision and acknowledge/type again to grant a new single-use retry. Nothing is renewed or retried automatically.</p> : null}
          <div className={styles.actions}><button type="button" disabled={!canRetry} onClick={() => { if (canRetry && grant) void controller.act('Retrying original action once', () => endpoints.retryRecovery(change.change_id, revision, grant.confirmation_id), retryIntent); }}>Retry original action once</button></div>
          <p className={styles.note}>Granting never retries automatically. An outstanding grant may come from another tab. Backend executor, pending-job, freshness, protection and authority checks remain decisive.</p>
        </section>
        <section aria-label="Manual hold release">
          <h4>Release hold manually</h4>
          <p className={styles.note}>Records a human decision, without host mutation or any upgrade to the original outcome. Current reason is cross-checked: {recovery.reason ?? 'unknown'}.</p>
          <label className={styles.field}><input type="checkbox" checked={releaseAck} disabled={!idle} onChange={(e) => setReleaseAck(e.target.checked)} />I understand release does not verify or repair the service.</label>
          <label className={styles.field}>Type {change.object_name} to release hold<input value={releaseTyped} autoComplete="off" spellCheck={false} disabled={!idle} onChange={(e) => setReleaseTyped(e.target.value)} /></label>
          <label className={styles.field}>Recorded note (optional, maximum 2000 characters)<textarea value={note} maxLength={2000} disabled={!idle} onChange={(e) => setNote(e.target.value)} /></label>
          <button type="button" disabled={!canResolve} onClick={() => { if (canResolve) void controller.act('Releasing object hold manually', () => endpoints.resolveRecovery(change.change_id, revision, { acknowledge: true, acknowledge_object: releaseTyped, expected_recovery_reason: recovery.reason, ...(note ? { note } : {}) }), releaseIntent); }}>Release object hold manually</button>
        </section>
      </div> : null}
      <Disclosure summary="Current recovery evidence and release provenance">
        <KeyValueList><KeyValue label="Released at / by">{formatTimestamp(recovery.released_at)} · {recovery.released_by ?? 'not recorded'}</KeyValue><KeyValue label="Release attempt">{recovery.released_by_attempt_id ?? 'none'}</KeyValue></KeyValueList>
        <h4>Known evidence</h4><RawEvidence value={recovery.known} />
        <h4>Last observed evidence (recorded, not live)</h4><RawEvidence value={recovery.last_observed} />
      </Disclosure>
      <h4>Recovery history · chronological</h4>
      {recovery.attempts.length ? <ol className={styles.history}>{[...recovery.attempts].sort((a, b) => a.sequence - b.sequence).map((attempt) => <li key={attempt.attempt_id}>
        <div className={styles.identity}><strong>{attempt.kind} · {attempt.outcome}</strong><span>{formatTimestamp(attempt.started_at)}</span></div>
        <p className={styles.note}>Host effect: {attempt.host_effect} · Hold released: {attempt.releases_hold ? 'yes' : 'no'} · Refusal: {attempt.refusal_code ?? 'none recorded'}</p>
        <Disclosure summary={`Attempt ${attempt.sequence} · evidence, dispatch and verification`}>
          <KeyValueList><KeyValue label="Attempt">{attempt.attempt_id}</KeyValue><KeyValue label="Finished">{formatTimestamp(attempt.finished_at)}</KeyValue><KeyValue label="Grant">{attempt.confirmation_id ?? 'none recorded'}</KeyValue><KeyValue label="Management path">{attempt.management_path}</KeyValue><KeyValue label="Protection evidence">{attempt.protection_evidence_id ?? 'none recorded'}</KeyValue><KeyValue label="Typed release statement">{attempt.operator_statement ?? 'none'}</KeyValue><KeyValue label="Recorded note">{attempt.note ?? 'none'}</KeyValue></KeyValueList>
          <h4>Pre-write evidence</h4><KeyValueList><KeyValue label="Conclusion / reason">{attempt.evidence.outcome} · {attempt.evidence.reason ?? 'not recorded'}</KeyValue><KeyValue label="Observation source">{attempt.evidence.observation_id ?? 'unavailable'}</KeyValue><KeyValue label="Observed / required state">{attempt.evidence.observed_state ?? 'unknown'} / {attempt.evidence.expected_state ?? 'unknown'}</KeyValue></KeyValueList>
          <h4>Mutation / dispatch</h4>{attempt.mutation ? <KeyValueList><KeyValue label="Outcome / reason">{attempt.mutation.outcome ?? 'unsettled'} · {attempt.mutation.reason ?? 'not recorded'}</KeyValue><KeyValue label="Provider / method">{attempt.mutation.provider} · {attempt.mutation.method}</KeyValue><KeyValue label="Dispatch / settled">{formatTimestamp(attempt.mutation.dispatch_began_at)} / {formatTimestamp(attempt.mutation.settled_at)}</KeyValue></KeyValueList> : <p className={styles.note}>No new mutation recorded.</p>}
          <h4>Post-write verification</h4>{attempt.verification ? <KeyValueList><KeyValue label="Conclusion / reason">{attempt.verification.outcome} · {attempt.verification.reason ?? 'not recorded'}</KeyValue><KeyValue label="Observation source">{attempt.verification.observation_id ?? 'unavailable'}</KeyValue><KeyValue label="Observed / required state">{attempt.verification.observed_state ?? 'unknown'} / {attempt.verification.expected_state ?? 'unknown'}</KeyValue></KeyValueList> : <p className={styles.note}>No post-write verification recorded.</p>}
          <Disclosure summary="Technical evidence"><RawEvidence value={{ before: attempt.evidence, mutation: attempt.mutation, after: attempt.verification }} /></Disclosure>
        </Disclosure>
      </li>)}</ol> : <p className={styles.note}>No recovery attempts recorded.</p>}
    </PlateBody>
  </Plate>;
}
