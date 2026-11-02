import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { endpoints } from '@/api/endpoints';
import { MemoryRouter } from 'react-router-dom';
import { ServiceJournal } from './ServiceJournal';

vi.mock('@/api/endpoints', () => ({ endpoints: { systemdJournal: vi.fn() } }));
const mockRead = vi.mocked(endpoints.systemdJournal);
const renderJournal = (objectId = 'obj-1', canonicalId = 'worker.service') =>
  render(<MemoryRouter><ServiceJournal objectId={objectId} canonicalId={canonicalId} /></MemoryRouter>);
const response = (fields: Record<string, unknown> = {}) => ({
  object_id: 'obj-1', canonical_id: 'worker.service', status: 'ok', retrieved_at: 'now',
  since: '15 minutes', boot_id: null, truncated: false, coverage: 'complete', entries: [{ timestamp: null, priority: null, message: '<img onerror=x> literal', boot_id: null, invocation_id: null }], gaps: [], ...fields,
}) as never;

afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe('ServiceJournal', () => {
  it('does not read automatically; exposes the warning before each explicit read and renders message as text', async () => {
    mockRead.mockResolvedValue(response());
    renderJournal();
    expect(mockRead).not.toHaveBeenCalled();
    expect(screen.getByText(/may contain sensitive information/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /acknowledge and read/i }));
    await screen.findByText('<img onerror=x> literal');
    expect(mockRead).toHaveBeenCalledWith('obj-1', expect.objectContaining({ signal: expect.any(AbortSignal) }));
    fireEvent.click(screen.getByRole('button', { name: /acknowledge and read/i }));
    await waitFor(() => expect(mockRead).toHaveBeenCalledTimes(2));
  });

  it('withholds data on identity mismatch and reports empty and provider status distinctly', async () => {
    mockRead.mockResolvedValue(response({ canonical_id: 'different.service' }));
    const { rerender } = renderJournal();
    fireEvent.click(screen.getByRole('button', { name: /acknowledge and read/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/did not match/i);
    expect(screen.queryByText(/literal/)).not.toBeInTheDocument();
    mockRead.mockResolvedValue(response({ status: 'empty', entries: [] }));
    rerender(<MemoryRouter><ServiceJournal objectId="obj-1" canonicalId="worker.service" /></MemoryRouter>);
    fireEvent.click(screen.getByRole('button', { name: /acknowledge and read/i }));
    expect(await screen.findByText(/no matching entries/i)).toBeInTheDocument();
  });

  it('distinguishes partial access coverage from fixed-limit truncation while retaining exact matches', async () => {
    mockRead.mockResolvedValue(response({ coverage: 'partial', truncated: false, gaps: ['some journal files were inaccessible'] }));
    renderJournal();
    fireEvent.click(screen.getByRole('button', { name: /acknowledge and read/i }));
    expect(await screen.findByText(/partial journal-file access/i)).toBeInTheDocument();
    expect(screen.getByText('<img onerror=x> literal')).toBeInTheDocument();
    expect(screen.queryByText(/truncated by the fixed collection limits/i)).not.toBeInTheDocument();
  });

  it('never presents an empty result when journal-file coverage is incomplete', async () => {
    mockRead.mockResolvedValue(response({ status: 'empty', coverage: 'partial', canonical_id: 'worker.service', entries: [] }));
    renderJournal();
    fireEvent.click(screen.getByRole('button', { name: /acknowledge and read/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/unavailable/i);
    expect(screen.queryByText(/no matching entries/i)).not.toBeInTheDocument();
  });

  it('labels unknown coverage independently from truncation', async () => {
    mockRead.mockResolvedValue(response({ coverage: 'unknown', truncated: true }));
    renderJournal();
    fireEvent.click(screen.getByRole('button', { name: /acknowledge and read/i }));
    expect(await screen.findByText(/coverage is unknown/i)).toBeInTheDocument();
    expect(screen.getByText(/truncated by the fixed collection limits/i)).toBeInTheDocument();
  });

  it('distinguishes absent MESSAGE from empty text without losing neighboring entries or claiming empty collection', async () => {
    mockRead.mockResolvedValue(response({ entries: [
      { timestamp: 't1', priority: '6', message: 'neighbor one', boot_id: 'boot', invocation_id: null },
      { timestamp: 't2', priority: '5', message: null, boot_id: 'boot', invocation_id: null },
      { timestamp: 't3', priority: '4', message: '', boot_id: 'boot', invocation_id: null },
      { timestamp: 't4', priority: '3', message: 'neighbor two', boot_id: 'boot', invocation_id: null },
    ] }));
    renderJournal();
    fireEvent.click(screen.getByRole('button', { name: /acknowledge and read/i }));
    expect(await screen.findByText('neighbor one')).toBeInTheDocument();
    expect(screen.getByText('MESSAGE not supplied for this entry')).toBeInTheDocument();
    expect(screen.getByText('Empty MESSAGE text')).toBeInTheDocument();
    expect(screen.getByText('neighbor two')).toBeInTheDocument();
    expect(screen.queryByText(/no matching entries/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each(['denied', 'unavailable', 'unsupported', 'timeout', 'busy', 'identity_changed', 'malformed'] as const)(
    'shows the typed %s state when canonical identity is unavailable, without entries', async (status) => {
      mockRead.mockResolvedValue(response({ status, canonical_id: null, entries: [], gaps: ['safe bounded reason'] }));
      renderJournal();
      fireEvent.click(screen.getByRole('button', { name: /acknowledge and read/i }));
      const label = status === 'timeout' ? 'timed out' : status.replace('_', ' ');
      expect(await screen.findByText(new RegExp(label, 'i'))).toBeInTheDocument();
      expect(screen.queryByText(/literal/)).not.toBeInTheDocument();
    },
  );

  it.each([
    ['HTTP rejection', () => mockRead.mockRejectedValue(new Error('private provider detail'))],
    ['unknown status', () => mockRead.mockResolvedValue(response({ status: 'future_status' }))],
    ['malformed entries', () => mockRead.mockResolvedValue(response({ entries: [{ message: 'must not render' }] }))],
    ['malformed message object', () => mockRead.mockResolvedValue(response({ entries: [{ timestamp: null, priority: null, message: {}, boot_id: null, invocation_id: null }] }))],
    ['malformed message array', () => mockRead.mockResolvedValue(response({ entries: [{ timestamp: null, priority: null, message: [], boot_id: null, invocation_id: null }] }))],
    ['malformed message number', () => mockRead.mockResolvedValue(response({ entries: [{ timestamp: null, priority: null, message: 7, boot_id: null, invocation_id: null }] }))],
    ['failure with entries', () => mockRead.mockResolvedValue(response({ status: 'denied', canonical_id: null }))],
    ['malformed gaps', () => mockRead.mockResolvedValue(response({ gaps: [42] }))],
    ['unknown coverage', () => mockRead.mockResolvedValue(response({ coverage: 'somewhere' }))],
    ['missing coverage', () => mockRead.mockResolvedValue(response({ coverage: undefined }))],
  ])('withholds malformed or rejected %s payloads', async (_label, configure) => {
    configure();
    renderJournal();
    fireEvent.click(screen.getByRole('button', { name: /acknowledge and read/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/unavailable/i);
    expect(screen.queryByText(/literal|must not render|private provider detail/)).not.toBeInTheDocument();
  });

  it('aborts and discards a late response when the panel closes', async () => {
    let finish!: (value: never) => void;
    mockRead.mockImplementation((_id, { signal }) => new Promise((resolve) => {
      signal?.addEventListener('abort', () => { /* request is cancelled on unmount */ });
      finish = resolve;
    }));
    const view = renderJournal();
    fireEvent.click(screen.getByRole('button', { name: /acknowledge and read/i }));
    const signal = mockRead.mock.calls[0]?.[1].signal;
    expect(signal).toBeDefined();
    view.unmount();
    expect(signal?.aborted).toBe(true);
    finish(response());
    expect(screen.queryByText(/literal/)).not.toBeInTheDocument();
  });

  it('does not retain displayed messages when the panel is reopened', async () => {
    mockRead.mockResolvedValue(response());
    const first = renderJournal();
    fireEvent.click(screen.getByRole('button', { name: /acknowledge and read/i }));
    await screen.findByText('<img onerror=x> literal');
    first.unmount();
    renderJournal();
    expect(screen.queryByText('<img onerror=x> literal')).not.toBeInTheDocument();
    expect(mockRead).toHaveBeenCalledTimes(1);
  });

  it('aborts and withholds a late response after the requested identity changes', async () => {
    let finish!: (value: never) => void;
    mockRead.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const view = renderJournal();
    fireEvent.click(screen.getByRole('button', { name: /acknowledge and read/i }));
    const signal = mockRead.mock.calls[0]?.[1].signal;
    view.rerender(<MemoryRouter><ServiceJournal objectId="obj-2" canonicalId="other.service" /></MemoryRouter>);
    expect(signal?.aborted).toBe(true);
    await act(async () => finish(response()));
    expect(screen.queryByText(/literal/)).not.toBeInTheDocument();
  });
});
