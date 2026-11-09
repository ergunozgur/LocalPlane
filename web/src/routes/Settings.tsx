/**
 * Settings.
 *
 * Browser preferences, effective policy and read-only installation diagnostics.
 *
 * The information architecture is what makes them additive: each area is a section on this
 * page or a sibling route, and per-user preferences already have a scope in
 * `PreferencesProvider`.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { AppearanceSelect } from '@/components/layout/AppearanceSelect';
import { PageHeader } from './PageHeader';
import styles from './Settings.module.css';
import { useViewer } from '@/identity/viewer';
import { endpoints } from '@/api/endpoints';
import type { Installation, InstallationBuild } from '@/api/types';
import { ApiError } from '@/api/client';

export function Settings(): JSX.Element {
  const viewer = useViewer();

  return (
    <>
      <PageHeader
        title="Settings"
        annotation="Preferences here affect how this browser presents LocalPlane. None of them is consulted by any safety or domain judgement, and none of them is authority for anything."
      />

      <Plate>
        <PlateHead title="Appearance" level={3} meta="how this console looks" />
        <PlateBody>
          <KeyValueList>
            <KeyValue label="Theme" hint="stored in this browser">
              <AppearanceSelect />
            </KeyValue>
          </KeyValueList>
        </PlateBody>
      </Plate>

      {/* Console settings. These are real LocalPlane concepts — the confirmation
          policy is published in every plan, the guard window is a policy constant, and the
          observation interval governs freshness — but no settings endpoint exists, so each
          states what is in force and that it cannot be changed here. */}
      <Plate style={{ marginTop: 14 }}>
        <PlateHead
          title="Console settings"
          level={3}
          meta="what governs observation and writes on this host"
        />
        <PlateBody>
          <KeyValueList>
            <KeyValue label="Observation interval" hint="not configurable in this build">
              <span className={styles.deferred}>
                Observation is taken on request and at startup. Nothing polls, and there is no
                interval to set.
              </span>
            </KeyValue>
            <KeyValue label="Connection guard" hint="not configurable in this build">
              <span className={styles.deferred}>
                The guard window is a policy constant published in each plan, not a setting.
                There is no request field for it anywhere in the API.
              </span>
            </KeyValue>
            <KeyValue label="Confirmations" hint="not configurable in this build">
              <span className={styles.deferred}>
                Required for medium and high risk, for anything that can remove the management
                path, and for anything that cannot be undone. The policy in force is recorded
                on every plan.
              </span>
            </KeyValue>
          </KeyValueList>
          <p className="note" style={{ marginTop: 11, maxWidth: '80ch' }}>
            No settings endpoint exists in this build. These are stated so the policy in force
            is visible, not so it can be edited here.
          </p>
        </PlateBody>
      </Plate>

      <Plate style={{ marginTop: 14 }}>
        <PlateHead title="Identity" level={3} meta="who this session is attributed to" />
        <PlateBody>
          <KeyValueList>
            <KeyValue label="Viewer">{viewer.displayName}</KeyValue>
            <KeyValue label="User id" hint="this build has no user model">
              <span className="mono">{viewer.id ?? 'none'}</span>
            </KeyValue>
            <KeyValue label="Attribution">
              <span className="mono">{viewer.attribution}</span>
            </KeyValue>
          </KeyValueList>
          <p className="note" style={{ marginTop: 11, maxWidth: '80ch' }}>
            This browser crossed the authentication boundary, but LocalPlane has no user model.
            New confirmations record <span className="mono">authenticated_request</span> without
            inventing a person; historical records retain their original source. Nothing here grants or withholds permission: the backend
            refuses what must be refused whether or not this console draws a control.
          </p>
        </PlateBody>
      </Plate>

      <InstallationPlate />
    </>
  );
}

const object = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null;
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every((v) => typeof v === 'string');
const nullableString = (value: unknown) => value === null || typeof value === 'string';
const buildShape = (value: unknown): boolean => object(value) && typeof value.status === 'string'
  && ['build_id', 'version', 'source_commit', 'source_tree', 'source_digest'].every((k) => nullableString(value[k]))
  && (value.source_dirty === null || typeof value.source_dirty === 'boolean') && strings(value.reasons);
const errorShape = (value: unknown): boolean => value === null || (object(value)
  && typeof value.code === 'string' && typeof value.message === 'string');
const maintenanceShape = (value: unknown): boolean => object(value) && typeof value.status === 'string'
  && typeof value.active === 'boolean' && typeof value.as_of === 'string' && errorShape(value.refusal)
  && (value.window === null || (object(value.window) && typeof value.window.window_id === 'string'
    && typeof value.window.state === 'string'));

function installationShape(value: unknown): value is Installation {
  if (!object(value) || typeof value.generated_at !== 'string' || !strings(value.gaps) || !buildShape(value.backend)
    || !maintenanceShape(value.maintenance) || !object(value.agent) || !object(value.console)
    || !object(value.schema_identity) || !object(value.providers)) return false;
  const { agent, console: consoleIdentity, schema_identity: schema, providers } = value;
  return typeof agent.reachable === 'boolean' && typeof agent.source === 'string' && typeof agent.as_of === 'string'
    && nullableString(agent.agent_instance_id) && (agent.build === null || buildShape(agent.build))
    && errorShape(agent.error) && (agent.maintenance === null || maintenanceShape(agent.maintenance))
    && typeof consoleIdentity.status === 'string' && nullableString(consoleIdentity.actual_digest)
    && nullableString(consoleIdentity.manifest_digest) && strings(consoleIdentity.reasons)
    && typeof schema.status === 'string' && strings(schema.reasons) && Array.isArray(schema.applied)
    && schema.applied.every((row: unknown) => object(row) && typeof row.version === 'number'
      && typeof row.name === 'string' && typeof row.checksum === 'string')
    && ['agent_cached', 'recorded', 'unavailable'].includes(String(providers.source))
    && nullableString(providers.as_of) && strings(providers.reasons)
    && Array.isArray(providers.capabilities) && providers.capabilities.every((p: unknown) => object(p)
      && typeof p.capability === 'string' && typeof p.status === 'string' && nullableString(p.reason)
      && typeof p.discovered_at === 'string');
}

function BuildRows({ identity }: { identity: InstallationBuild | null }): JSX.Element {
  return <KeyValueList>
    <KeyValue label="Identity">{identity?.status ?? 'unknown'}</KeyValue>
    <KeyValue label="Build"><span className="mono">{identity?.build_id ?? 'unknown'}</span></KeyValue>
    <KeyValue label="Version">{identity?.version ?? 'unknown'}</KeyValue>
    <KeyValue label="Source digest"><span className="mono">{identity?.source_digest ?? 'unknown'}</span></KeyValue>
    <KeyValue label={identity?.source_dirty ? 'Base commit' : 'Source commit'}>
      <span className="mono">{identity?.source_commit ?? 'unknown'}</span>
      {identity?.source_dirty ? ' · modified candidate; digest identifies contents' : null}
    </KeyValue>
    <KeyValue label="Exact clean tree"><span className="mono">{identity?.source_tree ?? 'unknown'}</span></KeyValue>
    {identity?.reasons.length ? <KeyValue label="Reasons">{identity.reasons.join(' · ')}</KeyValue> : null}
  </KeyValueList>;
}

export function InstallationPlate(): JSX.Element {
  const [installation, setInstallation] = useState<Installation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const pending = useRef<AbortController | null>(null);

  const read = useCallback(async () => {
    pending.current?.abort();
    const request = new AbortController();
    pending.current = request;
    setLoading(true);
    setError(null);
    try {
      const data = await endpoints.installation({ signal: request.signal });
      if (request.signal.aborted) return;
      if (!installationShape(data)) throw new Error('Installation response is unavailable or incompatible with this console.');
      setInstallation(data);
    } catch (err) {
      if (request.signal.aborted) return;
      if (err instanceof ApiError) {
        setError(`${err.code ?? err.kind}: ${err.message}`);
      } else {
        setError(String(err));
      }
    } finally {
      if (!request.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void read();
    return () => pending.current?.abort();
  }, [read]);

  const backend = installation?.backend;
  const agent = installation?.agent;
  const consoleIdentity = installation?.console;
  const maintenance = installation?.maintenance;

  return (
    <Plate className={styles.installation} style={{ marginTop: 14 }} aria-label="Installation diagnostics">
      <PlateHead
        title="Installation"
        level={3}
        meta="read-only build and maintenance evidence"
        asOf={installation?.generated_at}
      >
        <button type="button" className={styles.reRead} onClick={() => { void read(); }} disabled={loading}>
          {loading ? 'Reading…' : 'Re-read'}
        </button>
      </PlateHead>
      <PlateBody>
        <div aria-live="polite">
          {error ? <p role="alert" className={styles.error}>{error}</p> : null}
          {loading ? <p className={styles.deferred}>Reading installation diagnostics…</p> : null}
          {installation && (error || loading) ? <p className={styles.deferred}>Previous diagnostic read — not refreshed. Its original timestamps remain below.</p> : null}
        </div>
        {installation ? (
          <>
            {installation.gaps.length ? <p className={styles.deferred}>Diagnostic gaps: {installation.gaps.join(' · ')}</p> : null}
            <div className={styles.componentGrid}>
            <section aria-label="Backend build">
            <h4 className={styles.section}>Backend</h4>
            <BuildRows identity={backend ?? null} />
            </section>
            <section aria-label="Agent build">
            <h4 className={styles.section}>Agent</h4>
            <KeyValueList>
              <KeyValue label="Reachable">{agent?.reachable ? 'yes' : 'no'}</KeyValue>
              <KeyValue label="Evidence">{agent?.source ?? 'unknown'} · {agent?.as_of ?? 'unknown'}</KeyValue>
              <KeyValue label="Instance"><span className="mono">{agent?.agent_instance_id ?? 'unknown'}</span></KeyValue>
              {agent?.error ? <KeyValue label="Refusal">{agent.error.code} — {agent.error.message}</KeyValue> : null}
              <KeyValue label="Agent fence">{agent?.maintenance?.status ?? 'unknown'}</KeyValue>
            </KeyValueList>
            <BuildRows identity={agent?.build ?? null} />
            </section>
            </div>

            <h4 className={styles.section}>Loaded console</h4>
            <KeyValueList>
              <KeyValue label="Identity">{consoleIdentity?.status ?? 'unknown'}</KeyValue>
              <KeyValue label="Actual digest"><span className="mono">{consoleIdentity?.actual_digest ?? 'unknown'}</span></KeyValue>
              <KeyValue label="Manifest digest"><span className="mono">{consoleIdentity?.manifest_digest ?? 'unknown'}</span></KeyValue>
              {consoleIdentity?.reasons.length ? <KeyValue label="Reasons">{consoleIdentity.reasons.join(' · ')}</KeyValue> : null}
            </KeyValueList>

            <h4 className={styles.section}>Schema</h4>
            <KeyValueList>
              <KeyValue label="Match">{installation.schema_identity.status}</KeyValue>
              <KeyValue label="Applied migrations">{installation.schema_identity.applied.length || 'unknown; no rows reported'}</KeyValue>
              {installation.schema_identity.reasons.length ? <KeyValue label="Reasons">{installation.schema_identity.reasons.join(' · ')}</KeyValue> : null}
            </KeyValueList>
            {installation.schema_identity.applied.length ? <details className={styles.schemaDetails}>
              <summary>Applied schema checksums</summary>
              <KeyValueList>{installation.schema_identity.applied.map((row) =>
                <KeyValue key={row.version} label={`${row.version} · ${row.name}`}><span className="mono">{row.checksum}</span></KeyValue>)}</KeyValueList>
            </details> : null}

            <h4 className={styles.section}>Maintenance</h4>
            <KeyValueList>
              <KeyValue label="Fence">{maintenance?.status ?? 'unknown'}</KeyValue>
              <KeyValue label="Window"><span className="mono">{maintenance?.window?.window_id ?? 'none reported'}</span></KeyValue>
              <KeyValue label="As of">{maintenance?.as_of ?? 'unknown'}</KeyValue>
              <KeyValue label="Refusal">{maintenance?.refusal ? `${maintenance.refusal.code} — ${maintenance.refusal.message}` : 'none reported'}</KeyValue>
            </KeyValueList>

            <h4 className={styles.section}>Provider capability evidence</h4>
            <KeyValueList>
              <KeyValue label="Source">{installation.providers.source} · {installation.providers.as_of ?? 'timestamp unknown'}</KeyValue>
              {installation.providers.reasons.length ? <KeyValue label="Reasons">{installation.providers.reasons.join(' · ')}</KeyValue> : null}
              {installation.providers.capabilities.length ? installation.providers.capabilities.map((p) => (
                <KeyValue key={p.capability} label={p.capability}>
                  {p.status}{p.reason ? ` — ${p.reason}` : null}
                  <span className={styles.evidenceStamp}>discovered {p.discovered_at}</span>
                </KeyValue>
              )) : <KeyValue label="Capabilities">unknown; no capability evidence reported</KeyValue>}
            </KeyValueList>
          </>
        ) : !loading && !error ? <p className={styles.deferred}>Installation identity is unknown.</p> : null}
        <p className="note" style={{ marginTop: 11, maxWidth: '80ch' }}>
          This plate is read-only. It shows the build identity the backend reports for itself,
          the agent it can reach, the compiled console it loaded, and whether a maintenance
          window is open. Capability availability is not live provider health or permission to
          manage it. Recorded evidence may be stale. No setting here performs an update, restart or restore.
        </p>
      </PlateBody>
    </Plate>
  );
}
