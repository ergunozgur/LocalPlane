import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BrowserRouter, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { request } from '@/api/client';
import { endpoints } from '@/api/endpoints';
import { AuthenticationProvider, useAuthentication } from './AuthProvider';

const SESSION = {
  authenticated: true as const,
  mechanism: 'session' as const,
  expires_at: '2026-09-02T08:00:00Z',
};

function response(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function unauthenticated(): Response {
  return response({
    error: {
      code: 'authentication_required',
      message: 'authentication is required',
      detail: {},
    },
  }, 401);
}

function Location(): JSX.Element {
  const location = useLocation();
  return <div>shell:{location.pathname}</div>;
}

function Logout(): JSX.Element {
  const { logout, logoutError } = useAuthentication();
  return <>
    <button type="button" onClick={() => void logout()}>end session</button>
    {logoutError ? <p role="alert">{logoutError}</p> : null}
  </>;
}

function renderBoundary(child: JSX.Element = <Location />): void {
  render(
    <BrowserRouter>
      <AuthenticationProvider>{child}</AuthenticationProvider>
    </BrowserRouter>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState({}, '', '/');
});

describe('authenticated boot boundary', () => {
  it('does not mount application routes before session state and renders login on 401', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => unauthenticated()));
    renderBoundary();
    expect(screen.queryByText(/^shell:/)).not.toBeInTheDocument();
    expect(
      await screen.findByRole('heading', { name: 'Open the operator console' }),
    ).toBeInTheDocument();
  });

  it('exchanges the master once, clears it, and restores the deep link', async () => {
    window.history.replaceState({}, '', '/operations/runs/run_1');
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(unauthenticated())
      .mockResolvedValueOnce(response(SESSION));
    vi.stubGlobal('fetch', fetchMock);
    const storageSet = vi.spyOn(Storage.prototype, 'setItem');
    renderBoundary();

    const field = await screen.findByLabelText('Master credential');
    await userEvent.type(field, 'generated-master-value');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(field).toHaveValue('');
    expect(await screen.findByText('shell:/operations/runs/run_1')).toBeInTheDocument();
    const init = fetchMock.mock.calls[1]?.[1];
    expect(new Headers(init?.headers).get('authorization')).toBe(
      'Bearer generated-master-value',
    );
    expect(init?.credentials).toBe('same-origin');
    expect(storageSet).not.toHaveBeenCalled();
  });

  it('shows login failure without retaining the entered credential', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => unauthenticated()));
    renderBoundary();
    const field = await screen.findByLabelText('Master credential');
    await userEvent.type(field, 'wrong-master');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('not accepted');
    expect(field).toHaveValue('');
  });

  it('mounts from a valid session and returns to login after a later 401', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(SESSION))
      .mockResolvedValueOnce(unauthenticated());
    vi.stubGlobal('fetch', fetchMock);
    renderBoundary();
    expect(await screen.findByText('shell:/')).toBeInTheDocument();
    await act(async () => { await request('/status').catch(() => undefined); });
    expect(
      await screen.findByRole('heading', { name: 'Open the operator console' }),
    ).toBeInTheDocument();
  });

  it('logs out through the cookie session and returns to login', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(SESSION))
      .mockResolvedValueOnce(response(null, 204));
    vi.stubGlobal('fetch', fetchMock);
    renderBoundary(<Logout />);
    await userEvent.click(await screen.findByRole('button', { name: 'end session' }));
    expect(
      await screen.findByRole('heading', { name: 'Open the operator console' }),
    ).toBeInTheDocument();
    expect(fetchMock.mock.calls[1]?.[1]?.method).toBe('DELETE');
    expect(fetchMock.mock.calls[1]?.[1]?.credentials).toBe('same-origin');
  });

  it('treats an already-invalid session as signed out', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(SESSION))
      .mockResolvedValueOnce(unauthenticated());
    vi.stubGlobal('fetch', fetchMock);
    renderBoundary(<Logout />);
    await userEvent.click(await screen.findByRole('button', { name: 'end session' }));
    expect(
      await screen.findByRole('heading', { name: 'Open the operator console' }),
    ).toBeInTheDocument();
  });

  it('keeps the authenticated shell truthful when logout cannot reach the backend', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(SESSION))
      .mockRejectedValueOnce(new TypeError('offline'));
    vi.stubGlobal('fetch', fetchMock);
    renderBoundary(<Logout />);
    const button = await screen.findByRole('button', { name: 'end session' });
    await userEvent.click(button);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /session may still be active.*offline/i,
    );
    expect(button).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Open the operator console' })).not.toBeInTheDocument();
  });

  it.each([
    [403, 'origin_not_allowed', 'the Origin is not accepted'],
    [503, 'backend_unavailable', 'the backend is unavailable'],
  ])('keeps the authenticated shell truthful after logout HTTP %s', async (status, code, message) => {
    const failure = response({ error: { code, message, detail: {} } }, status);
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(SESSION))
      .mockResolvedValueOnce(failure);
    vi.stubGlobal('fetch', fetchMock);
    renderBoundary(<Logout />);
    const button = await screen.findByRole('button', { name: 'end session' });
    await userEvent.click(button);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      new RegExp('session may still be active.*' + message, 'i'),
    );
    expect(button).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Open the operator console' })).not.toBeInTheDocument();
  });

  it('keeps the application unmounted when the backend is unavailable', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('offline'); }));
    renderBoundary();
    expect(
      await screen.findByRole('heading', { name: 'LocalPlane is unavailable' }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^shell:/)).not.toBeInTheDocument();
  });

  it('does not introduce frontend Change Engine write controls', () => {
    expect('createRun' in endpoints).toBe(false);
    expect('confirmRun' in endpoints).toBe(false);
    expect('applyRun' in endpoints).toBe(false);
    expect('retryRecovery' in endpoints).toBe(false);
  });
});
