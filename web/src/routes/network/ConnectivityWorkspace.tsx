import type { ManagementPath, NetworkInterface, ObjectProtection } from '@/api/types';
import type { Resource } from '@/hooks/useResource';
import { Link } from 'react-router-dom';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { ResourceView } from '@/components/states/ResourceView';
import { Value } from '@/components/semantic/UnknownValue';
import { Disclosure, Gaps } from '@/components/semantic/Evidence';
import { ConnectivityPanel, ConnectivityPolicy, type ConnectivityActionEvidence, type DiagnosticPolicyView } from './ConnectivityPanel';
import { ConnectivitySnapshotView } from './ConnectivitySnapshotView';
import { isNumericDestination, tcpTargetKey, type ConnectivityController, type DestinationResult } from './useConnectivity';
import styles from './ConnectivityPanel.module.css';
import { tcpPolicyUnavailableReason } from './connectivityPolicy';

function destinationEvidence(result: DestinationResult): ConnectivityActionEvidence {
  if (!('kind' in result)) return {
    kind: 'route', target: result.destination, outcome: result.status, detail: result.detail,
    source: null, vantage: result.host_id ?? null, startedAt: null, completedAt: result.observed_at ?? null,
    egress: result.egress_interface || result.egress_object_id ? { name: result.egress_interface ?? result.egress_object_id!, objectId: result.egress_object_id ?? null } : null,
    facts: [
      { label: 'Query context', value: result.query_context ?? null },
      { label: 'Egress ifindex', value: result.egress_ifindex === null || result.egress_ifindex === undefined ? null : String(result.egress_ifindex) },
      { label: 'Agent instance', value: result.agent_instance_id ?? null },
      { label: 'Provider version', value: result.provider_version ?? null },
      { label: 'Source address', value: result.source ?? null },
      { label: 'Gateway', value: result.gateway ?? null },
      { label: 'Routing table', value: result.table ?? null },
      { label: 'Route type', value: result.route_type ?? null },
      { label: 'Metric', value: result.metric === null || result.metric === undefined ? null : String(result.metric) },
    ],
  };
  const common = {
    outcome: result.outcome, detail: result.detail, source: null, vantage: result.vantage ?? null,
    startedAt: result.started_at ?? null, completedAt: result.ended_at ?? null, egress: null,
  };
  return result.kind === 'dns' ? {
    ...common, kind: 'dns', target: `${result.name} (${result.record_type})`,
    facts: [
      { label: 'Answers', value: result.answers === null || result.answers === undefined ? null : result.answers.length ? result.answers.join(', ') : 'No answers reported' },
      { label: 'Answers truncated', value: result.truncated === null || result.truncated === undefined ? null : result.truncated ? 'yes' : 'no' },
    ],
  } : {
    ...common, kind: 'tcp', target: tcpTargetKey(result),
    facts: [
      { label: 'Actual local socket', value: result.local ? tcpTargetKey(result.local) : null },
      { label: 'Actual remote socket', value: result.remote ? tcpTargetKey(result.remote) : null },
    ],
  };
}

export function ConnectivityWorkspace({ controller: c, iface, managementPath, protection }: {
  controller: ConnectivityController;
  iface: NetworkInterface;
  managementPath: Resource<ManagementPath>;
  protection: Resource<ObjectProtection>;
}): JSX.Element {
  const s = c.state;
  const policy = s.snapshot?.policy;
  const dns = policy?.status === 'valid' ? policy.dns ?? [] : [];
  const tcp = policy?.status === 'valid' ? policy.tcp ?? [] : [];
  const policyView: DiagnosticPolicyView = !policy ? { state: 'waiting', detail: 'Read stored policy metadata before an active action.' }
    : policy.status === 'missing' ? { state: 'ready', dnsNames: [], tcpEndpoints: [] }
      : policy.status === 'valid' && Array.isArray(policy.dns) && Array.isArray(policy.tcp) ? { state: 'ready', dnsNames: policy.dns, tcpEndpoints: policy.tcp.map(tcpTargetKey) }
        : { state: 'blocked', detail: `Policy status: ${policy.status}. Authorization has not been established; passive lookup remains separate.` };
  const busy = !!s.pending || s.reading;
  const current = iface.observed_in_latest_sweep === true && iface.observation?.freshness === 'current';
  const activePolicy = policy?.status === 'valid' && policy.dns !== null && policy.tcp !== null && !!policy.digest && /^[0-9a-f]{64}$/.test(policy.digest)
    && !s.policyRefused && !s.readError;
  const tcpPolicyReady = tcpPolicyUnavailableReason(policy) === null;
  const workbench = <Plate>
    <PlateHead title="Destination workbench" level={3} />
    <PlateBody>
      <div className={styles.toolbar}>
        <button type="button" disabled={busy} onClick={() => c.reread()}>Re-read stored evidence</button>
        <button type="button" disabled={busy} onClick={() => { void c.observe(); }}>Observe connectivity</button>
      </div>
      <p className={styles.note}>Re-read loads stored evidence. Observe collects passive evidence, then re-reads it; neither sends DNS or TCP diagnostic packets.</p>
      <div role="status" aria-live="polite" aria-atomic="true">
        {s.reading ? <p className={styles.state}>Reading stored connectivity evidence…</p> : null}
        {s.pending ? <p className={styles.state}>Waiting for {s.pending === 'observe' ? 'passive observation' : s.pending} result…</p> : null}
      </div>
      {s.readError ? <p role="alert" className={styles.warning}>Stored read failed · {s.readError}. Previous evidence and results are retained.</p> : null}
      {s.actionError ? <p role="alert" className={styles.warning}>Action failed · {s.actionError}</p> : null}
      {!current ? <p className={styles.warning}>Selected interface membership or freshness is not current. Active DNS and TCP actions are disabled. <Link to="/network">Open Network inventory to observe interfaces</Link>, use its explicit “Observe network” control, then reopen this interface. Observe connectivity does not refresh primary interface inventory. Passive route lookup remains destination-only; it does not bind the selected interface.</p> : null}
      {s.policyRefused ? <p className={styles.warning}>Diagnostic policy was refused or changed. Re-read stored evidence and review the exact authorized targets before an explicit retry.</p> : null}
      <div className={styles.actions}>
        <form aria-label="Passive route lookup" onSubmit={(event) => { event.preventDefault(); void c.route(); }}>
          <label className={styles.field}>Numeric route destination<input value={s.destination} maxLength={45} autoComplete="off" spellCheck={false} onChange={(event) => c.update({ destination: event.target.value })} placeholder="IPv4 or IPv6 address" /></label>
          <p className={styles.note}>One numeric address, no URL, prefix or zone. Kernel lookup only; no packets.</p>
          <button type="submit" disabled={busy || !isNumericDestination(s.destination)}>Look up route</button>
        </form>
        <form aria-label="Authorized DNS lookup" onSubmit={(event) => { event.preventDefault(); void c.diagnostic('dns'); }}>
          <label className={styles.field}>Authorized DNS name<select value={s.dnsName} onChange={(event) => c.update({ dnsName: event.target.value })}>
            <option value="">Select an authorized name</option>
            {s.dnsName && !dns.includes(s.dnsName) ? <option value={s.dnsName} disabled>{s.dnsName} — no longer authorized</option> : null}
            {dns.map((name) => <option key={name} value={name}>{name}</option>)}
          </select></label>
          <label className={styles.field}>DNS record type<select value={s.recordType} onChange={(event) => c.update({ recordType: event.target.value === 'AAAA' ? 'AAAA' : 'A' })}><option>A</option><option>AAAA</option></select></label>
          <p className={styles.note}>Uses the configured host resolver.</p>
          <button type="submit" disabled={busy || !current || !activePolicy || !dns.includes(s.dnsName)}>Resolve name</button>
        </form>
        <form aria-label="Configured TCP handshake" onSubmit={(event) => { event.preventDefault(); void c.diagnostic('tcp'); }}>
          <label className={styles.field}>Configured TCP endpoint<select value={s.tcpTarget} onChange={(event) => c.update({ tcpTarget: event.target.value })}>
            <option value="">Select an exact IP:port pair</option>
            {s.tcpTarget && !tcp.some((target) => tcpTargetKey(target) === s.tcpTarget) ? <option value={s.tcpTarget} disabled>{s.tcpTarget} — no longer authorized</option> : null}
            {tcp.map((target) => <option key={tcpTargetKey(target)} value={tcpTargetKey(target)}>{tcpTargetKey(target)}</option>)}
          </select></label>
          {s.tcpTarget ? <p className={styles.note}>Selected pair: <span className="mono">{s.tcpTarget}</span></p> : null}
          <p className={styles.note}>Handshake and close only; no payload, TLS or banner read.</p>
          <button type="submit" disabled={busy || !current || !activePolicy || !tcpPolicyReady || !tcp.some((target) => tcpTargetKey(target) === s.tcpTarget)}>Check TCP endpoint</button>
        </form>
      </div>
      <ConnectivityPolicy policy={policyView} metadata={policy ?? null} />
      <p className={styles.note}>Policy is administrator-provisioned outside the product; there is no UI editor or request override.</p>
      <p className={styles.note}>Each submission is independent; leaving the view does not cancel execution.</p>
      <Disclosure summary="Diagnostic mechanics">
        <p className={styles.note}>Policy location: <span className="mono">/etc/localplane/diagnostic-targets.json</span>. Backend authorization and resource limits remain authoritative.</p>
        <p className={styles.note}>Resolver cache, aliases and retries are resolver-controlled. A refused connection does not identify the rejector; timeout does not identify a firewall cause.</p>
        <p className={styles.note}>No DNS-to-TCP chaining, background probes, automatic retries or cancellation guarantee. Reopening never resubmits an action.</p>
      </Disclosure>
    </PlateBody>
  </Plate>;

  return <ConnectivityPanel selectedInterface={iface.name} workbench={workbench}
    {...(s.result ? { attempt: { pending: s.pending ? `${s.pending} · ${s.pendingTarget ?? 'passive provider observation'}` : null, error: s.actionError } } : {})}
    result={s.result ? { state: 'complete', result: destinationEvidence(s.result) }
      : s.pending && s.pending !== 'observe' ? { state: 'pending', target: s.pendingTarget ?? 'unknown target' }
        : s.actionError ? { state: 'error', detail: s.actionError } : { state: 'idle' }}
    configuration={s.snapshot && s.readAt ? <ConnectivitySnapshotView snapshot={s.snapshot} readAt={s.readAt} /> : <p className={styles.note}>Connectivity source layers have not been read. Missing evidence is not an empty configuration.</p>}
    context={<>
      <div className={styles.facts}><KeyValueList>
        <KeyValue label="Selected interface"><span className="mono">{iface.name} · {iface.object_id}</span></KeyValue>
        <KeyValue label="Interface observation"><Value value={iface.observation?.observed_at} mono /></KeyValue>
        <KeyValue label="Stored connectivity read"><Value value={s.readAt?.toISOString()} mono /></KeyValue>
      </KeyValueList></div>
      <ResourceView resource={managementPath} what="management path">
        {(path) => <><div className={styles.facts}><KeyValueList>
          <KeyValue label="Current management path">{path.state} · {path.reason}</KeyValue>
          <KeyValue label="Management interface"><Value value={path.object_name} mono /></KeyValue>
          <KeyValue label="Assessment as of"><Value value={path.as_of} mono /></KeyValue>
          <KeyValue label="Proof observed at"><Value value={path.evidence?.observed_at} mono /></KeyValue>
          <KeyValue label="Proof TTL (seconds)"><Value value={path.evidence_ttl_seconds} mono /></KeyValue>
          <KeyValue label="Current accepted transport peer"><Value value={path.transport.peer_address} mono /></KeyValue>
          <KeyValue label="Current accepted local endpoint"><Value value={path.transport.local_endpoint_address} mono /></KeyValue>
          <KeyValue label="Transport usable for assessment">{path.transport.usable ? 'yes' : 'no'} · <Value value={path.transport.reason} /></KeyValue>
        </KeyValueList></div>
        {path.evidence ? <Disclosure summary="Stored management return-route proof"><KeyValueList>
          <KeyValue label="Stored proof provider / method">{path.evidence.provider} · {path.evidence.method}</KeyValue>
          <KeyValue label="Stored proof transport peer"><Value value={path.evidence.transport_peer_address} mono /></KeyValue>
          <KeyValue label="Stored proof local endpoint"><Value value={path.evidence.local_endpoint_address} mono /></KeyValue>
          <KeyValue label="Stored return-route status">{path.evidence.route.status} · <Value value={path.evidence.route.reason} /></KeyValue>
          <KeyValue label="Stored return-route source address"><Value value={path.evidence.route.preferred_source} mono /></KeyValue>
          <KeyValue label="Stored return-route egress ifindex"><Value value={path.evidence.route.oif_index} mono /></KeyValue>
          <KeyValue label="Stored return-route table"><Value value={path.evidence.route.table} mono /></KeyValue>
          <KeyValue label="Stored return-route gateway"><Value value={path.evidence.route.gateway} mono /></KeyValue>
          <KeyValue label="Stored return-route destination"><Value value={path.evidence.route.destination} mono /></KeyValue>
          <KeyValue label="Stored return-route proof observed at"><Value value={path.evidence.observed_at} mono /></KeyValue>
        </KeyValueList></Disclosure> : <p className={styles.note}>No stored return-route proof is present. Current accepted transport facts above do not by themselves confirm a management path.</p>}
        <Gaps items={path.missing_evidence} /></>}
      </ResourceView>
      <ResourceView resource={protection} what="interface protection">
        {(guard) => <KeyValueList><KeyValue label="Protection">{guard.status} · {guard.reason}</KeyValue><KeyValue label="Management path relation">{guard.management_path}</KeyValue></KeyValueList>}
      </ResourceView>
      <p className={styles.note}>Management-path and protection statements above come from their independent assessments, never from destination action results. Egress links appear only when the backend supplies an object identity; no interface relationship is guessed from a name.</p>
    </>}
  />;
}
