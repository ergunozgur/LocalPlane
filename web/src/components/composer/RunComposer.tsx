import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import type { Run } from '@/api/types';
import { endpoints } from '@/api/endpoints';
import { executionGate, buildOffersControl } from '@/domain/execution';
import { useRun, runRequestError } from '@/hooks/useRun';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { RawEvidence } from '@/components/semantic/Evidence';
import { ApiError } from '@/api/client';
import styles from './Composer.module.css';

type Controller = ReturnType<typeof useRun>;
export function RunComposer({ run, controller }: { run: Run; controller: Controller }): JSX.Element {
  const applyRef = useRef<HTMLButtonElement>(null);
  const focusAfterConfirm = useRef(false);
  const [typed, setTyped] = useState('');
  const [acknowledged, setAcknowledged] = useState(false);
  const preview = run.preview;
  const confirmation = preview.confirmation;
  const gate = executionGate(preview.how, confirmation);
  const preWrite = ['preview', 'awaiting_confirmation'].includes(run.state) && run.host_effect === 'none' && !run.change_id;
  const idle = !controller.pending && !controller.reading && !controller.readError && !controller.actionError;
  const valid = preview.validity.state === 'current' && preview.protection.status === 'clear';
  const supportedMethod = confirmation.required
    ? ['acknowledge', 'typed'].includes(confirmation.method)
    : confirmation.method === 'none';
  const ordinary = buildOffersControl(run.operation) && preWrite && valid && supportedMethod && gate.state === 'executable' && gate.mayOfferControl && !controller.applyAttempted;
  const confirmed = !confirmation.required || (confirmation.satisfied && run.confirmation?.preview_id === preview.preview_id && run.confirmation.preview_digest === preview.preview_digest && run.confirmation.method === confirmation.method && !run.confirmation.consumed);
  useEffect(() => {
    if (!focusAfterConfirm.current || controller.pending) return;
    focusAfterConfirm.current = false;
    // Do not pull focus away if the operator moved to another control during the request.
    if (confirmed && document.activeElement === document.body) applyRef.current?.focus();
  }, [confirmed, controller.pending]);
  const canConfirm = ordinary && idle && confirmation.required && !confirmation.satisfied && acknowledged && (confirmation.method !== 'typed' || typed === preview.what.object_name);
  const confirm = (): void => {
    if (!canConfirm) return;
    focusAfterConfirm.current = true;
    void controller.act('Confirming', () => endpoints.confirmRun(run.run_id, {
      preview_id: preview.preview_id,
      expected_preview_digest: preview.preview_digest,
      acknowledge: true,
      ...(confirmation.method === 'typed' ? { acknowledge_object: typed } : {}),
    }));
  };
  return <Plate className={styles.composer} aria-label="Change Composer">
    <PlateHead title="Change Composer" meta={`${preview.risk.tier} risk`} />
    <PlateBody>
      <div className={styles.identity}><span>{run.object_name}</span><span>{run.run_id}</span></div>
      <p className={styles.note}>{preview.operation.summary}</p>
      <p className={styles.note}>Review the published preview below. Closing this view leaves the Run intact; it does not cancel execution or roll anything back.</p>
      {ordinary && confirmation.required && !confirmation.satisfied ? <>
        <p className={styles.note}>{confirmation.policy} {confirmation.reasons.join(' · ')}</p>
        <label className={styles.confirm}><input type="checkbox" checked={acknowledged} disabled={!idle} onChange={(event) => setAcknowledged(event.target.checked)} />I have reviewed this preview and acknowledge this service action.</label>
        {confirmation.method === 'typed' ? <label className={styles.confirm}>Type {preview.what.object_name}<input className={styles.input} value={typed} disabled={!idle} autoComplete="off" onChange={(event) => setTyped(event.target.value)} /></label> : null}
      </> : null}
      {preWrite && !ordinary && !controller.applyAttempted ? <p className={styles.note}>Ordinary apply is unavailable. Read the execution, protection, validity and confirmation evidence below.{!supportedMethod ? ' This confirmation method is not supported here.' : ''}</p> : null}
      {controller.applyAttempted ? <p className={styles.note}>An apply request was attempted from this tab. It will not be repeated here. Refresh this Run and inspect its Change for the recorded outcome.</p> : null}
      <div role="status" aria-live="polite">
        {controller.pending ? `${controller.pending}… The backend owns this Run.` : controller.reading ? 'Reading the Run…' : `Recorded state: ${run.state}; host effect: ${run.host_effect}.`}
      </div>
      {run.change ? <div className={styles.note} aria-label="Recorded outcome">
        <p>Change result: {run.change.result} · Mutation: {run.change.mutation.outcome ?? 'unsettled'} · Verification: {run.change.verification.outcome}</p>
        <p>{run.change.mutation.reason} {run.change.recovery.reason}</p>
      </div> : null}
      {controller.actionError ? <RequestFailure error={controller.actionError} /> : null}
      {controller.readError ? <RequestFailure error={controller.readError} /> : null}
      <div className={styles.actions}>
        {ordinary && confirmation.required && !confirmation.satisfied ? <button type="button" className={`${styles.button} ${styles.primary}`} disabled={!canConfirm} onClick={confirm}>Confirm preview</button> : null}
        {buildOffersControl(run.operation) && preWrite ? <button ref={applyRef} type="button" className={`${styles.button} ${styles.primary}`} disabled={!ordinary || !confirmed || !idle} onClick={() => { if (ordinary && confirmed && idle) void controller.act('Applying', () => endpoints.applyRun(run.run_id)); }}>Apply approved preview</button> : null}
        {buildOffersControl(run.operation) && preWrite && !controller.applyAttempted ? <button type="button" className={styles.button} disabled={!idle} onClick={() => { if (idle) void controller.act('Cancelling', () => endpoints.cancelRun(run.run_id)); }}>Cancel before write</button> : null}
        <button type="button" className={styles.button} disabled={!!controller.pending || controller.reading} onClick={controller.refresh}>Refresh Run</button>
        <Link to="/operations?tab=runs">Close Composer</Link>
        {run.change_id ? <Link to={`/operations/changes/${encodeURIComponent(run.change_id)}`}>Inspect Change outcome and verification</Link> : null}
      </div>
    </PlateBody>
  </Plate>;
}

export function RequestFailure({ error }: { error: Error }): JSX.Element {
  return <div role="alert" className={styles.error}>
    <p>{runRequestError(error)}</p>
    {error instanceof ApiError && error.code ? <code>{error.code}</code> : null}
    <p>Refresh the existing Run to recover visibility before taking another action. No request is retried automatically.</p>
    {error instanceof ApiError && Object.keys(error.detail).length ? <RawEvidence value={error.detail} /> : null}
  </div>;
}
