import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom';
import { UnitDetail } from './UnitDetail';
import { BACKEND, stubBackend, urlOf } from '@/test/backend';
import type { SystemdUnit, SystemdUnitList, SystemdRelationship } from '@/api/types';

afterEach(() => vi.unstubAllGlobals());

const base = BACKEND['/api/v1/systemd/units/obj_unit'] as SystemdUnit;
const list = BACKEND['/api/v1/systemd/units'] as SystemdUnitList;
const sourceId = 'source:with space#?';
const relation: SystemdRelationship = {
  kind: 'After', group: 'ordering', target_unit: base.canonical_id,
  canonical_target: null, target_object_id: base.object_id,
  resolution: 'resolved', estate_state: 'current', source: 'systemd.dbus',
};
const detail: SystemdUnit = {
  ...base,
  relationships: [{ ...relation, kind: 'Wants', target_unit: 'outbound.service', target_object_id: 'outbound:with space' }],
  incoming_references: {
    host_id: 'host_1', last_inventory_sweep: { ...list.last_sweep!, status: 'partial' },
    inventory_freshness: 'stale', excluded_ambiguous_count: 1,
    references: [{
      source_object_id: sourceId, source_canonical_id: 'source.service',
      source_observed_in_latest_sweep: true,
      source_observation: { ...base.observation!, freshness: 'stale', fidelity: 'partial', gaps: ['read failed'] },
      relationship: relation,
    }],
  },
};

function mount(path = '/system/obj_unit?tab=relationships') {
  return render(<MemoryRouter initialEntries={[path]}>
    <Link to="/system/next?tab=relationships">Next unit</Link>
    <Routes><Route path="system/:objectId" element={<UnitDetail />} /></Routes>
  </MemoryRouter>);
}

describe('incoming systemd references', () => {
  it('preserves direction, shows snapshot limitations and filters both directions', async () => {
    stubBackend({ '/api/v1/systemd/units/obj_unit': detail });
    mount();
    const incoming = await screen.findByRole('table', { name: 'Incoming unit references' });
    expect(within(incoming).getByText('After')).toBeInTheDocument();
    expect(within(incoming).queryByText('Before')).not.toBeInTheDocument();
    expect(screen.getByText(/not a complete dependency graph/)).toBeInTheDocument();
    expect(screen.getByText(/conflicting alias ownership/)).toBeInTheDocument();
    expect(screen.getByText('read failed')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'outbound.service' })).toHaveAttribute('href', '/system/outbound%3Awith%20space');
    const filter = screen.getByRole('searchbox', { name: 'Filter relationships' });
    fireEvent.change(filter, { target: { value: 'After' } });
    expect(screen.getByRole('link', { name: 'source.service' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'outbound.service' })).not.toBeInTheDocument();
    fireEvent.change(filter, { target: { value: 'missing' } });
    expect(screen.getByText('No incoming reference matches')).toBeInTheDocument();
    expect(screen.getByText(/No matches does not prove no dependents/)).toBeInTheDocument();
  });

  it('activates encoded source routes, preserves the tab and resets local filter', async () => {
    stubBackend({
      '/api/v1/systemd/units/obj_unit': detail,
      [`/api/v1/systemd/units/${encodeURIComponent(sourceId)}`]: { ...base, object_id: sourceId, canonical_id: 'source.service' },
    });
    mount();
    const link = await screen.findByRole('link', { name: 'source.service' });
    expect(link).toHaveAttribute('href', `/system/${encodeURIComponent(sourceId)}?tab=relationships`);
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'source' } });
    link.focus();
    await userEvent.keyboard('{Enter}');
    await screen.findByText('Incoming references unavailable');
    expect(screen.getByRole('tab', { name: /Relationships/ })).toHaveAttribute('aria-selected', 'true');
    expect(vi.mocked(fetch).mock.calls.some(([input]) => urlOf(input) === `/api/v1/systemd/units/${encodeURIComponent(sourceId)}`)).toBe(true);
    expect(screen.getByRole('searchbox')).toHaveValue('');
  });

  it.each(['failed', 'partial', 'ok', null])('does not claim no dependents for an empty %s inventory', async (status) => {
    stubBackend({ '/api/v1/systemd/units/obj_unit': {
      ...detail, incoming_references: {
        ...detail.incoming_references!, references: [],
        last_inventory_sweep: status ? { ...list.last_sweep!, status } : null,
      },
    } });
    mount();
    await screen.findByText('No stored incoming references');
    expect(screen.getByText(/Dependents outside this evidence remain unknown/)).toBeInTheDocument();
  });

  it('shows loading and errors instead of empty reference claims', async () => {
    let finish!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => urlOf(input).endsWith('/obj_unit')
      ? new Promise<Response>((resolve) => { finish = resolve; })
      : Promise.resolve(new Response(JSON.stringify(list)))));
    mount();
    expect(screen.getByText('Reading unit…')).toBeInTheDocument();
    expect(screen.queryByText('No stored incoming references')).not.toBeInTheDocument();
    await act(async () => finish(new Response('{}', { status: 500 })));
    await screen.findByRole('button', { name: /try again/i });
    expect(screen.queryByText('No stored incoming references')).not.toBeInTheDocument();
  });

  it('ignores an obsolete response after navigation', async () => {
    let finish!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = urlOf(input);
      if (url.endsWith('/obj_unit')) return new Promise<Response>((resolve) => { finish = resolve; });
      const body = url.endsWith('/next') ? { ...base, object_id: 'next', canonical_id: 'next.service' } : BACKEND[url];
      return Promise.resolve(new Response(JSON.stringify(body ?? {}), { status: body ? 200 : 404 }));
    }));
    mount();
    await userEvent.click(screen.getByRole('link', { name: 'Next unit' }));
    await screen.findByText('Incoming references unavailable');
    await act(async () => finish(new Response(JSON.stringify(detail))));
    await waitFor(() => expect(screen.queryByRole('link', { name: 'source.service' })).not.toBeInTheDocument());
  });
});
