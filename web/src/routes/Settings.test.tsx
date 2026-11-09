import { afterEach, expect, it, vi } from 'vitest';
import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Installation, InstallationBuild } from '@/api/types';
import { InstallationPlate } from './Settings';

afterEach(() => vi.unstubAllGlobals());

const time = '2026-11-09T08:00:00Z';
const build = (name: string): InstallationBuild => ({
  status: 'packaged', build_id: `sha256:${name}`, version: '0.1.0', source_commit: 'base-commit',
  source_tree: null, source_dirty: true, source_digest: `sha256:source-${name}`, reasons: [],
  process: name, manifest_sha256: null, console_digest: null, file_count: null,
  migration_count: null, dependencies: [], platform: null, loaded_at: time,
});
function fixture(): Installation {
  return {
    generated_at: time, backend: build('backend'),
    agent: { reachable: true, source: 'live', as_of: time, agent_instance_id: 'process-2',
      build: build('agent'), error: null, maintenance: null, agent_version: '0.1.0', pid: null, started_at: null },
    console: { status: 'matched', actual_digest: 'sha256:loaded-console', manifest_digest: 'sha256:loaded-console',
      matches: true, asset_count: 1, reasons: [] },
    schema_identity: { status: 'matched', applied: [{ version: 1, name: 'init', checksum: 'schema-checksum' }],
      manifest_migrations: {}, matches: true, reasons: [] },
    maintenance: { status: 'active', active: true, window: { window_id: 'window-one', state: 'active',
      generation: 1, opened_at: time, closed_at: null, accepted_at: null, release_build_id: null,
      snapshot_id: null, reason: null, outcome: null }, refusal: null, root_id: null, as_of: time },
    providers: { source: 'recorded', as_of: time, capabilities: [], reasons: ['cached_not_live_health'] },
    gaps: [],
  };
}
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

it('keeps backend, agent, loaded console, schema and fence identities separate', async () => {
  const fetch = vi.fn().mockResolvedValue(response(fixture()));
  vi.stubGlobal('fetch', fetch);
  render(<InstallationPlate />);
  expect(await screen.findByText('sha256:backend')).toBeInTheDocument();
  expect(within(screen.getByRole('region', { name: 'Agent build' })).getByText('sha256:agent')).toBeInTheDocument();
  expect(screen.getAllByText('sha256:loaded-console')).toHaveLength(2);
  expect(screen.getByText('window-one')).toBeInTheDocument();
  expect(screen.getByText('cached_not_live_health')).toBeInTheDocument();
  expect(screen.getByText(/unknown; no capability evidence/)).toBeInTheDocument();
  expect(screen.queryByText(/all providers ready/i)).not.toBeInTheDocument();
  await userEvent.click(screen.getByText('Applied schema checksums'));
  expect(screen.getByText('schema-checksum')).toBeVisible();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0]?.[0]).toBe('/api/v1/installation');
  expect(fetch.mock.calls[0]?.[1]).toMatchObject({ method: 'GET', credentials: 'same-origin' });
  expect(screen.getAllByRole('button').map((b) => b.textContent)).toEqual(['Re-read']);
});

it('labels unavailable agent evidence and unknown fence, never false all-clear', async () => {
  const data = fixture();
  data.agent = { ...data.agent, reachable: false, source: 'unavailable', build: null,
    error: { code: 'agent_unreachable', message: 'No live response', detail: {} } };
  data.maintenance = { ...data.maintenance, status: 'unknown', active: false, window: null,
    refusal: { code: 'witness_malformed', message: 'Cannot prove fence state', detail: {} } };
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(data)));
  render(<InstallationPlate />);
  expect(await screen.findByText('agent_unreachable — No live response')).toBeInTheDocument();
  const agent = screen.getByRole('region', { name: 'Agent build' });
  expect(within(agent).queryByText('sha256:backend')).not.toBeInTheDocument();
  expect(screen.getByText('witness_malformed — Cannot prove fence state')).toBeInTheDocument();
  expect(screen.queryByText('healthy')).not.toBeInTheDocument();
});

it('shows loading and retains timestamped previous evidence after manual read failure', async () => {
  let resolve: (value: Response) => void = () => {};
  const fetch = vi.fn().mockImplementationOnce(() => new Promise<Response>((r) => { resolve = r; }))
    .mockResolvedValueOnce(response({ error: { code: 'diagnostic_unavailable', message: 'Try a later read', detail: {} } }, 503));
  vi.stubGlobal('fetch', fetch);
  render(<InstallationPlate />);
  expect(screen.getByRole('button', { name: 'Reading…' })).toBeDisabled();
  expect(screen.getByText('Reading installation diagnostics…')).toBeInTheDocument();
  await act(async () => resolve(response(fixture())));
  const user = userEvent.setup();
  const reread = screen.getByRole('button', { name: 'Re-read' });
  reread.focus();
  await user.keyboard('{Enter}');
  expect(await screen.findByRole('alert')).toHaveTextContent('diagnostic_unavailable');
  expect(screen.getByText(/Previous diagnostic read — not refreshed/)).toBeInTheDocument();
  expect(screen.getByText('sha256:backend')).toBeInTheDocument();
  expect(fetch).toHaveBeenCalledTimes(2);
});

it.each([null, {}, { backend: { build_id: 'fake' } }])('refuses malformed/empty diagnostic response %s', async (body) => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(body)));
  render(<InstallationPlate />);
  expect(await screen.findByRole('alert')).toHaveTextContent('unavailable or incompatible');
  expect(screen.queryByText('fake')).not.toBeInTheDocument();
});

it('aborts a pending read on unmount without attempting a mutation', () => {
  const fetch = vi.fn().mockImplementation(() => new Promise<Response>(() => {}));
  vi.stubGlobal('fetch', fetch);
  const rendered = render(<InstallationPlate />);
  const signal = fetch.mock.calls[0]?.[1]?.signal as AbortSignal;
  rendered.unmount();
  expect(signal.aborted).toBe(true);
  expect(fetch).toHaveBeenCalledTimes(1);
});

it.each(['agent_cached', 'recorded', 'unavailable'] as const)('keeps %s provider source with unknown timestamp explicit', async (source) => {
  const data = fixture();
  data.providers = { source, as_of: null, capabilities: [], reasons: ['not_live_health'] };
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(data)));
  render(<InstallationPlate />);
  expect(await screen.findByText(`${source} · timestamp unknown`)).toBeInTheDocument();
  expect(screen.getByText('not_live_health')).toBeInTheDocument();
});

it('refuses a fictitious live provider-health source', async () => {
  const data = fixture();
  const incompatible = { ...data, providers: { ...data.providers, source: 'live' } };
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(incompatible)));
  render(<InstallationPlate />);
  expect(await screen.findByRole('alert')).toHaveTextContent('unavailable or incompatible');
});

it('surfaces the final diagnostic contract gaps without inventing readiness', async () => {
  const data = fixture();
  data.gaps = ['agent_identity_unavailable', 'provider_capabilities_cached'];
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(data)));
  render(<InstallationPlate />);
  expect(await screen.findByText('Diagnostic gaps: agent_identity_unavailable · provider_capabilities_cached')).toBeInTheDocument();
});
