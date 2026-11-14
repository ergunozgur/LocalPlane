import type { ConnectivityApplied, ConnectivityEvidence as ApiEvidence, ConnectivityIPSettings, ConnectivityProfile, ConnectivityRoute, ConnectivitySnapshot } from '@/api/types';
import { Link } from 'react-router-dom';
import { PlateBody } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { DataTable } from '@/components/primitives/DataTable';
import { Value } from '@/components/semantic/UnknownValue';
import { Disclosure } from '@/components/semantic/Evidence';
import { ConnectivityEvidencePlate, type ConnectivityEvidence } from './ConnectivityPanel';
import styles from './ConnectivityPanel.module.css';
import local from './ConnectivitySnapshotView.module.css';

function list(items: readonly string[] | null | undefined): string | null {
  return items === null || items === undefined ? null : items.length ? items.join(', ') : 'None reported';
}
function domains(items: readonly { name: string; route_only?: boolean | null }[] | null | undefined): string | null {
  return list(items?.map((domain) => `${domain.name} (route-only: ${domain.route_only === null || domain.route_only === undefined ? 'unknown' : domain.route_only ? 'yes' : 'no'})`));
}

function FamilySettings({ settings, kind }: { settings: ConnectivityIPSettings[] | null | undefined; kind: 'profile' | 'applied' }): JSX.Element {
  if (settings === null || settings === undefined) return <p className={styles.warning}>IP-family settings not observed; configuration methods remain unknown.</p>;
  if (!settings.length) return <p className={styles.note}>No IP-family settings reported; no method or automatic contribution is inferred.</p>;
  return <>{settings.map((setting, index) => {
    const family = setting.family === 'ipv4' ? 'IPv4' : 'IPv6';
    return <Disclosure key={`${setting.family}-${index}`} summary={`${family} configuration · ${setting.method ?? 'method unknown'}`}>
      <section aria-label={`${kind === 'profile' ? 'Profile' : 'Applied'} ${family} settings`}>
        <KeyValueList>
          <KeyValue label="Family"><span className="mono">{setting.family}</span></KeyValue>
          <KeyValue label="Method"><Value value={setting.method} mono /></KeyValue>
          <KeyValue label="Gateway"><Value value={setting.gateway} mono /></KeyValue>
          <KeyValue label="Configured route metric">{setting.route_metric === -1
            ? 'Automatic / provider-selected (configured -1; effective metric unknown)'
            : <Value value={setting.route_metric} mono />}</KeyValue>
          <KeyValue label="DNS priority"><Value value={setting.dns_priority} mono /></KeyValue>
          <KeyValue label="Ignore automatic DNS"><Value value={setting.ignore_auto_dns === null || setting.ignore_auto_dns === undefined ? null : setting.ignore_auto_dns ? 'yes' : 'no'} /></KeyValue>
          <KeyValue label="Family DNS contribution"><Value value={list(setting.dns_servers)} mono /></KeyValue>
        </KeyValueList>
        {setting.method === 'auto' ? <p className={styles.note}>Automatic configuration can supply runtime addresses, routes and DNS even when configured lists are empty.</p> : null}
      </section>
    </Disclosure>;
  })}</>;
}

function evidenceView(evidence: ApiEvidence, readAt: Date): ConnectivityEvidence {
  const observed = evidence.observed_at ? Date.parse(evidence.observed_at) : NaN;
  const age = readAt.getTime() - observed;
  return {
    state: evidence.status === 'observed' ? evidence.completeness === 'complete' ? 'complete' : 'partial'
      : evidence.status === 'unsupported' ? 'unavailable' : evidence.status,
    source: evidence.source ?? null, observedAt: evidence.observed_at ?? null,
    age: Number.isFinite(age) && age >= 0 ? `${Math.floor(age / 1000)}s at stored read` : null,
    freshness: evidence.freshness,
    detail: `${evidence.status} · completeness ${evidence.completeness}.`,
    gaps: evidence.gaps ?? [], facts: [
      { label: 'Provider version', value: evidence.provider_version ?? null },
      { label: 'Vantage', value: evidence.vantage ?? null },
      { label: 'Host identity', value: evidence.host_id ?? null },
      { label: 'Agent instance', value: evidence.agent_instance_id ?? null },
    ].filter((fact) => fact.value !== null),
  };
}

export function ConnectivityRoutes({ routes, title }: { routes: ConnectivityRoute[] | null | undefined; title: string }): JSX.Element {
  if (routes === null || routes === undefined) return <PlateBody><p className={styles.warning}>Route entries unknown; this is not an empty route inventory.</p></PlateBody>;
  if (!routes.length) return <PlateBody><p className={styles.note}>No route entries reported in this section. See source completeness above.</p></PlateBody>;
  return <DataTable caption={title} rows={routes.map((route, row) => ({ ...route, row }))} rowKey={(r) => String(r.row)} columns={[
    { key: 'family', header: 'Family', render: (r) => r.family },
    { key: 'destination', header: 'Prefix / destination', render: (r) => <span className="mono">{r.destination}</span> },
    { key: 'nexthops', header: 'Next hops', render: (r) => r.nexthops === null || r.nexthops === undefined ? <Value value={null} /> : !r.nexthops.length ? 'None reported' : <ul className={local.hops}>{r.nexthops.map((hop, i) => <li key={i}><KeyValueList>
      <KeyValue label="Gateway"><Value value={hop.gateway} mono /></KeyValue>
      <KeyValue label="Interface">{hop.object_id ? <Link className="mono" to={`/network/${encodeURIComponent(hop.object_id)}?tab=connectivity`}>{hop.interface ?? hop.object_id}</Link> : <Value value={hop.interface} mono />}</KeyValue>
      <KeyValue label="Ifindex"><Value value={hop.ifindex} mono /></KeyValue>
      <KeyValue label="Weight"><Value value={hop.weight} mono /></KeyValue>
    </KeyValueList></li>)}</ul> },
    { key: 'table', header: 'Table', render: (r) => <Value value={r.table} mono /> },
    { key: 'metric', header: 'Metric', render: (r) => <Value value={r.metric} mono /> },
    { key: 'type', header: 'Type', render: (r) => <Value value={r.route_type} mono /> },
    { key: 'protocol', header: 'Protocol', render: (r) => <Value value={r.protocol} mono /> },
    { key: 'scope', header: 'Scope', render: (r) => <Value value={r.scope} mono /> },
  ]} />;
}

function ConfigurationFacts({ value, kind }: { value: ConnectivityProfile | ConnectivityApplied | null | undefined; kind: 'profile' | 'applied' }): JSX.Element {
  if (!value) return <PlateBody><p className={styles.warning}>Configuration not established by this source.</p></PlateBody>;
  const profile = kind === 'profile' ? value as ConnectivityProfile : null;
  return <>
    <PlateBody><KeyValueList>
      <KeyValue label="Provider"><Value value={value.provider} mono /></KeyValue>
      {profile ? <>
        <KeyValue label="Profile UUID"><Value value={profile.uuid} mono /></KeyValue>
        <KeyValue label="Profile name"><Value value={profile.name} /></KeyValue>
        <KeyValue label="Profile state"><Value value={profile.state} /></KeyValue>
        <KeyValue label="Profile version"><Value value={profile.version} mono /></KeyValue>
        <KeyValue label="Unsaved"><Value value={profile.unsaved === null || profile.unsaved === undefined ? null : String(profile.unsaved)} /></KeyValue>
        <KeyValue label="Generated"><Value value={profile.generated === null || profile.generated === undefined ? null : String(profile.generated)} /></KeyValue>
        <KeyValue label="External"><Value value={profile.external === null || profile.external === undefined ? null : String(profile.external)} /></KeyValue>
        <KeyValue label="Device identity"><Value value={profile.device_identity} mono /></KeyValue>
        <KeyValue label="Device ifindex"><Value value={profile.device_ifindex} mono /></KeyValue>
        <KeyValue label="Active connection identity"><Value value={profile.active_connection_identity} mono /></KeyValue>
      </> : null}
      {kind === 'applied' ? <KeyValue label="Applied version"><Value value={value.version} mono /></KeyValue> : null}
      <KeyValue label="Addresses"><Value value={list(value.addresses?.map((a) => `${a.address}/${a.prefix ?? '?'}`))} mono /></KeyValue>
      <KeyValue label="DNS contribution"><Value value={list(value.dns_servers)} mono /></KeyValue>
      <KeyValue label="Search domains"><Value value={list(value.search_domains)} mono /></KeyValue>
      <KeyValue label="DNS routing domains"><Value value={domains(value.routing_domains)} mono /></KeyValue>
    </KeyValueList>
    <p className={styles.note}>Configuration contribution only; empty configured DNS is not global DNS absence.</p>
    <FamilySettings settings={value.ip_settings} kind={kind} />
    </PlateBody>
    <ConnectivityRoutes routes={value.routes} title={kind === 'profile' ? 'Profile routes' : 'Applied routes'} />
  </>;
}

export function ConnectivitySnapshotView({ snapshot: s, readAt }: { snapshot: ConnectivitySnapshot; readAt: Date }): JSX.Element {
  return <div className={local.snapshot}>
    <p className={styles.note}>Stored evidence, not live health. Each source has its own observation time, freshness and gaps.</p>
    <div className={styles.comparison}>
      <ConnectivityEvidencePlate title="Profile configuration" evidence={evidenceView(s.profile.evidence, readAt)}><ConfigurationFacts value={s.profile.value} kind="profile" /></ConnectivityEvidencePlate>
      <ConnectivityEvidencePlate title="Applied configuration" evidence={evidenceView(s.applied.evidence, readAt)}><ConfigurationFacts value={s.applied.value} kind="applied" /></ConnectivityEvidencePlate>
      <ConnectivityEvidencePlate title="Observed kernel facts" evidence={{ ...evidenceView(s.kernel.evidence, readAt), facts: [...evidenceView(s.kernel.evidence, readAt).facts, { label: 'Addresses', value: list(s.kernel.value?.addresses?.map((a) => `${a.address}/${a.prefix ?? '?'}`)) }] }} />
    </div>
    <Disclosure summary="How to compare configuration sources">
      <p className={styles.note}>Profile, applied and kernel facts are independent readings, not a drift verdict. Empty configured addresses, routes or DNS describe only that contribution; automatic configuration may supply different runtime values.</p>
      <p className={styles.note}>Family methods and priorities are shown only when reported. DNS priority is not an IP route metric, and no default metric or resolver selection is inferred. See the Connectivity diagnostics documentation for provider and collection mechanics.</p>
    </Disclosure>
    <ConnectivityEvidencePlate title="Installed routes" evidence={evidenceView(s.routes.evidence, readAt)}><ConnectivityRoutes routes={s.routes.value} title="Installed routes" /></ConnectivityEvidencePlate>
    <ConnectivityEvidencePlate title="Routing rules" evidence={evidenceView(s.rules.evidence, readAt)}>
      {s.rules.value === null || s.rules.value === undefined ? <PlateBody><p className={styles.warning}>Routing rules unknown, not empty.</p></PlateBody> : !s.rules.value.length ? <PlateBody><p className={styles.note}>No rules reported. See source completeness above.</p></PlateBody> : <DataTable caption="Routing rules" rows={s.rules.value.map((rule, row) => ({ ...rule, row }))} rowKey={(r) => String(r.row)} columns={[
        { key: 'family', header: 'Family', render: (r) => r.family },
        { key: 'priority', header: 'Priority', render: (r) => <Value value={r.priority} mono /> },
        { key: 'source', header: 'Source', render: (r) => <Value value={r.source} mono /> },
        { key: 'destination', header: 'Destination', render: (r) => <Value value={r.destination} mono /> },
        { key: 'table', header: 'Table', render: (r) => <Value value={r.table} mono /> },
        { key: 'action', header: 'Action', render: (r) => <Value value={r.action} mono /> },
        { key: 'selectors', header: 'Additional selectors', render: (r) => <Disclosure summary="Rule selectors"><KeyValueList>
          <KeyValue label="Firewall mark"><Value value={r.fwmark} mono /></KeyValue>
          <KeyValue label="Firewall mask"><Value value={r.fwmask} mono /></KeyValue>
          <KeyValue label="Incoming interface"><Value value={r.incoming_interface} mono /></KeyValue>
          <KeyValue label="Outgoing interface"><Value value={r.outgoing_interface} mono /></KeyValue>
          <KeyValue label="UID range"><Value value={r.uid_range} mono /></KeyValue>
          <KeyValue label="Suppress prefix length"><Value value={r.suppress_prefix_length} mono /></KeyValue>
        </KeyValueList><p className={styles.note}>Unknown selector fields are not assumed absent. This inventory is not a reimplementation of kernel route selection.</p></Disclosure> },
      ]} />}
    </ConnectivityEvidencePlate>
    <ConnectivityEvidencePlate title="Resolver evidence" evidence={evidenceView(s.resolver.evidence, readAt)}>
      <PlateBody><KeyValueList>
        <KeyValue label="Global DNS servers"><Value value={list(s.resolver.value?.global_servers)} mono /></KeyValue>
        <KeyValue label="Global domains"><Value value={list(s.resolver.value?.global_domains)} mono /></KeyValue>
        <KeyValue label="Global routing domains"><Value value={domains(s.resolver.value?.global_routing_domains)} mono /></KeyValue>
        <KeyValue label="Resolver-selected current server"><Value value={s.resolver.value?.current_server} mono /></KeyValue>
        <KeyValue label="Current-server scope ifindex"><Value value={s.resolver.value?.current_server_ifindex} mono /></KeyValue>
      </KeyValueList>
      <p className={styles.note}>Current server is not proof of the server that answered a diagnostic. DNS default-route is not the IP default route.</p>
      <Disclosure summary="Resolver scope and domain details">
        <p className={styles.note}>Current-server scope is resolver-global for ifindex 0 or link-scoped for a reported positive ifindex. Missing scope stays unknown; the server is not added to the global server list.</p>
        <p className={styles.note}>Domains prefixed with ~ are route-only domains. Profile and applied DNS contributions are separate from resolver observations. A client stub (such as 127.0.0.53) is not an upstream server.</p>
      </Disclosure></PlateBody>
      {s.resolver.value?.links === null || s.resolver.value?.links === undefined ? <PlateBody><p className={styles.warning}>Per-link resolver evidence unknown.</p></PlateBody> : !s.resolver.value.links.length ? <PlateBody><p className={styles.note}>No per-link resolver entries reported. See source completeness above.</p></PlateBody> : <DataTable caption="Per-link resolver" rows={s.resolver.value.links.map((link, row) => ({ ...link, row }))} rowKey={(r) => String(r.row)} columns={[
        { key: 'interface', header: 'Interface', render: (r) => <Value value={r.interface} mono /> },
        { key: 'servers', header: 'Servers', render: (r) => <Value value={list(r.servers)} mono /> },
        { key: 'domains', header: 'Domains (including route-only)', render: (r) => <Value value={list(r.domains)} mono /> },
        { key: 'routing_domains', header: 'Explicit routing domains', render: (r) => <Value value={domains(r.routing_domains)} mono /> },
        { key: 'current', header: 'Current server', render: (r) => <Value value={r.current_server} mono /> },
        { key: 'default', header: 'DNS default-route', render: (r) => <Value value={r.dns_default_route === null || r.dns_default_route === undefined ? null : r.dns_default_route ? 'yes' : 'no'} /> },
      ]} />}
    </ConnectivityEvidencePlate>
    <ConnectivityEvidencePlate title="Client resolver configuration" evidence={{ ...evidenceView(s.client_resolver.evidence, readAt), facts: [...evidenceView(s.client_resolver.evidence, readAt).facts,
      { label: 'Client mode', value: s.client_resolver.value?.client_mode ?? null },
      { label: 'Client nameservers', value: list(s.client_resolver.value?.client_nameservers) },
    ] }} />
    <p className={styles.note}>A client stub is not an upstream server. Missing provider evidence does not invalidate independent kernel facts.</p>
  </div>;
}
