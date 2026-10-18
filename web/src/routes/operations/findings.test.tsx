/**
 * The Findings surface.
 *
 * A finding is a claim the backend publishes; this UI must present it and its evidence
 * without re-deriving either. In particular a comparison the backend could not make must not
 * become agreement.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useNavigate } from 'react-router-dom';
import { App } from '@/App';
import type { Finding, NetworkInterfaceList, DockerContainerList } from '@/api/types';
import { ViewerProvider } from '@/identity/viewer';
import { PreferencesProvider } from '@/preferences/preferences';
import { AuthenticationProvider } from '@/auth/AuthProvider';
import { BACKEND, stubBackend } from '@/test/backend';

afterEach(() => vi.unstubAllGlobals());

const INVENTORY_PATHS = [
  '/api/v1/network/interfaces',
  '/api/v1/docker/containers',
  '/api/v1/systemd/units',
] as const;

function trackInventoryResponses(): Map<string, Promise<Response>> {
  const original = globalThis.fetch;
  const responses = new Map<string, Promise<Response>>();
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : input instanceof URL ? input.href : input;
    const path = new URL(url, 'http://local.test').pathname;
    const response = original(input, init);
    if ((INVENTORY_PATHS as readonly string[]).includes(path)) responses.set(path, response);
    return response;
  }));
  return responses;
}

async function settleInventoryResponses(responses: Map<string, Promise<Response>>): Promise<void> {
  await waitFor(() => {
    for (const path of INVENTORY_PATHS) expect(responses.has(path), `${path} was requested`).toBe(true);
  });
  await act(async () => {
    await Promise.all(INVENTORY_PATHS.map((path) => responses.get(path)!));
  });
}

function renderAt(path: string, controls?: JSX.Element): void {
  render(
    <ViewerProvider>
      <PreferencesProvider>
        <AuthenticationProvider>
          <MemoryRouter initialEntries={[path]}>
            <App />
            {controls}
          </MemoryRouter>
        </AuthenticationProvider>
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
  const baseFinding = BACKEND['/api/v1/findings/find_1'] as Finding;
  it.each([
    ['obj_if', '/network/obj_if'],
    ['obj_ct', '/workloads/obj_ct'],
    ['obj_unit', '/system/obj_unit'],
  ])('links an object only to its matching typed inventory record (%s)', async (objectId, href) => {
    stubBackend({ '/api/v1/findings/find_1': { ...baseFinding, object_id: objectId } });
    const reads = trackInventoryResponses();
    renderAt('/operations/findings/find_1');
    await settleInventoryResponses(reads);
    expect(await screen.findByRole('link', { name: baseFinding.object_name })).toHaveAttribute('href', href);
  });

  it('does not link a same-named object when its stable id is not present', async () => {
    stubBackend({ '/api/v1/findings/find_1': { ...baseFinding, object_id: 'different-id' } });
    const reads = trackInventoryResponses();
    renderAt('/operations/findings/find_1');
    await screen.findByText('The claim');
    await settleInventoryResponses(reads);
    expect(screen.queryByRole('link', { name: baseFinding.object_name })).not.toBeInTheDocument();
  });

  it('encodes special characters in an evidenced stable ID', async () => {
    const list = BACKEND['/api/v1/network/interfaces'] as NetworkInterfaceList;
    const id = 'if/a?b';
    stubBackend({
      '/api/v1/findings/find_1': { ...baseFinding, object_id: id },
      '/api/v1/network/interfaces': { ...list, interfaces: [{ ...list.interfaces[0]!, object_id: id }] },
    });
    const reads = trackInventoryResponses();
    renderAt('/operations/findings/find_1');
    await settleInventoryResponses(reads);
    expect(await screen.findByRole('link', { name: baseFinding.object_name })).toHaveAttribute('href', '/network/if%2Fa%3Fb');
  });

  it('does not join an inventory record from another host', async () => {
    const list = BACKEND['/api/v1/network/interfaces'] as NetworkInterfaceList;
    stubBackend({ '/api/v1/network/interfaces': { ...list, host_id: 'host_other' } });
    const reads = trackInventoryResponses();
    renderAt('/operations/findings/find_1');
    await screen.findByText('The claim');
    await settleInventoryResponses(reads);
    expect(screen.queryByRole('link', { name: baseFinding.object_name })).not.toBeInTheDocument();
  });

  it('does not route an ID match whose typed domain kind is unsupported', async () => {
    const list = BACKEND['/api/v1/network/interfaces'] as NetworkInterfaceList;
    stubBackend({ '/api/v1/network/interfaces': { ...list, interfaces: [{ ...list.interfaces[0]!, kind: 'unexpected.kind' }] } });
    const reads = trackInventoryResponses();
    renderAt('/operations/findings/find_1');
    await screen.findByText('The claim');
    await settleInventoryResponses(reads);
    expect(screen.queryByRole('link', { name: baseFinding.object_name })).not.toBeInTheDocument();
  });

  it('leaves conflicting domain matches unlinked', async () => {
    const list = BACKEND['/api/v1/docker/containers'] as DockerContainerList;
    stubBackend({ '/api/v1/docker/containers': { ...list, containers: [{ ...list.containers[0]!, object_id: baseFinding.object_id }] } });
    const reads = trackInventoryResponses();
    renderAt('/operations/findings/find_1');
    await screen.findByText('The claim');
    await settleInventoryResponses(reads);
    expect(screen.queryByRole('link', { name: baseFinding.object_name })).not.toBeInTheDocument();
  });

  it('activates an encoded object link to the existing detail route', async () => {
    const list = BACKEND['/api/v1/network/interfaces'] as NetworkInterfaceList;
    const detail = BACKEND['/api/v1/network/interfaces/obj_if'] as NetworkInterfaceList['interfaces'][number];
    const id = 'if/a?b';
    stubBackend({
      '/api/v1/findings/find_1': { ...baseFinding, object_id: id },
      '/api/v1/network/interfaces': { ...list, interfaces: [{ ...list.interfaces[0]!, object_id: id }] },
      [`/api/v1/network/interfaces/${encodeURIComponent(id)}`]: { ...detail, object_id: id },
    });
    const reads = trackInventoryResponses();
    renderAt('/operations/findings/find_1');
    await settleInventoryResponses(reads);
    fireEvent.click(screen.getByRole('link', { name: baseFinding.object_name }));
    expect(await screen.findByRole('heading', { name: 'Link' })).toBeInTheDocument();
    expect(await screen.findByText(id)).toBeInTheDocument();
  });

  it.each([
    ['obj_ct', '/workloads/obj_ct', 'grafana'],
    ['obj_unit', '/system/obj_unit', 'ModemManager.service'],
  ])('activates the typed object link to its existing route (%s)', async (objectId, href, detailName) => {
    stubBackend({ '/api/v1/findings/find_1': { ...baseFinding, object_id: objectId } });
    const reads = trackInventoryResponses();
    renderAt('/operations/findings/find_1');
    await settleInventoryResponses(reads);
    const link = screen.getByRole('link', { name: baseFinding.object_name });
    expect(link).toHaveAttribute('href', href);
    fireEvent.click(link);
    expect((await screen.findAllByText(detailName)).length).toBeGreaterThan(0);
  });

  it('drops a previous target when navigating to a different finding', async () => {
    stubBackend({ '/api/v1/findings/find_2': {
      ...baseFinding,
      finding_id: 'find_2',
      object_id: 'unknown-object',
      status: 'resolved',
      summary: 'second finding is resolved',
      resolution: 'intent_released',
      resolved_at: '2026-08-28T09:00:00Z',
    } });
    function NavigateFinding(): JSX.Element {
      const navigate = useNavigate();
      return <button onClick={() => navigate('/operations/findings/find_2')}>Next finding</button>;
    }
    renderAt('/operations/findings/find_1', <NavigateFinding />);
    expect(await screen.findByRole('link', { name: baseFinding.object_name })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next finding' }));
    await screen.findByText('second finding is resolved');
    expect(screen.queryByRole('link', { name: baseFinding.object_name })).not.toBeInTheDocument();
  });

  it('keeps a proven match linkable when another inventory read fails', async () => {
    stubBackend();
    const reads = trackInventoryResponses();
    const trackedFetch = globalThis.fetch;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = input instanceof Request ? input.url : input instanceof URL ? input.href : input;
      const response = url.includes('/docker/containers')
        ? Promise.resolve(new Response('{}', { status: 500 }))
        : trackedFetch(input, init);
      const path = new URL(url, 'http://local.test').pathname;
      if (path === '/api/v1/docker/containers') reads.set(path, response);
      return response;
    }));
    renderAt('/operations/findings/find_1');
    await settleInventoryResponses(reads);
    expect(await screen.findByRole('link', { name: baseFinding.object_name })).toHaveAttribute('href', '/network/obj_if');
  });

  it('does not show a target link while its inventory read is pending', async () => {
    stubBackend();
    const original = globalThis.fetch;
    let release!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { release = resolve; });
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = input instanceof Request ? input.url : input instanceof URL ? input.href : input;
      return url.includes('/network/interfaces') ? pending : original(input, init);
    }));
    renderAt('/operations/findings/find_1');
    await screen.findByText('The claim');
    expect(screen.queryByRole('link', { name: baseFinding.object_name })).not.toBeInTheDocument();
    release(new Response(JSON.stringify(BACKEND['/api/v1/network/interfaces']), { status: 200 }));
    await screen.findByRole('link', { name: baseFinding.object_name });
    expect(screen.getByRole('link', { name: baseFinding.object_name })).toHaveAttribute('href', '/network/obj_if');
  });

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
    const nav = await screen.findByRole('navigation', { name: 'Primary' });

    // Operations has sub-surfaces, so its control is a menu trigger rather than a link.
    const trigger = await within(nav).findByRole('button', { name: /Operations/ });
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
    fireEvent.click(trigger);

    const findings = await screen.findByRole('menuitem', { name: /Findings/ });
    expect(findings).toHaveAttribute('href', '/operations/findings');
  });
});
