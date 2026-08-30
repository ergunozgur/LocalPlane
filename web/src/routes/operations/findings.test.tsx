/**
 * The Findings surface.
 *
 * A finding is a claim the backend publishes; this UI must present it and its evidence
 * without re-deriving either. In particular a comparison the backend could not make must not
 * become agreement.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
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

describe('findings list', () => {
  it('lists findings the backend published, with their type and status', async () => {
    stubBackend();
    renderAt('/operations/findings');

    expect(await screen.findByText(/mtu differs from its intent/i)).toBeInTheDocument();
    expect(screen.getAllByText('drift').length).toBeGreaterThan(0);
    expect(screen.getAllByText('open').length).toBeGreaterThan(0);
  });

  it('distinguishes an open finding from a resolution', async () => {
    stubBackend();
    renderAt('/operations/findings');
    await screen.findByText(/mtu differs/i);
    // No resolution exists, so the cell is an explicit unknown rather than blank.
    expect(screen.getByRole('img', { name: /still open/i })).toBeInTheDocument();
  });

  it('explains an empty list as a statement about claims, not about the host', async () => {
    stubBackend({
      '/api/v1/findings': { host_id: 'host_1', status: 'open', count: 0, findings: [] },
    });
    renderAt('/operations/findings');
    expect(await screen.findByText(/No open findings/i)).toBeInTheDocument();
    expect(screen.getByText(/not a guarantee about the host/i)).toBeInTheDocument();
  });
});

describe('finding detail', () => {
  it('shows the intended and observed values with the backend’s comparison', async () => {
    stubBackend();
    renderAt('/operations/findings/find_1');

    await screen.findByText('The claim');
    expect(screen.getByText('1400')).toBeInTheDocument();
    expect(screen.getByText('1500')).toBeInTheDocument();
    expect(screen.getAllByText('differs').length).toBeGreaterThan(0);
  });

  it('renders an unreadable observed value as not comparable, never as agreement', async () => {
    stubBackend({
      '/api/v1/findings/find_1': {
        finding_id: 'find_1', finding_key: 'k', host_id: 'host_1',
        object_id: 'obj_if', object_name: 'eth0', finding_type: 'drift', subject: 'mtu',
        status: 'open', summary: 'mtu could not be compared',
        evidence: {
          intent_id: 'int_1', field: 'mtu',
          intended: { type: 'integer', value: 1400 },
          observed: null,
          comparison: 'unknown',
          reason: 'value_unreadable', observation: null, sweep: null,
        },
        first_seen_at: '2026-08-20T10:00:00Z', last_seen_at: '2026-08-28T08:00:00Z',
        updated_at: '2026-08-28T08:00:00Z', resolved_at: null, resolution: null,
      },
    });
    renderAt('/operations/findings/find_1');

    await screen.findByText('The claim');
    expect(screen.getByText('not comparable')).toBeInTheDocument();
    expect(screen.queryByText('matches')).not.toBeInTheDocument();
    // The observed side is an explicit unknown, not a blank and not the intended value.
    const observed = screen.getByText('Observed').closest('div') as HTMLElement;
    expect(within(observed).getByRole('img', { name: /could not read/i })).toBeInTheDocument();
  });

  it('says a resolution ends the claim, not that the host was put right', async () => {
    stubBackend({
      '/api/v1/findings/find_1': {
        finding_id: 'find_1', finding_key: 'k', host_id: 'host_1',
        object_id: 'obj_if', object_name: 'eth0', finding_type: 'drift', subject: 'mtu',
        status: 'resolved', summary: 'mtu drift ended',
        evidence: {
          intent_id: 'int_1', field: 'mtu',
          intended: { type: 'integer', value: 1500 },
          observed: { type: 'integer', value: 1500 },
          comparison: 'differs', reason: 'r', observation: null, sweep: null,
        },
        first_seen_at: '2026-08-20T10:00:00Z', last_seen_at: '2026-08-28T08:00:00Z',
        updated_at: '2026-08-28T08:00:00Z', resolved_at: '2026-08-28T09:00:00Z',
        resolution: 'intent_revised',
      },
    });
    renderAt('/operations/findings/find_1');

    expect(await screen.findByText(/not that the host was put right/i)).toBeInTheDocument();
  });
});

describe('navigation', () => {
  it('reaches findings from the Operations menu with a live count', async () => {
    stubBackend();
    renderAt('/operations');
    const nav = screen.getByRole('navigation', { name: 'Primary' });

    // Operations has sub-surfaces, so its control is a menu trigger rather than a link.
    const trigger = await within(nav).findByRole('button', { name: /Operations/ });
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
    fireEvent.click(trigger);

    const findings = await screen.findByRole('menuitem', { name: /Findings/ });
    expect(findings).toHaveAttribute('href', '/operations/findings');
  });
});
