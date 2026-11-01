import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import type { SystemdJournalResponse } from '@/api/types';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import styles from './ServiceJournal.module.css';

/** Ephemeral, consent-gated service evidence. Never move this response into shared resources. */
export function ServiceJournal({ objectId, canonicalId }: { objectId: string; canonicalId: string }): JSX.Element {
  const [result, setResult] = useState<SystemdJournalResponse | null>(null);
  const [state, setState] = useState<'idle' | 'loading' | 'error'>('idle');
  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    setResult(null);
    setState('idle');
    return () => { controller.current?.abort(); controller.current = null; };
  }, [objectId, canonicalId]);

  async function readJournal(): Promise<void> {
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    setResult(null);
    setState('loading');
    try {
      const response: unknown = await endpoints.systemdJournal(objectId, { signal: request.signal });
      if (request.signal.aborted || controller.current !== request) return;
      if (!isJournalResponse(response, objectId, canonicalId)) {
        setResult(null);
        setState('error');
        return;
      }
      setResult(response);
      setState('idle');
    } catch {
      if (!request.signal.aborted && controller.current === request) {
        setResult(null);
        setState('error');
      }
    }
  }

  return (
    <Plate>
      <PlateHead title="Recent service journal" level={3} meta="explicit read · current boot · last 15 minutes" />
      <PlateBody>
        <p className={styles.warning} role="note">
          Journal messages may contain sensitive information. LocalPlane does not reliably redact secrets.
          Only this service&apos;s MESSAGE text is requested. Exact _SYSTEMD_UNIT matching can omit manager messages and does not prove cause.
        </p>
        <p className={styles.meta}>Fixed limits: current boot, 15 minutes, at most 100 entries / 256 KiB collected / 4 KiB per message; 3-second collection deadline. Effective agent OS access only.</p>
        <p className={styles.handoff}>
          To make an eligible change, open <Link to={`/system/${encodeURIComponent(objectId)}?tab=lifecycle`}>Lifecycle for {canonicalId}</Link>.
          Journal availability does not enable or block service actions.
        </p>
        <button className={styles.read} type="button" disabled={state === 'loading'} onClick={() => void readJournal()}>
          {state === 'loading' ? 'Reading journal…' : 'Acknowledge and read journal'}
        </button>
        {state === 'error' ? <p role="alert">Journal evidence is unavailable: the response failed or did not match this service.</p> : null}
        {result ? <JournalResult result={result} /> : null}
      </PlateBody>
    </Plate>
  );
}

const JOURNAL_STATUSES = ['ok', 'empty', 'denied', 'unavailable', 'unsupported', 'timeout', 'busy', 'identity_changed', 'malformed'] as const;
const NEGATIVE_STATUSES = new Set(['denied', 'unavailable', 'unsupported', 'timeout', 'busy', 'identity_changed', 'malformed']);
const JOURNAL_COVERAGES = ['complete', 'partial', 'unknown'] as const;

function isJournalResponse(value: unknown, objectId: string, canonicalId: string): value is SystemdJournalResponse {
  if (!value || typeof value !== 'object') return false;
  const response = value as Partial<SystemdJournalResponse>;
  const coverage = Reflect.get(value, 'coverage') as unknown;
  if (response.object_id !== objectId || !JOURNAL_STATUSES.includes(response.status as typeof JOURNAL_STATUSES[number]) ||
      !JOURNAL_COVERAGES.includes(coverage as typeof JOURNAL_COVERAGES[number]) ||
      typeof response.retrieved_at !== 'string' || typeof response.since !== 'string' ||
      typeof response.truncated !== 'boolean' || !Array.isArray(response.entries) ||
      !Array.isArray(response.gaps) || !response.gaps.every((gap) => typeof gap === 'string') ||
      !(response.canonical_id === null || typeof response.canonical_id === 'string') ||
      !(response.boot_id === null || typeof response.boot_id === 'string')) return false;

  const validEntries = response.entries.every((entry) => entry && typeof entry === 'object' &&
    (entry.timestamp === null || typeof entry.timestamp === 'string') &&
    (entry.priority === null || typeof entry.priority === 'string') &&
    (entry.message === null || typeof entry.message === 'string') &&
    (entry.boot_id === null || typeof entry.boot_id === 'string') &&
    (entry.invocation_id === null || typeof entry.invocation_id === 'string'));
  if (!validEntries) return false;
  // Failures may not have a canonical identity. They remain useful only as typed, message-free
  // states scoped to the requested object; no provider text is used as an error message.
  if (NEGATIVE_STATUSES.has(response.status!)) return response.entries.length === 0;
  // Messages are sensitive: only a successful exact canonical match may display them.
  if (response.canonical_id !== canonicalId) return false;
  if (response.status === 'empty') return response.coverage === 'complete' && response.entries.length === 0;
  return response.status === 'ok' && response.entries.length > 0;
}

function JournalResult({ result }: { result: SystemdJournalResponse }): JSX.Element {
  const statusText: Record<string, string> = {
    ok: 'Entries returned', empty: 'No matching entries in this range', denied: 'Access denied',
    unavailable: 'Journal unavailable', unsupported: 'Journal access unsupported', timeout: 'Collection timed out',
    busy: 'Journal reader busy', identity_changed: 'Service identity changed; evidence withheld', malformed: 'Journal response was malformed',
  };
  return (
    <section className={styles.result} aria-label="Journal result">
      <p className={styles.meta}>{statusText[result.status] ?? 'Journal status unknown'} · retrieved {result.retrieved_at || 'time unavailable'} · {result.since || 'range unavailable'} · current boot {result.boot_id ?? 'unknown'}</p>
      {result.coverage === 'complete' ? <p className={styles.coverage}>Coverage complete within the declared journal-file query and fixed bounds only; this is not a frozen or all-history view.</p> : null}
      {result.coverage === 'partial' ? <p className={styles.partial} role="status">Partial journal-file access: matching messages may be omitted from files the agent cannot read.</p> : null}
      {result.coverage === 'unknown' ? <p className={styles.partial} role="status">Journal-file coverage is unknown; these results may be incomplete.</p> : null}
      {result.truncated ? <p role="status">Results are truncated by the fixed collection limits.</p> : null}
      {result.gaps?.length ? <ul>{result.gaps.map((gap, index) => <li key={`${index}-${gap}`}>{gap}</li>)}</ul> : null}
      {result.entries.length ? <ol className={styles.entries} aria-label="Service journal entries">
        {result.entries.map((entry, index) => <li key={`${entry.timestamp ?? 'unknown'}-${index}`}>
          <header>{entry.timestamp ?? 'Time unknown'} · priority {entry.priority ?? 'unknown'}</header>
          <pre>{entry.message === null ? 'MESSAGE not supplied for this entry' : entry.message === '' ? 'Empty MESSAGE text' : entry.message}</pre>
          <small>boot {entry.boot_id ?? 'unknown'} · invocation {entry.invocation_id ?? 'unknown'}</small>
        </li>)}
      </ol> : result.status === 'empty' ? <p>No entries matched this service and range.</p> : null}
    </section>
  );
}
