/**
 * A key-value row: a label column and a value column, ruled between rows.
 *
 * The label column is `minmax(96px, auto)` so labels align without a fixed width forcing
 * truncation, and the value column is `minmax(0, 1fr)` so long identifiers wrap or scroll
 * rather than pushing the layout apart.
 */
import type { ReactNode } from 'react';
import styles from './KeyValue.module.css';

export function KeyValueList({
  children,
  columns = 1,
}: {
  children: ReactNode;
  /** `auto` fills the container with as many readable columns as fit. */
  columns?: 1 | 'auto';
}): JSX.Element {
  return (
    <dl className={columns === 'auto' ? styles.listAuto : styles.list}>{children}</dl>
  );
}

export function KeyValue({
  label,
  children,
  hint,
}: {
  label: ReactNode;
  children: ReactNode;
  /** A short qualifier after the value — a unit, a source, a caveat. */
  hint?: ReactNode;
}): JSX.Element {
  return (
    <div className={styles.row}>
      <dt className={styles.key}>{label}</dt>
      <dd className={styles.value}>
        {children}
        {hint ? <span className={styles.hint}>{hint}</span> : null}
      </dd>
    </div>
  );
}
