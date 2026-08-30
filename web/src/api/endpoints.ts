/**
 * One typed function per backend endpoint.
 *
 * This is the adapter seam. Production binds it to the real backend; a future website demo
 * can substitute an object of the same shape backed by fixtures, without a component
 * knowing. Nothing here interprets a response — that is the domain layer's job — and nothing
 * here invents a value when a request fails.
 *
 * Only read endpoints are bound. The host-writing surfaces (`runs/{id}/apply`,
 * `changes/{id}/recovery/retry`) and the record-writing surfaces (`POST /runs`, `confirm`,
 * `adopt`, `release`, intent revision) are deliberately absent from this build: see the
 * implementation plan's deferral list. Adding one is a function here plus a control, not a
 * restructuring.
 */
import { request, type RequestOptions } from './client';
import type * as T from './types';

type Opts = Pick<RequestOptions, 'signal'>;

export const endpoints = {
  /* ------------------------------------------------------------------ status & identity */
  status: (o?: Opts) => request<T.BackendStatus>('/status', o),
  host: (o?: Opts) => request<T.Host>('/host', o),
  agent: (o?: Opts) => request<T.AgentStatus>('/agent', o),
  capabilities: (o?: Opts) => request<T.Capabilities>('/agent/capabilities', o),

  /* -------------------------------------------------------------------------- network */
  interfaces: (o?: Opts) => request<T.NetworkInterfaceList>('/network/interfaces', o),
  interfaceDetail: (id: string, o?: Opts) =>
    request<T.NetworkInterface>(`/network/interfaces/${encodeURIComponent(id)}`, o),
  interfaceProtection: (id: string, o?: Opts) =>
    request<T.ObjectProtection>(`/network/interfaces/${encodeURIComponent(id)}/protection`, o),
  interfaceProvenance: (id: string, o?: Opts) =>
    request<T.Provenance>(`/network/interfaces/${encodeURIComponent(id)}/provenance`, o),
  interfaceEvidence: (id: string, o?: Opts) =>
    request<T.Evidence>(`/network/interfaces/${encodeURIComponent(id)}/evidence`, o),
  interfaceIntent: (id: string, o?: Opts) =>
    request<T.Intent>(`/network/interfaces/${encodeURIComponent(id)}/intent`, o),
  interfaceReconciliation: (id: string, o?: Opts) =>
    request<T.ReconciliationResult>(
      `/network/interfaces/${encodeURIComponent(id)}/reconciliation`,
      o,
    ),
  interfaceIntentHistory: (id: string, o?: Opts) =>
    request<T.IntentHistory>(`/network/interfaces/${encodeURIComponent(id)}/intent/history`, o),

  /* --------------------------------------------------------------------------- systemd */
  systemdUnits: (o?: Opts) => request<T.SystemdUnitList>('/systemd/units', o),
  systemdUnit: (id: string, o?: Opts) =>
    request<T.SystemdUnit>(`/systemd/units/${encodeURIComponent(id)}`, o),

  /* ---------------------------------------------------------------------------- docker */
  containers: (o?: Opts) => request<T.DockerContainerList>('/docker/containers', o),
  container: (id: string, o?: Opts) =>
    request<T.DockerContainer>(`/docker/containers/${encodeURIComponent(id)}`, o),
  /**
   * Container resource usage. `POST` because that is what the backend declares, though it
   * reads: it samples the daemon and writes nothing. Fetched on demand from a detail page
   * only — never once per row of a list.
   */
  containerStats: (id: string, o?: Opts) =>
    request<T.ContainerStats>(`/docker/containers/${encodeURIComponent(id)}/stats`, {
      ...o,
      method: 'POST',
    }),
  /** Container logs, bounded by the backend's own line and byte limits. */
  containerLogs: (id: string, tail: number, o?: Opts) =>
    request<T.ContainerLogs>(`/docker/containers/${encodeURIComponent(id)}/logs`, {
      ...o,
      method: 'POST',
      query: { tail },
    }),

  /* ----------------------------------------------------------- management path & sweeps */
  managementPath: (o?: Opts) => request<T.ManagementPath>('/management-path', o),
  sweeps: (limit?: number, o?: Opts) =>
    request<T.SweepList>('/observations/sweeps', { ...o, ...(limit ? { query: { limit } } : {}) }),

  /* ---------------------------------------------------------------- runs, changes, findings */
  runs: (params?: { state?: string; object_id?: string; limit?: number }, o?: Opts) =>
    request<T.RunList>('/runs', { ...o, ...(params ? { query: params } : {}) }),
  run: (id: string, o?: Opts) => request<T.Run>(`/runs/${encodeURIComponent(id)}`, o),
  runPreview: (id: string, o?: Opts) =>
    request<T.RunPreview>(`/runs/${encodeURIComponent(id)}/preview`, o),

  changes: (params?: { object_id?: string; result?: string; limit?: number }, o?: Opts) =>
    request<T.ChangeList>('/changes', { ...o, ...(params ? { query: params } : {}) }),
  change: (id: string, o?: Opts) => request<T.Change>(`/changes/${encodeURIComponent(id)}`, o),

  findings: (params?: { status?: string; object_id?: string; limit?: number }, o?: Opts) =>
    request<T.FindingList>('/findings', { ...o, ...(params ? { query: params } : {}) }),
  finding: (id: string, o?: Opts) => request<T.Finding>(`/findings/${encodeURIComponent(id)}`, o),
} as const;

export type Endpoints = typeof endpoints;
