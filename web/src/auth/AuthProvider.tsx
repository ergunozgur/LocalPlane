import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { ApiError, setAuthenticationFailureHandler } from '@/api/client';
import { endpoints } from '@/api/endpoints';
import type { SessionStatus } from '@/api/types';
import styles from './AuthProvider.module.css';

type AuthState = { status: 'checking' } | { status: 'unauthenticated'; message: string | null } | { status: 'authenticated'; session: SessionStatus; logoutError: string | null } | { status: 'unavailable'; message: string };
interface AuthenticationContextValue { readonly session: SessionStatus; readonly logoutError: string | null; logout: () => Promise<void>; }
const AuthenticationContext = createContext<AuthenticationContextValue | null>(null);

// eslint-disable-next-line react-refresh/only-export-components -- colocated public hook for this provider.
export function useAuthentication(): AuthenticationContextValue {
  const value = useContext(AuthenticationContext);
  if (value === null) throw new Error('authentication context is unavailable');
  return value;
}

export function AuthenticationProvider({ children }: { children: ReactNode }): JSX.Element {
  const [state, setState] = useState<AuthState>({ status: 'checking' });
  const mounted = useRef(true);
  const expire = useCallback(() => { if (mounted.current) setState({ status: 'unauthenticated', message: 'Your session ended. Sign in again.' }); }, []);
  const check = useCallback(async () => {
    setState({ status: 'checking' });
    try {
      const session = await endpoints.session();
      if (mounted.current) setState({ status: 'authenticated', session, logoutError: null });
    } catch (error) {
      if (!mounted.current) return;
      if (error instanceof ApiError && error.status === 401) setState({ status: 'unauthenticated', message: null });
      else setState({ status: 'unavailable', message: error instanceof Error ? error.message : 'The backend could not be reached.' });
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    setAuthenticationFailureHandler(expire);
    void check();
    return () => { mounted.current = false; setAuthenticationFailureHandler(null); };
  }, [check, expire]);
  const login = useCallback(async (credential: string) => {
    try {
      const session = await endpoints.login(credential);
      if (mounted.current) setState({ status: 'authenticated', session, logoutError: null });
    } catch (error) {
      if (!mounted.current) return;
      setState({ status: 'unauthenticated', message: error instanceof ApiError && error.status === 401 ? 'That credential was not accepted.' : error instanceof Error ? error.message : 'Sign in failed.' });
    }
  }, []);
  const logout = useCallback(async () => {
    if (mounted.current) {
      setState((current) => current.status === 'authenticated' ? { ...current, logoutError: null } : current);
    }
    try {
      await endpoints.logout();
      if (mounted.current) setState({ status: 'unauthenticated', message: null });
    } catch (error) {
      if (!mounted.current) return;
      if (error instanceof ApiError && error.status === 401) {
        setState({ status: 'unauthenticated', message: null });
        return;
      }
      const reason = error instanceof Error ? error.message : 'the backend did not confirm logout';
      setState((current) => current.status === 'authenticated'
        ? { ...current, logoutError: 'Sign out failed. This browser session may still be active: ' + reason }
        : current);
    }
  }, []);
  const value = useMemo<AuthenticationContextValue | null>(() => state.status === 'authenticated' ? { session: state.session, logoutError: state.logoutError, logout } : null, [logout, state]);
  if (state.status === 'checking') return <BoundaryMessage title="LocalPlane" body="Checking this session…" />;
  if (state.status === 'unavailable') return <BoundaryMessage title="LocalPlane is unavailable" body={state.message} action="Try again" onAction={() => void check()} />;
  if (state.status === 'unauthenticated') return <Login message={state.message} onLogin={login} />;
  return <AuthenticationContext.Provider value={value}>{children}</AuthenticationContext.Provider>;
}

function Login({ message, onLogin }: { message: string | null; onLogin: (credential: string) => Promise<void> }): JSX.Element {
  const [credential, setCredential] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const submit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (!credential || submitting) return;
    setSubmitting(true);
    const oneRequestCredential = credential;
    setCredential('');
    try { await onLogin(oneRequestCredential); } finally { setSubmitting(false); }
  };
  return <main className={styles.boundary}><section className={styles.card} aria-labelledby="login-title">
    <div className={styles.wordmark}>Local<i>Plane</i></div>
    <h1 id="login-title">Open the operator console</h1>
    <p className={styles.annotation}>Enter the local master credential. It is exchanged once for a 12-hour browser session and is not retained by this console.</p>
    <form onSubmit={(event) => void submit(event)}>
      <label htmlFor="master-credential">Master credential</label>
      <input id="master-credential" type="password" autoComplete="off" spellCheck={false} value={credential} onChange={(event) => setCredential(event.target.value)} disabled={submitting} autoFocus />
      {message ? <p className={styles.error} role="alert">{message}</p> : null}
      <button type="submit" disabled={!credential || submitting}>{submitting ? 'Signing in…' : 'Sign in'}</button>
    </form>
    <p className={styles.limit}>Loopback development only · no users or roles</p>
  </section></main>;
}

function BoundaryMessage({ title, body, action, onAction }: { title: string; body: string; action?: string; onAction?: () => void }): JSX.Element {
  return <main className={styles.boundary}><section className={styles.card}>
    <div className={styles.wordmark}>Local<i>Plane</i></div><h1>{title}</h1><p className={styles.annotation}>{body}</p>
    {action && onAction ? <button type="button" onClick={onAction}>{action}</button> : null}
  </section></main>;
}
