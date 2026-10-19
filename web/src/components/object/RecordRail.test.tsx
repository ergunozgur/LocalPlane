import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useParams } from 'react-router-dom';
import { RecordRail } from './RecordRail';
import { refreshAll } from '@/hooks/useResource';
import { BACKEND } from '@/test/backend';
import type { ChangeList, FindingList, RunList } from '@/api/types';

const mocks = vi.hoisted(() => ({ findings: vi.fn(), changes: vi.fn(), runs: vi.fn() }));
vi.mock('@/api/endpoints', () => ({ endpoints: mocks }));

const findings = BACKEND['/api/v1/findings'] as FindingList;
const changes = BACKEND['/api/v1/changes'] as ChangeList;
const run: RunList['runs'][number] = {
  run_id: 'run/one?#', host_id: 'host_1', object_id: 'obj_if', object_name: 'lan',
  operation: 'network.interface.reconcile_mtu', state: 'applying',
  created_at: '2026-08-27T21:49:00Z', cancelled_at: null, finished_at: null,
  host_effect: 'write_unknown', change_created: true, change_id: 'change/one?#',
  preview: {
    preview_id: 'preview_1', preview_digest: 'digest', published_at: '2026-08-27T21:49:00Z',
    kind: 'field', action: null, observed_state: null, expected_state: null,
    risk_tier: 'low', confirmation_required: true, execution_availability: 'implemented',
    execution_eligibility: 'eligible', blockers: [],
    validity: { state: 'current', reasons: [], as_of: '2026-08-27T21:49:00Z' },
  },
};
const runs: RunList = { host_id: 'host_1', state: 'all', count: 1, runs: [run] };

function emptyLists(): void {
  mocks.findings.mockResolvedValue({ ...findings, count: 0, findings: [] });
  mocks.changes.mockResolvedValue({ ...changes, count: 0, changes: [] });
  mocks.runs.mockResolvedValue({ ...runs, count: 0, runs: [] });
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function RecordRoute(): JSX.Element {
  const { recordId } = useParams();
  return <span data-testid="opened-record">{recordId}</span>;
}

function Rail({ objectId = 'obj_if' }: { objectId?: string }): JSX.Element {
  return (
    <MemoryRouter>
      <RecordRail objectId={objectId} objectName="lan" />
      <Routes>
        <Route path="/operations/:kind/:recordId" element={<RecordRoute />} />
        <Route path="*" element={null} />
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => { vi.resetAllMocks(); emptyLists(); });

describe('object record investigation', () => {
  it('requests bounded all-state lists using the exact object identity, without mutations', async () => {
    render(<Rail objectId="object/id?#" />);
    await screen.findByText('No loaded records');
    expect(mocks.findings).toHaveBeenCalledWith({ object_id: 'object/id?#', status: 'all', limit: 12 }, { signal: expect.any(AbortSignal) });
    expect(mocks.runs).toHaveBeenCalledWith({ object_id: 'object/id?#', state: 'all', limit: 12 }, { signal: expect.any(AbortSignal) });
    expect(mocks.changes).toHaveBeenCalledWith({ object_id: 'object/id?#', limit: 12 }, { signal: expect.any(AbortSignal) });
    expect(screen.getByText(/Not exhaustive history/)).toBeInTheDocument();
    expect(screen.queryByText('Nothing has happened to this object')).not.toBeInTheDocument();
  });

  it('includes resolved findings without a verification marker or repair claim', async () => {
    mocks.findings.mockResolvedValue({ ...findings, status: 'all', findings: [{
      ...findings.findings[0]!, status: 'resolved', resolution: 'intent_released',
    }] });
    render(<Rail />);
    const record = (await screen.findByRole('link', { name: findings.findings[0]!.summary })).closest('li')!;
    expect(record).toHaveTextContent('Finding · resolved');
    expect(record).toHaveTextContent('intent released');
    expect(record.className).not.toMatch(/verify/);
    expect(screen.getByText(/resolved Findings do not by themselves prove repair/)).toBeInTheDocument();
  });

  it.each(['loading', 'failed', 'empty', 'present'])('keeps change-producing Runs when Changes is %s', async (state) => {
    mocks.runs.mockResolvedValue(runs);
    if (state === 'loading') mocks.changes.mockReturnValue(new Promise(() => {}));
    if (state === 'failed') mocks.changes.mockRejectedValue(new Error('unreadable'));
    if (state === 'present') mocks.changes.mockResolvedValue(changes);
    render(<Rail />);
    expect(await screen.findByText('Run · applying · host effect write unknown')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Change record' })).toHaveAttribute('href', '/operations/changes/change%2Fone%3F%23');
    expect(screen.queryByText(/nothing was written/)).not.toBeInTheDocument();
    if (state === 'loading') expect(await screen.findByText('Reading changes…')).toBeInTheDocument();
    if (state === 'failed') expect(await screen.findByText(/changes stream could not be read/)).toBeInTheDocument();
    if (state === 'present') expect(await screen.findByText('Change · recovery required · host effect write unknown')).toBeInTheDocument();
  });

  it('uses the recorded host effect even when change_created is false', async () => {
    mocks.runs.mockResolvedValue({ ...runs, runs: [{ ...run, change_created: false, change_id: null }] });
    render(<Rail />);
    expect(await screen.findByText('Run · applying · host effect write unknown')).toBeInTheDocument();
    expect(screen.queryByText(/nothing was written/)).not.toBeInTheDocument();
  });

  it.each(['findings', 'changes', 'runs'] as const)('opens the exact encoded %s record with keyboard activation', async (kind) => {
    const id = 'record/id?#';
    if (kind === 'findings') mocks.findings.mockResolvedValue({ ...findings, findings: [{ ...findings.findings[0]!, finding_id: id }] });
    if (kind === 'changes') mocks.changes.mockResolvedValue({ ...changes, changes: [{ ...changes.changes[0]!, change_id: id }] });
    if (kind === 'runs') mocks.runs.mockResolvedValue({ ...runs, runs: [{ ...run, run_id: id, change_id: null }] });
    render(<Rail />);
    const link = await screen.findByRole('link');
    expect(link).toHaveAttribute('href', `/operations/${kind}/record%2Fid%3F%23`);
    link.focus();
    await userEvent.keyboard('{Enter}');
    expect(await screen.findByTestId('opened-record')).toHaveTextContent(id);
    expect(mocks[kind]).toHaveBeenCalledTimes(1);
  });

  it('bounds the combined view while kind filters expose loaded records hidden by that bound', async () => {
    mocks.runs.mockResolvedValue({ ...runs, count: 12, runs: Array.from({ length: 12 }, (_, i) => ({
      ...run, run_id: `run_${i}`, change_id: null, operation: `run-${i}`,
      created_at: '2026-08-28T00:00:00Z',
    })) });
    mocks.changes.mockResolvedValue(changes);
    mocks.findings.mockResolvedValue(findings);
    render(<Rail />);
    expect(await screen.findByText(/Showing 12 of 14 loaded records/)).toBeInTheDocument();
    expect(screen.getAllByRole('listitem')).toHaveLength(12);
    expect(screen.queryByRole('link', { name: findings.findings[0]!.summary })).not.toBeInTheDocument();
    const filter = screen.getByRole('button', { name: 'Findings' });
    filter.focus();
    await userEvent.keyboard(' ');
    expect(filter).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('link', { name: findings.findings[0]!.summary })).toBeInTheDocument();
    expect(screen.getByText('Showing 1 of 1 loaded findings.')).toBeInTheDocument();
    expect(mocks.findings).toHaveBeenCalledTimes(1);
  });

  it('orders by creation/opening instants, not finish/last-seen time or timezone text', async () => {
    mocks.runs.mockResolvedValue({ ...runs, runs: [{ ...run, operation: 'old-run', change_id: null,
      created_at: '2026-08-27T23:45:00+02:00', finished_at: '2026-09-01T00:00:00Z' }] });
    mocks.changes.mockResolvedValue({ ...changes, changes: [{ ...changes.changes[0]!, operation: 'new-change',
      created_at: '2026-08-27T21:50:00Z' }] });
    mocks.findings.mockResolvedValue({ ...findings, findings: [{ ...findings.findings[0]!,
      summary: 'oldest-finding', first_seen_at: '2026-08-20T00:00:00Z', last_seen_at: '2026-09-02T00:00:00Z' }] });
    render(<Rail />);
    await screen.findByRole('link', { name: 'oldest-finding' });
    expect(screen.getAllByRole('listitem').map((entry) => within(entry).getByRole('link').textContent))
      .toEqual(['new-change', 'old-run', 'oldest-finding']);
    expect(screen.getByRole('link', { name: 'oldest-finding' }).closest('li')).toHaveTextContent('Opened');
  });

  it('shows independent pending sources alongside available records, never a settled empty state', async () => {
    mocks.findings.mockReturnValue(new Promise(() => {}));
    mocks.changes.mockResolvedValue(changes);
    render(<Rail />);
    await screen.findByRole('link');
    expect(screen.getByText('Reading findings…')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Findings' }));
    expect(screen.queryByText('No loaded records')).not.toBeInTheDocument();
  });

  it('retries only a failed source without losing other records', async () => {
    mocks.findings.mockRejectedValueOnce(new Error('unreadable')).mockResolvedValue(findings);
    mocks.runs.mockResolvedValue(runs);
    render(<Rail />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry findings' }));
    expect(await screen.findByRole('link', { name: findings.findings[0]!.summary })).toBeInTheDocument();
    expect(screen.getByText('Run · applying · host effect write unknown')).toBeInTheDocument();
    expect(mocks.runs).toHaveBeenCalledTimes(1);
    expect(mocks.changes).toHaveBeenCalledTimes(1);
    expect(mocks.findings).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('button', { name: 'Retry findings' })).not.toBeInTheDocument();
  });

  it('labels retained data during refresh and rereads all three sources', async () => {
    mocks.runs.mockResolvedValueOnce(runs).mockReturnValue(new Promise(() => {}));
    render(<Rail />);
    await screen.findByText('Run · applying · host effect write unknown');
    act(() => refreshAll());
    expect(await screen.findByText(/Refreshing runs; previously read records remain visible/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Change record' })).toBeInTheDocument();
    expect(mocks.runs).toHaveBeenCalledTimes(2);
  });

  it('clears old records and selection on object navigation, aborting and ignoring late results', async () => {
    const late = deferred<FindingList>();
    mocks.findings.mockReturnValueOnce(late.promise).mockResolvedValue({ ...findings, findings: [] });
    mocks.runs.mockResolvedValueOnce(runs).mockResolvedValue({ ...runs, runs: [] });
    const view = render(<Rail />);
    await screen.findByText('Run · applying · host effect write unknown');
    fireEvent.click(screen.getByRole('button', { name: 'Runs' }));
    const signal = mocks.findings.mock.calls[0]![1].signal as AbortSignal;
    view.rerender(<Rail objectId="other-object" />);
    expect(screen.queryByText('Run · applying · host effect write unknown')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'All' })).toHaveAttribute('aria-pressed', 'true');
    expect(signal.aborted).toBe(true);
    await act(async () => late.resolve(findings));
    await waitFor(() => expect(screen.getByText('No loaded records')).toBeInTheDocument());
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(mocks.findings.mock.calls[1]![0].object_id).toBe('other-object');
  });
});
