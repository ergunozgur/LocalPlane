/**
 * Deferred structure, and what it must never become.
 *
 * The design direction's structures are preserved where the backend cannot fill them,
 * which is only safe if a deferred slot is unmistakably not a control. These tests hold
 * that line: no clickable affordance, no invented value, and wording that describes
 * *this build* rather than the host.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { App } from '@/App';
import { ViewerProvider } from '@/identity/viewer';
import { PreferencesProvider } from '@/preferences/preferences';
import { stubBackend } from '@/test/backend';

afterEach(() => vi.unstubAllGlobals());

function renderAt(path: string): void {
  render(
    <ViewerProvider>
      <PreferencesProvider>
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>
      </PreferencesProvider>
    </ViewerProvider>,
  );
}

describe('the Engine plate', () => {
  it('shows the engine facts that are real', async () => {
    stubBackend();
    renderAt('/workloads/runtime');
    await screen.findByText('Engine');
    expect(screen.getAllByText('docker').length).toBeGreaterThan(0);
    expect(screen.getByText('29.1.3')).toBeInTheDocument();
  });

  it('names unavailable engine facts as unobserved rather than inventing them', async () => {
    stubBackend();
    renderAt('/workloads/runtime');
    await screen.findByText('Engine');

    for (const label of ['Storage driver', 'Cgroup', 'Volumes', 'Live restore']) {
      const row = screen.getByText(label).closest('div') as HTMLElement;
      expect(within(row).getByText(/not observed by this build/i)).toBeInTheDocument();
    }
  });

  it('offers no control anywhere in the deferred rows', async () => {
    stubBackend();
    renderAt('/workloads/runtime');
    await screen.findByText('Engine');
    const row = screen.getByText('Storage driver').closest('div') as HTMLElement;
    expect(within(row).queryByRole('button')).not.toBeInTheDocument();
    expect(within(row).queryByRole('link')).not.toBeInTheDocument();
  });
});

describe('the Runtimes plate', () => {
  it('never reports an undetectable runtime as absent', async () => {
    stubBackend();
    renderAt('/workloads/runtime');
    await screen.findByText('podman');

    for (const name of ['podman', 'kubelet', 'systemd-supervised']) {
      const row = screen.getByText(name).closest('tr') as HTMLElement;
      expect(within(row).getByText(/not detected by this build/i)).toBeInTheDocument();
      expect(within(row).queryByText(/^no$/)).not.toBeInTheDocument();
    }
  });
});

describe('the account menu', () => {
  it('marks Customize dashboard as unavailable without offering a control', async () => {
    stubBackend();
    renderAt('/settings');
    fireEvent.click(screen.getAllByRole('button', { name: 'Account and appearance' })[0]!);

    const entry = screen.getByText('Customize dashboard').closest('div') as HTMLElement;
    expect(within(entry).getByText('not in this build')).toBeInTheDocument();
    expect(entry.closest('a')).toBeNull();
  });

  it('does not offer a functional sign out', async () => {
    stubBackend();
    renderAt('/settings');
    fireEvent.click(screen.getAllByRole('button', { name: 'Account and appearance' })[0]!);

    const entry = screen.getByText('Sign out').closest('div') as HTMLElement;
    expect(entry.closest('a')).toBeNull();
    expect(within(entry).queryByRole('button')).not.toBeInTheDocument();
    expect(screen.getByText(/no authentication, so there is nothing to sign out of/i)).toBeInTheDocument();
  });

  it('links the change ledger to a real destination with a real count', async () => {
    stubBackend();
    renderAt('/settings');
    fireEvent.click(screen.getAllByRole('button', { name: 'Account and appearance' })[0]!);

    const ledger = screen.getByText('Change ledger').closest('a');
    expect(ledger).toHaveAttribute('href', '/operations');
    // The count arrives with the changes read; until then the entry says what it links to
    // rather than guessing a number.
    expect(await screen.findByText(/1 recorded change/)).toBeInTheDocument();
  });
});

describe('the host picker', () => {
  it('offers the real host and marks fleet as absent from this build', async () => {
    stubBackend();
    renderAt('/settings');
    const trigger = await screen.findByRole('button', { name: 'Host' });
    fireEvent.click(trigger);

    expect(screen.getByText('local')).toBeInTheDocument();
    const fleet = screen.getByText('More than one host').closest('div') as HTMLElement;
    expect(within(fleet).getByText('not in this build')).toBeInTheDocument();
    // No "Add a host" control is offered, because nothing could act on it.
    expect(screen.queryByRole('button', { name: /add a host/i })).not.toBeInTheDocument();
  });
});

describe('console settings', () => {
  it('states the policy in force without offering controls for it', async () => {
    stubBackend();
    renderAt('/settings');
    await screen.findByText('Console settings');

    for (const label of ['Observation interval', 'Connection guard', 'Confirmations']) {
      const row = screen.getByText(label).closest('div') as HTMLElement;
      expect(within(row).queryByRole('button')).not.toBeInTheDocument();
      expect(within(row).queryByRole('combobox')).not.toBeInTheDocument();
      expect(within(row).queryByRole('textbox')).not.toBeInTheDocument();
    }
    expect(screen.getByText(/No settings endpoint exists in this build/i)).toBeInTheDocument();
  });
});

describe('breadcrumbs', () => {
  it('names the host, the section and the object', async () => {
    stubBackend();
    renderAt('/workloads/obj_ct');

    const crumbs = await screen.findByRole('navigation', { name: 'Breadcrumb' });
    expect(within(crumbs).getByText('workloads')).toBeInTheDocument();
    // The compose project is a real parent; the service is the part this container plays.
    expect(within(crumbs).getByText('monitoring')).toBeInTheDocument();
    expect(within(crumbs).getByText('grafana')).toBeInTheDocument();
  });

  it('does not invent a parent for a standalone container', async () => {
    stubBackend({
      '/api/v1/docker/containers/obj_ct': {
        ...(stubBackendContainer()),
        labels: {},
      },
    });
    renderAt('/workloads/obj_ct');
    const crumbs = await screen.findByRole('navigation', { name: 'Breadcrumb' });
    expect(within(crumbs).queryByText('monitoring')).not.toBeInTheDocument();
  });
});

/** The shared fixture's container, so the standalone case only changes its labels. */
function stubBackendContainer(): Record<string, unknown> {
  return {
    object_id: 'obj_ct', kind: 'docker.container', name: 'grafana',
    container_id: 'abc123def4567890', short_id: 'abc123def456',
    identity: { basis: 'container_id', value: 'abc123def4567890', confidence: 'high' },
    management: { state: 'observed', reason: 'observe_only' },
    ownership: { state: 'attributed', reason: 'externally_created', created_by: null,
      configured_by: null, evidence_gaps: [],
      adoption: { eligible: false, reason: 'externally_created', blocked_by: null, evidence_gaps: [] } },
    health: { state: 'inactive', reason: 'exited' },
    observation: null, observed_in_latest_sweep: true,
    image: { reference: 'grafana/grafana-oss:latest', image_id: 'sha256:deadbeef' },
    created_at: '2026-08-20T10:00:00Z',
    runtime: { state: 'exited', running: false, paused: false, restarting: false, exit_code: 0,
      error: null, oom_killed: false, pid: null, started_at: null, finished_at: null, restart_count: 0 },
    container_health: { checked: false, status: null, failing_streak: null },
    restart_policy: { name: 'unless-stopped', maximum_retry_count: 0 },
    network_mode: 'bridge', networks: [], ports: [], mounts: [],
    labels_dropped: 0, log_driver: 'json-file', platform: 'linux',
    first_seen_at: '2026-08-27T21:46:55Z', last_seen_at: '2026-08-27T21:47:11Z',
  };
}
