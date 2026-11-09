/**
 * SystemList URL-driven type filtering.
 *
 * The unit-type dropdown is now the visible face of the `type` query parameter. These tests
 * assert that direct URL entry, section links, dropdown changes, history navigation, and
 * local search all agree, without issuing another unit-inventory request.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from 'react-router-dom';
import { SystemList } from './SystemList';
import { BACKEND, stubBackend, urlOf } from '@/test/backend';
import { refreshAll } from '@/hooks/useResource';
import type { SystemdUnit, SystemdUnitList } from '@/api/types';

afterEach(() => vi.unstubAllGlobals());

function unitTemplate(): SystemdUnit {
  const list = BACKEND['/api/v1/systemd/units'] as SystemdUnitList;
  return list.units[0] as SystemdUnit;
}

function unitOf(
  unitType: string,
  objectId: string,
  canonicalId: string,
): SystemdUnit {
  return { ...unitTemplate(), object_id: objectId, canonical_id: canonicalId, unit_type: unitType };
}

const MIXED_LIST: SystemdUnitList = {
  ...(BACKEND['/api/v1/systemd/units'] as SystemdUnitList),
  count: 6,
  units: [
    unitOf('service', 'obj_svc', 'ModemManager.service'),
    unitOf('socket', 'obj_sock', 'cups.socket'),
    unitOf('timer', 'obj_timer', 'fstrim.timer'),
    unitOf('target', 'obj_target', 'network.target'),
    unitOf('mount', 'obj_mount', 'boot-efi.mount'),
    unitOf('path', 'obj_path', 'cups.path'),
  ],
};

function AddressProbe(): JSX.Element {
  const location = useLocation();
  return <span data-testid="address">{`${location.pathname}${location.search}`}</span>;
}

function renderAt(path: string, overrides: Record<string, unknown> = {}): ReturnType<typeof render> {
  stubBackend({ '/api/v1/systemd/units': MIXED_LIST, ...overrides });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="system" element={<SystemList />} />
      </Routes>
      <AddressProbe />
    </MemoryRouter>,
  );
}

it('separates reported failures by latest, retained and unknown membership', async () => {
  renderAt('/system', { '/api/v1/systemd/units': {
    ...MIXED_LIST,
    count: 3,
    units: [true, false, null].map((membership, index) => ({
      ...unitOf('service', `failure-${index}`, `failure-${index}.service`),
      active_state: 'failed',
      observed_in_latest_sweep: membership,
    })),
  } });
  const exactChip = (text: string) => screen.findByText((_content, element) =>
    element?.tagName === 'SPAN' && element.textContent === text);
  expect(await exactChip('1 latest-sighted reported failed')).toBeInTheDocument();
  expect(await exactChip('1 retained historical failure')).toBeInTheDocument();
  expect(await exactChip('1 reported failed, membership unknown')).toBeInTheDocument();
});

function typeSelect(): HTMLSelectElement {
  return screen.getByRole('combobox', { name: /unit type/i });
}

function expectRow(name: string): void {
  expect(screen.getByRole('link', { name })).toBeInTheDocument();
}

function expectNoRow(name: string): void {
  expect(screen.queryByRole('link', { name })).not.toBeInTheDocument();
}

function sectionLink(name: string): HTMLElement {
  // Tabs also render a count, so match the label at the start of the accessible name.
  return screen.getByRole('link', { name: new RegExp(`^${name}`) });
}

describe('URL-driven unit type', () => {
  it('shows only services and sets the dropdown at /system?type=service', async () => {
    renderAt('/system?type=service');

    await waitFor(() => expectRow('ModemManager.service'));
    expectNoRow('cups.socket');
    expect(typeSelect()).toHaveValue('service');
  });

  it('shows all loaded types at /system', async () => {
    renderAt('/system');

    await waitFor(() => expectRow('ModemManager.service'));
    expectRow('cups.socket');
    expectRow('fstrim.timer');
    expect(typeSelect()).toHaveValue('');
  });

  it.each([
    ['service', 'ModemManager.service'],
    ['socket', 'cups.socket'],
    ['timer', 'fstrim.timer'],
    ['target', 'network.target'],
    ['mount', 'boot-efi.mount'],
    ['path', 'cups.path'],
  ])('recognizes ?type=%s', async (type, canonicalId) => {
    renderAt(`/system?type=${type}`);

    await waitFor(() => expectRow(canonicalId));
    for (const other of MIXED_LIST.units.filter((u) => u.unit_type !== type)) {
      expectNoRow(other.canonical_id);
    }
    expect(typeSelect()).toHaveValue(type);
  });

  it.each([
    ['missing type', '/system'],
    ['empty type', '/system?type='],
    ['invalid type', '/system?type=notatype'],
  ])('treats %s as all types', async (_label, path) => {
    renderAt(path);
    await waitFor(() => expectRow('ModemManager.service'));
    expectRow('cups.socket');
    expect(typeSelect()).toHaveValue('');
  });

  it('uses the first value for repeated type parameters', async () => {
    renderAt('/system?type=socket&type=service');

    await waitFor(() => expectRow('cups.socket'));
    expectNoRow('ModemManager.service');
    expect(typeSelect()).toHaveValue('socket');
  });
});

describe('section links and current state', () => {
  it('marks Units current by default and Services not current', async () => {
    renderAt('/system');
    await waitFor(() => expectRow('ModemManager.service'));

    expect(sectionLink('Units')).toHaveAttribute('aria-current', 'page');
    expect(sectionLink('Services')).not.toHaveAttribute('aria-current');
  });

  it('marks Services current for service type and Units not current', async () => {
    renderAt('/system?type=service');
    await waitFor(() => expectRow('ModemManager.service'));

    expect(sectionLink('Services')).toHaveAttribute('aria-current', 'page');
    expect(sectionLink('Units')).not.toHaveAttribute('aria-current');
  });

  it('follows the Services link to the service filter', async () => {
    const user = userEvent.setup();
    renderAt('/system');
    await waitFor(() => expectRow('ModemManager.service'));

    await user.click(sectionLink('Services'));
    await waitFor(() =>
      expect(screen.getByTestId('address')).toHaveTextContent('/system?type=service'),
    );
    expectNoRow('cups.socket');
    expect(typeSelect()).toHaveValue('service');
    expect(sectionLink('Services')).toHaveAttribute('aria-current', 'page');
    expect(sectionLink('Units')).not.toHaveAttribute('aria-current');
  });

  it('follows the Units link back to all types', async () => {
    const user = userEvent.setup();
    renderAt('/system?type=service');
    await waitFor(() => expectRow('ModemManager.service'));

    await user.click(sectionLink('Units'));
    await waitFor(() => expect(screen.getByTestId('address').textContent).toBe('/system'));
    expectRow('cups.socket');
    expect(typeSelect()).toHaveValue('');
    expect(sectionLink('Units')).toHaveAttribute('aria-current', 'page');
    expect(sectionLink('Services')).not.toHaveAttribute('aria-current');
  });
});

describe('dropdown changes', () => {
  it('updates the URL and rows when a type is selected', async () => {
    const user = userEvent.setup();
    renderAt('/system');
    await waitFor(() => expectRow('ModemManager.service'));

    await user.selectOptions(typeSelect(), 'socket');
    await waitFor(() =>
      expect(screen.getByTestId('address')).toHaveTextContent('/system?type=socket'),
    );
    expectRow('cups.socket');
    expectNoRow('ModemManager.service');
  });

  it('preserves unrelated query parameters', async () => {
    const user = userEvent.setup();
    renderAt('/system?keep=this');
    await waitFor(() => expectRow('ModemManager.service'));

    await user.selectOptions(typeSelect(), 'timer');
    await waitFor(() =>
      expect(screen.getByTestId('address')).toHaveTextContent('/system?keep=this&type=timer'),
    );
  });

  it('removes the type parameter when All types is selected', async () => {
    const user = userEvent.setup();
    renderAt('/system?type=service&keep=this');
    await waitFor(() => expectRow('ModemManager.service'));

    await user.selectOptions(typeSelect(), '');
    await waitFor(() =>
      expect(screen.getByTestId('address')).toHaveTextContent('/system?keep=this'),
    );
  });
});

describe('history navigation', () => {
  it('restores type and rows via Back and Forward', async () => {
    const user = userEvent.setup();

    function HistoryHarness(): JSX.Element {
      const navigate = useNavigate();
      const location = useLocation();
      return (
        <>
          <SystemList />
          <button type="button" onClick={() => navigate(-1)}>
            Back
          </button>
          <button type="button" onClick={() => navigate(1)}>
            Forward
          </button>
          <span data-testid="address">{`${location.pathname}${location.search}`}</span>
        </>
      );
    }

    stubBackend({ '/api/v1/systemd/units': MIXED_LIST });
    render(
      <MemoryRouter initialEntries={['/system']}>
        <Routes>
          <Route path="system" element={<HistoryHarness />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expectRow('ModemManager.service'));
    await user.selectOptions(typeSelect(), 'service');
    await waitFor(() =>
      expect(screen.getByTestId('address')).toHaveTextContent('/system?type=service'),
    );
    expectNoRow('cups.socket');

    await user.click(screen.getByRole('button', { name: 'Back' }));
    await waitFor(() => expect(screen.getByTestId('address').textContent).toBe('/system'));
    expectRow('cups.socket');

    await user.click(screen.getByRole('button', { name: 'Forward' }));
    await waitFor(() =>
      expect(screen.getByTestId('address')).toHaveTextContent('/system?type=service'),
    );
    expectNoRow('cups.socket');
  });
});

describe('local filter intersection', () => {
  it('intersects search text with the selected type', async () => {
    const user = userEvent.setup();
    renderAt('/system?type=service');
    await waitFor(() => expectRow('ModemManager.service'));

    await user.type(screen.getByRole('searchbox'), 'cups');
    await waitFor(() => expect(screen.queryByRole('link', { name: 'ModemManager.service' })).not.toBeInTheDocument());
    expect(screen.getByText(/No unit matches/i)).toBeInTheDocument();

    await user.clear(screen.getByRole('searchbox'));
    await user.type(screen.getByRole('searchbox'), 'modem');
    await waitFor(() => expectRow('ModemManager.service'));
  });

  it('intersects active-state filter with the selected type', async () => {
    const user = userEvent.setup();
    renderAt('/system?type=service');
    await waitFor(() => expectRow('ModemManager.service'));

    await user.selectOptions(screen.getByRole('combobox', { name: /active state/i }), 'inactive');
    await waitFor(() =>
      expect(screen.queryByRole('link', { name: 'ModemManager.service' })).not.toBeInTheDocument(),
    );
    expect(screen.getByText(/No unit matches/i)).toBeInTheDocument();
  });
});

describe('inventory read behavior', () => {
  it('does not fetch the unit list again when the type changes', async () => {
    const user = userEvent.setup();
    renderAt('/system');
    await waitFor(() => expectRow('ModemManager.service'));

    const before = vi.mocked(fetch).mock.calls.filter(
      ([input]) => urlOf(input).includes('/systemd/units'),
    ).length;

    await user.selectOptions(typeSelect(), 'service');
    await waitFor(() =>
      expect(screen.getByTestId('address')).toHaveTextContent('/system?type=service'),
    );

    const after = vi.mocked(fetch).mock.calls.filter(
      ([input]) => urlOf(input).includes('/systemd/units'),
    ).length;
    expect(after).toBe(before);
  });
});

describe('degraded capability', () => {
  it('still surfaces the capability notice while filtering by type', async () => {
    const degraded: SystemdUnitList = {
      ...MIXED_LIST,
      capability: {
        capability: 'systemd.units.observe',
        version: 1,
        status: 'degraded',
        mutating: false,
        summary: '',
        reason: 'partial read',
        detail: {},
        discovered_at: '2026-08-27T21:46:40Z',
      },
    };
    renderAt('/system?type=service', { '/api/v1/systemd/units': degraded });
    await waitFor(() => expect(screen.getByText(/not fully available/i)).toBeInTheDocument());
    expect(screen.getByText(/partial read/i)).toBeInTheDocument();
    expectRow('ModemManager.service');
  });
});

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (reason?: unknown) => void } {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function unitListWith(units: SystemdUnit[], sweepId = 'swp_initial'): SystemdUnitList {
  const base = BACKEND['/api/v1/systemd/units'] as SystemdUnitList;
  return {
    ...base,
    count: units.length,
    units,
    last_sweep: {
      ...(base.last_sweep as NonNullable<SystemdUnitList['last_sweep']>),
      sweep_id: sweepId,
      capability: 'systemd.units.observe',
      status: 'ok',
      issues: [],
      object_count: units.length,
    },
  };
}

function renderSystem(): void {
  render(
    <MemoryRouter initialEntries={['/system']}>
      <Routes><Route path="system" element={<SystemList />} /></Routes>
    </MemoryRouter>,
  );
}

function unitReads(fetchMock: ReturnType<typeof vi.fn<typeof fetch>>) {
  return fetchMock.mock.calls.filter(([input, init]) =>
    urlOf(input) === '/api/v1/systemd/units' && (init?.method ?? 'GET') === 'GET');
}

function observationPosts(fetchMock: ReturnType<typeof vi.fn<typeof fetch>>) {
  return fetchMock.mock.calls.filter(([input, init]) =>
    urlOf(input) === '/api/v1/systemd/observations/refresh' && init?.method === 'POST');
}

describe('explicit systemd observation', () => {
  it.each(['pending', 'failed'] as const)('reports no retained records with a cold %s first GET and recovers with GET only', async (initial) => {
    const first = deferred<Response>();
    const post = deferred<Response>();
    let reads = 0;
    let recover = false;
    const fetchMock = vi.fn<typeof fetch>((input) => {
      const path = urlOf(input);
      if (path === '/api/v1/systemd/observations/refresh') return post.promise;
      if (path === '/api/v1/systemd/units') {
        reads += 1;
        if (reads === 1 && initial === 'pending') return first.promise;
        return Promise.resolve(recover ? response(unitListWith([unitOf('service', 'obj_svc', 'sshd.service')]))
          : response({ error: { code: 'store_busy', message: 'store busy', detail: {} } }, 503));
      }
      return Promise.resolve(response(BACKEND[path] ?? {}));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderSystem();
    if (initial === 'failed') await screen.findByText('Stored unit records could not be re-read');
    fireEvent.click(await screen.findByRole('button', { name: 'Observe units' }));
    expect(screen.getByText(/No stored records are currently visible/)).toBeInTheDocument();
    expect(screen.queryByText(/prior stored records remain visible|last visible records remain on screen/i)).not.toBeInTheDocument();
    await act(async () => post.resolve(response({ error: { code: 'unavailable', message: 'failed', detail: {} } }, 503)));
    await screen.findByText('Systemd units observation request failed');
    await screen.findByText('Stored unit records could not be re-read');
    expect(screen.queryByText(/prior stored records remain visible|last visible records remain on screen/i)).not.toBeInTheDocument();
    recover = true;
    fireEvent.click(screen.getByRole('button', { name: 'Re-read stored records' }));
    await screen.findByRole('link', { name: 'sshd.service' });
    expect(observationPosts(fetchMock)).toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([, init]) => (init?.method ?? 'GET') !== 'GET')).toHaveLength(1);
  });
  it('treats a malformed nested issue as uncertain while retaining systemd rows', async () => {
    const fetchMock = vi.fn<typeof fetch>((input) => {
      const path = urlOf(input);
      if (path === '/api/v1/systemd/observations/refresh') return Promise.resolve(response({
        host_id: 'host_1', sweep_id: 'swp_obs', status: 'ok', unit_count: 1, issues: [null], provider_version: '1', listed_count: 1, selected_count: 1, inventory_limit: null, inventory_complete: true, truncated: false, cap_reached: false, inventory_method: 'list', agent_unit_resolution: null,
      }));
      return Promise.resolve(response(path === '/api/v1/systemd/units'
        ? unitListWith([unitOf('service', 'obj_svc', 'sshd.service')], 'swp_before')
        : BACKEND[path] ?? {}));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderSystem();
    await screen.findByRole('link', { name: 'sshd.service' });
    fireEvent.click(screen.getByRole('button', { name: 'Observe units' }));
    await screen.findByText('Systemd units observation outcome is uncertain');
    expect(screen.getByRole('link', { name: 'sshd.service' })).toBeInTheDocument();
    await waitFor(() => expect(unitReads(fetchMock).length).toBeGreaterThan(1));
    expect(observationPosts(fetchMock)).toHaveLength(1);
  });
  it('issues one POST on click and guards rapid activation', async () => {
    const baseUnits = [unitOf('service', 'obj_svc', 'sshd.service')];
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/systemd/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({ host_id: 'host_1', sweep_id: 'swp_obs', status: 'ok', unit_count: 1, issues: [], provider_version: '1', listed_count: 1, selected_count: 1, inventory_limit: null, inventory_complete: true, truncated: false, cap_reached: false, inventory_method: 'list', agent_unit_resolution: null }));
      }
      if (path === '/api/v1/systemd/units') {
        return Promise.resolve(response(unitListWith(baseUnits, 'swp_obs')));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderSystem();

    const button = await screen.findByRole('button', { name: 'Observe units' });
    fireEvent.click(button);
    fireEvent.click(button);

    await screen.findByText('Systemd units observation completed');
    expect(observationPosts(fetchMock)).toHaveLength(1);
    expect(unitReads(fetchMock).length).toBeGreaterThanOrEqual(1);
  });

  it('keeps the observe button accessible when the initial list is empty', async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/systemd/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({ host_id: 'host_1', sweep_id: 'swp_empty', status: 'ok', unit_count: 0, issues: [], provider_version: '1', listed_count: 0, selected_count: 0, inventory_limit: null, inventory_complete: true, truncated: false, cap_reached: false, inventory_method: 'list', agent_unit_resolution: null }));
      }
      if (path === '/api/v1/systemd/units') {
        return Promise.resolve(response(unitListWith([], 'swp_empty')));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderSystem();

    const button = await screen.findByRole('button', { name: 'Observe units' });
    fireEvent.click(button);
    await screen.findByText('Systemd units observation completed');
    expect(observationPosts(fetchMock)).toHaveLength(1);
  });

  it('reports provider failure status separately from a lost request', async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/systemd/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({ host_id: 'host_1', sweep_id: 'swp_fail', status: 'failed', unit_count: 0, issues: [{ source: 'systemd.units.observe', code: 'dbus_error', message: 'D-Bus returned an error', detail: {} }], provider_version: '1', listed_count: null, selected_count: null, inventory_limit: null, inventory_complete: null, truncated: null, cap_reached: null, inventory_method: null, agent_unit_resolution: null }));
      }
      if (path === '/api/v1/systemd/units') {
        return Promise.resolve(response(unitListWith([], 'swp_before')));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderSystem();

    fireEvent.click(await screen.findByRole('button', { name: 'Observe units' }));
    expect(await screen.findByText('Systemd units observation reported provider failure')).toBeInTheDocument();
    expect(screen.getByText(/D-Bus returned an error/)).toBeInTheDocument();
  });

  it('uses console-wide re-read as GET-only and never starts a provider observation', async () => {
    const baseUnits = [unitOf('service', 'obj_svc', 'sshd.service')];
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(unitListWith(baseUnits, 'swp_before'))));
    renderSystem();

    await screen.findByRole('button', { name: 'Observe units' });
    act(() => refreshAll());
    await waitFor(() => expect(screen.getByRole('link', { name: 'sshd.service' })).toBeInTheDocument());
    expect(observationPosts(vi.mocked(fetch))).toHaveLength(0);
  });

  it('retains prior data when the POST fails and shows the error as uncertain', async () => {
    const baseUnits = [unitOf('service', 'obj_svc', 'sshd.service')];
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/systemd/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({ error: { code: 'agent_unavailable', message: 'Agent unavailable', detail: {} } }, 503));
      }
      if (path === '/api/v1/systemd/units') {
        return Promise.resolve(response(unitListWith(baseUnits, 'swp_before')));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderSystem();

    await screen.findByRole('link', { name: 'sshd.service' });
    fireEvent.click(screen.getByRole('button', { name: 'Observe units' }));
    expect(await screen.findByText(/Agent unavailable/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'sshd.service' })).toBeInTheDocument();
  });

  it('keeps a newer global stored GET when the POST fails late', async () => {
    const baseUnits = [unitOf('service', 'obj_initial', 'initial.service')];
    const newer = [unitOf('service', 'obj_global', 'global.service')];
    const post = deferred<Response>();
    let listCall = 0;
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/systemd/observations/refresh' && method === 'POST') {
        return post.promise;
      }
      if (path === '/api/v1/systemd/units') {
        listCall += 1;
        if (listCall === 1) return Promise.resolve(response(unitListWith(baseUnits, 'swp_initial')));
        return Promise.resolve(response(unitListWith(newer, 'swp_global')));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderSystem();

    await screen.findByRole('link', { name: 'initial.service' });
    fireEvent.click(screen.getByRole('button', { name: 'Observe units' }));

    // Global re-read arrives while the POST is still pending; the newer stored GET wins.
    act(() => refreshAll());
    await screen.findByRole('link', { name: 'global.service' });

    await act(async () =>
      post.resolve(response({ error: { code: 'agent_unavailable', message: 'Agent unavailable', detail: {} } }, 503))
    );

    expect(await screen.findByText(/Agent unavailable/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'global.service' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'initial.service' })).not.toBeInTheDocument();
  });

  it('recovers from a successful POST with a failed stored read by re-reading stored records only', async () => {
    const fresh = [unitOf('service', 'obj_fresh', 'fresh.service')];
    let getCall = 0;
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/systemd/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({ host_id: 'host_1', sweep_id: 'swp_fresh', status: 'ok', unit_count: 1, issues: [], provider_version: '1', listed_count: 1, selected_count: 1, inventory_limit: null, inventory_complete: true, truncated: false, cap_reached: false, inventory_method: 'list', agent_unit_resolution: null }));
      }
      if (path === '/api/v1/systemd/units') {
        getCall += 1;
        if (getCall === 2) {
          return Promise.resolve(response({ error: { code: 'store_busy', message: 'store busy', detail: {} } }, 503));
        }
        return Promise.resolve(response(getCall === 1 ? unitListWith([unitOf('service', 'obj_before', 'before.service')], 'swp_before') : unitListWith(fresh, 'swp_fresh')));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderSystem();

    fireEvent.click(await screen.findByRole('button', { name: 'Observe units' }));
    expect(await screen.findByText(/responded, but stored records could not be re-read/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Re-read stored records' }));
    expect(await screen.findByRole('link', { name: 'fresh.service' })).toBeInTheDocument();
    expect(observationPosts(fetchMock)).toHaveLength(1);
  });

  it('recovers from a global GET after a successful POST with a failed stored read', async () => {
    const fresh = [unitOf('service', 'obj_fresh', 'global.service')];
    let getCall = 0;
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/systemd/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({ host_id: 'host_1', sweep_id: 'swp_fresh', status: 'ok', unit_count: 1, issues: [], provider_version: '1', listed_count: 1, selected_count: 1, inventory_limit: null, inventory_complete: true, truncated: false, cap_reached: false, inventory_method: 'list', agent_unit_resolution: null }));
      }
      if (path === '/api/v1/systemd/units') {
        getCall += 1;
        if (getCall === 2) {
          return Promise.resolve(response({ error: { code: 'store_busy', message: 'store busy', detail: {} } }, 503));
        }
        return Promise.resolve(response(getCall === 1 ? unitListWith([unitOf('service', 'obj_before', 'before.service')], 'swp_before') : unitListWith(fresh, 'swp_fresh')));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderSystem();

    fireEvent.click(await screen.findByRole('button', { name: 'Observe units' }));
    expect(await screen.findByText(/responded, but stored records could not be re-read/)).toBeInTheDocument();

    act(() => refreshAll());
    expect(await screen.findByRole('link', { name: 'global.service' })).toBeInTheDocument();
    expect(observationPosts(fetchMock)).toHaveLength(1);
  });

  it('reports a sweep mismatch explicitly', async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = urlOf(input);
      const method = init?.method ?? 'GET';
      if (path === '/api/v1/systemd/observations/refresh' && method === 'POST') {
        return Promise.resolve(response({ host_id: 'host_1', sweep_id: 'swp_returned', status: 'ok', unit_count: 1, issues: [], provider_version: '1', listed_count: 1, selected_count: 1, inventory_limit: null, inventory_complete: true, truncated: false, cap_reached: false, inventory_method: 'list', agent_unit_resolution: null }));
      }
      if (path === '/api/v1/systemd/units') {
        return Promise.resolve(response(unitListWith([unitOf('service', 'obj_svc', 'sshd.service')], 'swp_stored')));
      }
      const body = BACKEND[path];
      if (body !== undefined) return Promise.resolve(response(body));
      return Promise.resolve(response({ error: { code: 'not_found', message: `${method} missing`, detail: {} } }, 404));
    });
    vi.stubGlobal('fetch', fetchMock);
    renderSystem();

    fireEvent.click(await screen.findByRole('button', { name: 'Observe units' }));
    expect(await screen.findByText('Systemd units observation completed')).toBeInTheDocument();
    expect(screen.getByText(/latest sweep differs from this returned response/i)).toBeInTheDocument();
  });
});
