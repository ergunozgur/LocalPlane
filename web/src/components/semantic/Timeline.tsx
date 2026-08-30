/**
 * The machine record: a vertical timeline of things that happened to one object.
 *
 * The `.tick` — a rail with a marker per entry, a mono timestamp, a sentence, and
 * an italic serif note underneath explaining what the entry means. The marker *shape and
 * colour carry the kind*: a round oxide dot for drift, a square accent dot for a change, a
 * grey dot for a boot, a green dot for a verification, and a hollow ring for an ordinary
 * observation. The footer line is the point of the whole component — *every entry here was
 * written by an observation, not by a person*.
 *
 * Nothing here interprets. A tick renders what the record says happened; it never derives a
 * "probably" from two entries that sit next to each other.
 */
import type { ReactNode } from 'react';
import styles from './Timeline.module.css';

export type TickKind = 'drift' | 'change' | 'boot' | 'verify' | 'observe';

export interface TickEntry {
  id: string;
  kind: TickKind;
  /** The stamp, already formatted. A tick does not decide what a time looks like. */
  at: string;
  what: ReactNode;
  /** Why this entry is in the record — the serif italic line. */
  note?: ReactNode;
}

export function Timeline({ entries }: { entries: readonly TickEntry[] }): JSX.Element {
  return (
    <ol className={styles.timeline}>
      {entries.map((entry) => (
        <li key={entry.id} className={`${styles.tick} ${styles[entry.kind]}`}>
          <div className={styles.at}>{entry.at}</div>
          <div className={styles.what}>{entry.what}</div>
          {entry.note ? <div className={styles.note}>{entry.note}</div> : null}
        </li>
      ))}
    </ol>
  );
}
