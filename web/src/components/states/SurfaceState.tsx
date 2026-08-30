/**
 * The states a surface is in when it is not showing data.
 *
 * Designed rather than defaulted, because these are most of what an operator sees when
 * something is wrong, and a generic toast would throw away the one thing that matters: which
 * kind of nothing this is. An empty list because Docker is unreadable and an empty list
 * because there are no containers are different facts and get different screens.
 */
import type { ReactNode } from 'react';
import { ApiError } from '@/api/client';
import styles from './SurfaceState.module.css';

/** A quiet placeholder that holds the layout while a first read is in flight. */
export function Loading({ label = 'Reading…' }: { label?: string }): JSX.Element {
  return (
    <div className={styles.state} role="status" aria-live="polite">
      <span className={styles.pulse} aria-hidden="true" />
      <span className={styles.loadingText}>{label}</span>
    </div>
  );
}

/**
 * Nothing here — and a sentence saying what that means.
 *
 * `explanation` is not decoration. "No containers" and "nothing is managed, so nothing can
 * drift" are the difference between an operator moving on and an operator investigating.
 */
export function Empty({
  title,
  explanation,
  children,
}: {
  title: string;
  explanation?: ReactNode;
  children?: ReactNode;
}): JSX.Element {
  return (
    <div className={styles.state}>
      <p className={styles.emptyTitle}>{title}</p>
      {explanation ? <p className={styles.emptyBody}>{explanation}</p> : null}
      {children}
    </div>
  );
}

/**
 * A request did not produce a body.
 *
 * The five `ApiError` kinds get five different sentences, because the remedies differ: start
 * the backend, wait, look at the agent, report a bug. The stable error code is always shown
 * — that is the string an operator can search for and quote.
 */
export function Failed({
  error,
  onRetry,
  what = 'record',
}: {
  error: ApiError;
  onRetry?: () => void;
  /** The noun a 404 is about, so the sentence reads as English. */
  what?: string;
}): JSX.Element {
  const { headline, guidance } = describe(error, what);
  return (
    <div className={`${styles.state} ${styles.failed}`} role="alert">
      <p className={styles.failedTitle}>{headline}</p>
      <p className={styles.emptyBody}>{guidance}</p>
      <p className={styles.detail}>
        <code>{error.code ?? error.kind}</code>
        {error.status !== null ? <span className={styles.status}>HTTP {error.status}</span> : null}
        <span className={styles.path}>{error.path}</span>
      </p>
      {error.message ? <p className={styles.message}>{error.message}</p> : null}
      {onRetry && error.retryable ? (
        <button type="button" className={styles.retry} onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}

function describe(error: ApiError, what: string): { headline: string; guidance: string } {
  switch (error.kind) {
    case 'unreachable':
      return {
        headline: 'The backend could not be reached',
        guidance:
          'Nothing is known about the host from here — this is a statement about this browser’s connection to LocalPlane, not about the host itself.',
      };
    case 'timeout':
      return {
        headline: 'The backend did not answer in time',
        guidance:
          'The request was abandoned. Whatever it was reading may still be in progress on the host.',
      };
    case 'malformed':
      return {
        headline: 'The response could not be read',
        guidance:
          'The backend answered with something this build does not understand. Nothing has been assumed from it.',
      };
    case 'cancelled':
      return { headline: 'The request was cancelled', guidance: 'Nothing was read.' };
    case 'backend':
    default:
      if (error.notFound) {
        return {
          headline: `No such ${what}`,
          guidance: 'The backend has no record of it. It may have been removed, or never observed.',
        };
      }
      return {
        headline: 'The backend refused this request',
        guidance:
          'It answered with a typed error rather than data. The code below is what to search for.',
      };
  }
}

/**
 * Data is shown, but something about it is limited.
 *
 * Never used to soften an error and never used where a value is simply unknown — this is for
 * a surface that is working with a caveat: a partial sweep, a stale observation, an
 * unavailable capability.
 */
export function Degraded({
  title,
  children,
  tone = 'warn',
}: {
  title: ReactNode;
  children?: ReactNode;
  tone?: 'warn' | 'unknown';
}): JSX.Element {
  return (
    <div className={`${styles.degraded} ${tone === 'unknown' ? styles.degradedUnknown : ''}`}>
      <div className={styles.degradedTitle}>{title}</div>
      {children ? <div className={styles.degradedBody}>{children}</div> : null}
    </div>
  );
}

/**
 * A question this build did not ask.
 *
 * Distinct from `Empty` and from `Failed`: nothing failed and nothing is absent — the
 * assessment simply has not been made. Used for execution status where no plan exists.
 */
export function NotAssessed({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}): JSX.Element {
  return (
    <div className={styles.notAssessed}>
      <div className={styles.notAssessedHead}>
        <span className={styles.notAssessedGlyph} aria-hidden="true">
          ?
        </span>
        <span>{title}</span>
      </div>
      {children ? <div className={styles.degradedBody}>{children}</div> : null}
    </div>
  );
}
