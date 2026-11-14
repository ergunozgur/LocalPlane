/** Presentation models only. These are not API contracts or provider observations. */
import { Link } from 'react-router-dom';
import type { ReactNode } from 'react';
import type { ConnectivityPolicy as ApiConnectivityPolicy } from '@/api/types';
import { tcpPolicyUnavailableReason } from './connectivityPolicy';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { Disclosure, Gaps } from '@/components/semantic/Evidence';
import { Value } from '@/components/semantic/UnknownValue';
import { Empty } from '@/components/states/SurfaceState';
import styles from './ConnectivityPanel.module.css';

export interface ConnectivityEvidence {
  state: 'waiting' | 'not_observed' | 'unavailable' | 'error' | 'complete' | 'partial' | 'changed';
  source: string | null;
  observedAt: string | null;
  age: string | null;
  freshness: string | null;
  detail: string;
  gaps: readonly string[];
  /** Empty is meaningful only when the section is complete. Null fields remain unknown. */
  facts: readonly { label: string; value: string | null }[];
}

export interface ConnectivityComparison {
  profile: ConnectivityEvidence;
  applied: ConnectivityEvidence;
  kernel: ConnectivityEvidence;
  resolver: ConnectivityEvidence;
  routes: ConnectivityEvidence;
  rules: ConnectivityEvidence;
}

export type DiagnosticPolicyView =
  | { state: 'waiting'; detail: string }
  | { state: 'blocked'; detail: string }
  | { state: 'ready'; dnsNames: readonly string[]; tcpEndpoints: readonly string[] };

export interface ConnectivityActionEvidence {
  kind: 'route' | 'dns' | 'tcp';
  target: string;
  outcome: string;
  detail: string;
  source: string | null;
  vantage: string | null;
  startedAt: string | null;
  completedAt: string | null;
  /** Route selection is not an interface-binding promise for a diagnostic. */
  egress: { name: string; objectId: string | null } | null;
  facts: readonly { label: string; value: string | null }[];
}

export type ConnectivityResultState =
  | { state: 'idle' }
  | { state: 'pending'; target: string }
  | { state: 'error'; detail: string }
  | { state: 'complete'; result: ConnectivityActionEvidence };

export interface ConnectivityAttempt {
  pending: string | null;
  error: string | null;
}

const waitingEvidence: ConnectivityEvidence = {
  state: 'waiting', source: null, observedAt: null, age: null, freshness: null,
  detail: 'Connectivity evidence has not been loaded.', gaps: [], facts: [],
};

const waitingComparison: ConnectivityComparison = {
  profile: waitingEvidence, applied: waitingEvidence, kernel: waitingEvidence,
  resolver: waitingEvidence, routes: waitingEvidence, rules: waitingEvidence,
};

const stateLabels: Record<ConnectivityEvidence['state'], string> = {
  waiting: 'Waiting for evidence', not_observed: 'Not observed', unavailable: 'Provider unavailable',
  error: 'Read failed', complete: 'Complete observation', partial: 'Partial observation',
  changed: 'Changed during read',
};

export function ConnectivityEvidencePlate({ title, evidence, children }: {
  title: string; evidence: ConnectivityEvidence; children?: ReactNode;
}): JSX.Element {
  return (
    <Plate aria-label={title}>
      <PlateHead title={title} level={3} />
      <PlateBody>
        <p className={styles.state} data-state={evidence.state}>{stateLabels[evidence.state]}</p>
        <p className={styles.note}>{evidence.detail}</p>
        <KeyValueList>
          <KeyValue label="Source"><Value value={evidence.source} mono /></KeyValue>
          <KeyValue label="Observed at"><Value value={evidence.observedAt} mono /></KeyValue>
          <KeyValue label="Age"><Value value={evidence.age} /></KeyValue>
          <KeyValue label="Freshness"><Value value={evidence.freshness} /></KeyValue>
          {evidence.facts.map((fact) => (
            <KeyValue key={fact.label} label={fact.label}><Value value={fact.value} mono /></KeyValue>
          ))}
        </KeyValueList>
        {evidence.facts.length === 0 && evidence.state === 'complete' && !children ? (
          <Empty title="No entries reported" explanation="This source reported an empty section at the observation time, not current absence." />
        ) : null}
        <Gaps items={evidence.gaps} />
      </PlateBody>
      {children}
    </Plate>
  );
}

export function ConnectivityPolicy({ policy, metadata }: { policy: DiagnosticPolicyView; metadata?: ApiConnectivityPolicy | null }): JSX.Element {
  const tcpUnavailable = tcpPolicyUnavailableReason(metadata);
  return (
    <div className={styles.policy}>
      {policy.state === 'waiting' ? <p className={styles.note}>Active diagnostic authorization unknown. {policy.detail}</p> : null}
      {policy.state === 'blocked' ? <p className={styles.warning}>Active diagnostics unavailable. {policy.detail}</p> : null}
      {policy.state === 'ready' ? (
        <>
          {policy.dnsNames.length === 0 && policy.tcpEndpoints.length === 0 ? (
            <Empty title="No targets configured for active diagnostics" explanation="An administrator must provision targets before DNS or TCP diagnostics can run. Passive route lookup is separate." />
          ) : null}
          <KeyValueList>
            <KeyValue label="Authorized DNS names">{policy.dnsNames.length ? policy.dnsNames.join(', ') : 'None configured'}</KeyValue>
            <KeyValue label="Configured TCP allow entries">{policy.tcpEndpoints.length ? policy.tcpEndpoints.join(', ') : 'None configured'}</KeyValue>
          </KeyValueList>
        </>
      ) : null}
      <p className={tcpUnavailable ? styles.warning : styles.note}>{tcpUnavailable ?? 'Backend TCP policy eligibility is ready for an explicit configured-pair request; this does not prove current agent agreement or live guard availability.'}</p>
      <p className={styles.note}>DNS name and TCP IP:port authorizations are independent. Resolving a name does not authorize a connection to its answers.</p>
      <p className={styles.note}>TCP requires a complete administrator-attested v2 control-plane declaration, not automatic discovery. LocalPlane cannot detect every undeclared topology change. If the declaration cannot be maintained, use DNS-only with TCP disabled.</p>
      <p className={styles.warning}>Before changing management topology, provision <code>tcp: []</code>, restart BOTH backend and agent, and wait for any actual running diagnostic attempt to finish before the topology change. Policy loading is startup-only: saving the file alone does not disable currently loaded TCP targets.</p>
      <p className={styles.note}>After the topology change, reconcile the complete control-plane declaration and desired allowed targets, then restart BOTH backend and agent with matching validated policy before TCP can be re-enabled. There is no UI enable button, policy editor or hot reload.</p>
      <Disclosure summary="Policy metadata and checks">
        <KeyValueList>
          <KeyValue label="Loaded policy version"><Value value={metadata?.version} mono /></KeyValue>
          <KeyValue label="Control-plane declaration"><Value value={metadata?.control_plane_complete === true ? 'Complete — administrator attestation' : metadata?.control_plane_complete === false ? 'Incomplete — not attested complete' : null} /></KeyValue>
          <KeyValue label="Declared control-plane address count"><Value value={metadata?.address_count} mono /></KeyValue>
          <KeyValue label="Backend TCP policy status"><Value value={metadata?.tcp_status} mono /></KeyValue>
        </KeyValueList>
        <p className={styles.note}>Configured TCP allow entries may still be rejected by the declared address-wide deny set. The count does not disclose those addresses. Backend policy enforcement and fresh agent policy/current local, broadcast and namespace checks occur on each request; neither management-path confirmation nor universal discovery is implied.</p>
      </Disclosure>
    </div>
  );
}

export function ConnectivityResult({ selectedInterface, value, attempt }: {
  selectedInterface: string; value: ConnectivityResultState; attempt?: ConnectivityAttempt;
}): JSX.Element {
  const result = value.state === 'complete' ? value.result : null;
  return (
    <Plate aria-label="Destination result">
      <PlateHead title="Destination result" level={3} />
      <PlateBody>
        <div role="status" aria-live="polite" aria-atomic="true">
          {attempt?.pending ? <p className={styles.state}>New action pending · {attempt.pending}. The last completed result below is previous evidence, not this attempt’s outcome.</p> : null}
          {attempt?.error ? <p className={styles.warning}>Latest request failed · {attempt.error}. The last completed result below has not been refreshed by this failure.</p> : null}
          {value.state === 'idle' ? <p className={styles.note}>No action has been requested. Opening this tab sends no diagnostic packets.</p> : null}
          {value.state === 'pending' ? <p className={styles.state}>Waiting for result · {value.target}</p> : null}
          {value.state === 'error' ? <p className={styles.warning}>Request failed · {value.detail}</p> : null}
          {result ? <><p className={styles.note}>Earlier result · last completed destination action · {result.completedAt ?? 'completion time unknown'}. The target below is the submitted target, not necessarily the current selection.</p><p className={styles.state}>{result.kind === 'route' ? 'Route lookup' : result.kind === 'dns' ? 'DNS lookup' : 'TCP handshake'} · {result.outcome}</p></> : null}
        </div>
        <div className={styles.facts}><KeyValueList>
          <KeyValue label="Selected interface"><span className="mono">{selectedInterface}</span></KeyValue>
          {result ? <>
            <KeyValue label="Target"><span className="mono">{result.target}</span></KeyValue>
            <KeyValue label="Source"><Value value={result.source} mono /></KeyValue>
            <KeyValue label="Execution vantage"><Value value={result.vantage} mono /></KeyValue>
            <KeyValue label="Started"><Value value={result.startedAt} mono /></KeyValue>
            <KeyValue label="Completed"><Value value={result.completedAt} mono /></KeyValue>
            <KeyValue label="Selected egress">{result.egress ? (
              result.egress.objectId ? <Link to={`/network/${encodeURIComponent(result.egress.objectId)}?tab=connectivity`}>{result.egress.name}</Link> : <span className="mono">{result.egress.name}</span>
            ) : <Value value={null} reason="No egress evidence was supplied" />}</KeyValue>
            {result.facts.map((fact) => <KeyValue key={fact.label} label={fact.label}><Value value={fact.value} mono /></KeyValue>)}
          </> : null}
        </KeyValueList></div>
        {result ? <p className={styles.note}>{result.detail}</p> : null}
        <p className={styles.note}>The selected workspace interface does not bind diagnostic egress. A route is not reachability; a connection is not service health. Neither confirms the management path.</p>
        {value.state !== 'idle' ? <p className={styles.note}>Leaving this view does not cancel execution. Results are view-held, not durable diagnostic history. Retry only with an explicit action.</p> : null}
      </PlateBody>
    </Plate>
  );
}

/** Shared composition; controlled props also support isolated presentation fixtures. */
export function ConnectivityPanel({ selectedInterface, comparison = waitingComparison,
  policy = { state: 'waiting', detail: 'Policy has not been read.' }, result = { state: 'idle' },
  configuration, workbench, context, attempt,
}: {
  selectedInterface: string;
  comparison?: ConnectivityComparison;
  policy?: DiagnosticPolicyView;
  result?: ConnectivityResultState;
  configuration?: ReactNode;
  workbench?: ReactNode;
  context?: ReactNode;
  attempt?: ConnectivityAttempt;
}): JSX.Element {
  return (
    <div className={styles.stack}>
      <Plate>
        <PlateHead title="Connectivity" level={3} meta="configuration, observation, destination" />
        <PlateBody>
          <p className={styles.note}>Profile configuration, applied configuration and kernel observations are independently dated sources. Disagreement alone is not a LocalPlane drift verdict.</p>
          {!workbench ? <p className={styles.waiting}>Connectivity reads and controls are not connected yet. No observation or diagnostic has been requested.</p> : null}
          {context}
        </PlateBody>
      </Plate>
      {configuration ?? <><div className={styles.comparison}>
        <ConnectivityEvidencePlate title="Profile configuration" evidence={comparison.profile} />
        <ConnectivityEvidencePlate title="Applied configuration" evidence={comparison.applied} />
        <ConnectivityEvidencePlate title="Observed kernel facts" evidence={comparison.kernel} />
      </div>
      <div className={styles.inventory}>
        <ConnectivityEvidencePlate title="Installed routes" evidence={comparison.routes} />
        <ConnectivityEvidencePlate title="Routing rules" evidence={comparison.rules} />
      </div>
      <ConnectivityEvidencePlate title="Resolver evidence" evidence={comparison.resolver} />
      <p className={styles.note}>Keep profile and applied DNS contributions, resolver global/per-link routing domains and client stub configuration separate. A stub is not an upstream server; a current server is not proof it answered a query.</p>
      </>}
      {workbench ?? <Plate>
        <PlateHead title="Destination workbench" level={3} />
        <PlateBody>
          <p className={styles.note}>Each action is deliberate and independent. No automatic probes, retries or DNS-to-TCP chaining.</p>
          <div className={styles.actions}>
            <div><button type="button" disabled>Look up numeric route</button><p className={styles.note}>Passive kernel lookup · no packets. Awaiting connection to the route API.</p></div>
            <div><button type="button" disabled>Resolve authorized name</button><p className={styles.note}>A or AAAA through the host resolver. Awaiting policy and diagnostic API.</p></div>
            <div><button type="button" disabled>Check authorized TCP endpoint</button><p className={styles.note}>Numeric IP:port · handshake only, no payload. Awaiting policy and diagnostic API.</p></div>
          </div>
          <ConnectivityPolicy policy={policy} />
        </PlateBody>
      </Plate>}
      <ConnectivityResult selectedInterface={selectedInterface} value={result} {...(attempt ? { attempt } : {})} />
    </div>
  );
}
