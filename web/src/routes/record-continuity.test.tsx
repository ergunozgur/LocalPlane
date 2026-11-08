import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/api/client';
import { endpoints } from '@/api/endpoints';
import type { Change } from '@/api/types';
import { BACKEND } from '@/test/backend';
import runFixture from '@/test/fixtures/systemd-run.json';
import { RunDetail } from './operations/RunDetail';
import { ChangeDetail } from './operations/ChangeDetail';

type Kind = 'run' | 'change';
let sequence = 0;
const pathFor = (kind: Kind, id: string) => `/${kind === 'run' ? 'runs' : 'changes'}/${id}`;
const regionFor = (kind: Kind) => kind === 'run' ? 'Change Composer' : 'Recovery console';
const refreshFor = (kind: Kind) => kind === 'run' ? 'Refresh Run' : 'Refresh recorded status';
const keyFor = (kind: Kind, id: string) => `localplane.${kind === 'run' ? 'run' : 'recovery'}-intents:${id}`;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
function Address(): JSX.Element {
  return <span data-testid="record-address">{useLocation().pathname}</span>;
}
function show(kind: Kind, id: string) {
  return render(<MemoryRouter initialEntries={[`/operations${pathFor(kind, id)}`]}><Routes>
    <Route path="/operations/runs/:runId" element={<RunDetail />} />
    <Route path="/operations/changes/:changeId" element={<ChangeDetail />} />
  </Routes><Address /></MemoryRouter>);
}
function fixture(kind: Kind, id: string) {
  const run = { ...runFixture, run_id: kind === 'run' ? id : `related_${id}` };
  const change = { ...(structuredClone(BACKEND['/api/v1/changes/chg_1']) as Change), change_id: id, run_id: run.run_id };
  const fetch = vi.fn(async (path: string, _options: RequestInit) => path.includes('/recovery-holds') ? json({ total_count: 1, changes: [] }) : path.includes('/runs/') ? json(run) : json(change));
  vi.stubGlobal('fetch', fetch);
  return fetch;
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); sessionStorage.clear(); });

describe.each(['run', 'change'] as const)('%s record cache truth', (kind) => {
  it.each(['cold', 'warm'])('an exact structured main-record 404 clears the %s view without erasing intents or URL', async (cache) => {
    const id = `missing_${++sequence}`;
    const fetch = fixture(kind, id);
    const intents = JSON.stringify(['Applying', 'Confirming:preview:digest', 'retry:old-grant', 'historical-ambiguous']);
    sessionStorage.setItem(keyFor(kind, id), intents);
    sessionStorage.setItem(`localplane.apply-attempt:${id}`, 'sent');
    const missing = new ApiError({ kind: 'backend', status: 404, code: `${kind}_not_found`, path: pathFor(kind, id), message: 'No such record' });
    const refuse = () => {
      if (kind === 'run') vi.spyOn(endpoints, 'run').mockRejectedValue(missing);
      else vi.spyOn(endpoints, 'change').mockRejectedValue(missing);
    };
    if (cache === 'cold') refuse();
    show(kind, id);
    if (cache === 'warm') {
      await screen.findByRole('region', { name: regionFor(kind) });
      await waitFor(() => expect(screen.getByRole('button', { name: refreshFor(kind) })).toBeEnabled());
      refuse(); fireEvent.click(screen.getByRole('button', { name: refreshFor(kind) }));
    }
    expect(await screen.findByRole('alert')).toHaveTextContent(`${kind}_not_found`);
    expect(screen.queryByRole('region', { name: regionFor(kind) })).not.toBeInTheDocument();
    expect(screen.getByTestId('record-address')).toHaveTextContent(`/operations${pathFor(kind, id)}`);
    expect(sessionStorage.getItem(keyFor(kind, id))).toBe(intents);
    expect(sessionStorage.getItem(`localplane.apply-attempt:${id}`)).toBe('sent');
    expect(fetch.mock.calls.every(([, options]) => options.method === 'GET')).toBe(true);
  });

  it.each(['timeout', 'server-error', 'generic-404', 'malformed-404', 'other-entity-404'])('retains last-known data and a qualified read error for %s, without POST', async (failure) => {
    const id = `retained_${++sequence}`; const fetch = fixture(kind, id);
    show(kind, id);
    await screen.findByRole('region', { name: regionFor(kind) });
    await waitFor(() => expect(screen.getByRole('button', { name: refreshFor(kind) })).toBeEnabled());
    const error = new ApiError({
      kind: failure === 'timeout' ? 'timeout' : failure === 'malformed-404' ? 'malformed' : 'backend',
      status: failure === 'timeout' ? null : failure === 'server-error' ? 500 : 404,
      code: failure === 'generic-404' ? 'http_404' : `${kind}_not_found`,
      path: failure === 'other-entity-404' ? pathFor(kind, 'different-record') : pathFor(kind, id),
      message: `Read unavailable: ${failure}`,
    });
    if (kind === 'run') vi.spyOn(endpoints, 'run').mockRejectedValue(error);
    else vi.spyOn(endpoints, 'change').mockRejectedValue(error);
    fireEvent.click(screen.getByRole('button', { name: refreshFor(kind) }));
    expect(await screen.findByRole('alert')).toHaveTextContent(`Read unavailable: ${failure}`);
    expect(screen.getByRole('region', { name: regionFor(kind) })).toBeInTheDocument();
    expect(screen.getByText(/Retained.*data may be stale/)).toBeInTheDocument();
    expect(screen.getByTestId('record-address')).toHaveTextContent(`/operations${pathFor(kind, id)}`);
    expect(fetch.mock.calls.every(([, options]) => options.method === 'GET')).toBe(true);
  });
});

it.each(['related-run', 'holds'])('an optional %s 404 does not clear a valid Change', async (related) => {
  const id = `related_missing_${++sequence}`; const fetch = fixture('change', id);
  const missing = new ApiError({ kind: 'backend', status: 404, code: related === 'related-run' ? 'run_not_found' : 'change_not_found', path: related === 'related-run' ? `/runs/related_${id}` : '/changes/recovery-holds', message: 'Related record unavailable' });
  if (related === 'related-run') vi.spyOn(endpoints, 'run').mockRejectedValue(missing);
  else vi.spyOn(endpoints, 'recoveryHolds').mockRejectedValue(missing);
  show('change', id);
  expect(await screen.findByRole('alert')).toHaveTextContent('Related record unavailable');
  expect(screen.getByRole('region', { name: 'Recovery console' })).toBeInTheDocument();
  expect(screen.getByTestId('record-address')).toHaveTextContent(`/operations/changes/${id}`);
  expect(fetch.mock.calls.every(([, options]) => options.method === 'GET')).toBe(true);
});
