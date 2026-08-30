/**
 * The rendering of a fact nobody established.
 *
 * `null` in a LocalPlane response is never a zero, an empty string or a default — the speed
 * of a link with no carrier is `null` because the kernel does not know it. This component is
 * how that stays visible: an em-dash with a dashed underline and an accessible name, which
 * is visibly different from a `0` and from a `false`.
 *
 * Use `<Value>` for anything that may be absent, rather than `value ?? '—'` at the call
 * site. Doing it in one place is what makes the guarantee checkable.
 */
import type { ReactNode } from 'react';
import styles from './UnknownValue.module.css';

export function UnknownValue({
  reason = 'not known',
  className,
}: {
  /** Why it is absent, when the backend says. Shown on hover and to assistive technology. */
  reason?: string | undefined;
  className?: string | undefined;
}): JSX.Element {
  return (
    <span
      className={[styles.unknown, className ?? ''].filter(Boolean).join(' ')}
      title={reason}
      role="img"
      aria-label={reason}
    >
      <span aria-hidden="true">—</span>
    </span>
  );
}

/**
 * Render a value, or an explicit unknown when it is absent.
 *
 * `0` and `false` are values and render as themselves; only `null`, `undefined` and the
 * empty string are absences.
 */
export function Value({
  value,
  mono = false,
  reason,
  children,
}: {
  value?: string | number | null | undefined;
  mono?: boolean;
  reason?: string | undefined;
  children?: ReactNode;
}): JSX.Element {
  const resolved = children ?? value;
  if (resolved === null || resolved === undefined || resolved === '') {
    return <UnknownValue {...(reason ? { reason } : {})} />;
  }
  return mono ? <span className="mono">{resolved}</span> : <>{resolved}</>;
}
