/**
 * SystemList URL-driven type filtering.
 *
 * The unit-type dropdown is now the visible face of the `type` query parameter. These tests
 * assert that direct URL entry, section links, dropdown changes, history navigation, and
 * local search all agree, without issuing another unit-inventory request.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
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
