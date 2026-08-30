/**
 * Where an object sits, in the machine's voice.
 *
 * This sits inside the object's own plate, below the head and above the tabs, on an
 * inset ground in mono: `helios ▸ workloads ▸ monitoring ▸ grafana ▸ container a7f3… ▸
 * Overview`. It differs from the scope-bar breadcrumb in kind, not degree — the breadcrumb
 * says which *surface* you are on; this says what this *object* is part of, ending in the
 * tab you are reading.
 *
 * Every segment must come from a real relationship. A container's compose project is one; a
 * container without a project gets no invented parent.
 */
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import styles from './Pathline.module.css';

export interface PathSegment {
  label: string;
  to?: string | undefined;
  /** A prefix word rendered in the interface face, e.g. "container" before an id. */
  prefix?: string | undefined;
  current?: boolean;
}

export function Pathline({
  segments,
  fact,
}: {
  segments: readonly PathSegment[];
  fact?: ReactNode;
}): JSX.Element {
  return (
    <div className={styles.pathline}>
      {segments.map((segment, index) => (
        <span key={`${segment.label}-${index}`} className={styles.segment}>
          {index > 0 ? (
            <span className={styles.arrow} aria-hidden="true">
              ▸
            </span>
          ) : null}
          {segment.prefix ? <span className={styles.prefix}>{segment.prefix}</span> : null}
          {segment.to ? (
            <Link to={segment.to} className={styles.link}>
              {segment.label}
            </Link>
          ) : (
            <b className={segment.current ? styles.current : undefined}>{segment.label}</b>
          )}
        </span>
      ))}
      {fact ? <span className={styles.fact}>{fact}</span> : null}
    </div>
  );
}
