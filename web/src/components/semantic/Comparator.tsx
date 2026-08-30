/**
 * Observed ‖ = ‖ Intended.
 *
 * The `.dual` grid, and the most distinctive structure in the console: two columns of
 * values with a narrow operator column between them carrying `=`, `≠` or nothing. The point
 * is that the *relation* is a first-class cell rather than a status word tucked in a fourth
 * table column — the eye reads down the middle and finds the disagreements.
 *
 * Three relations, and the third is load-bearing:
 *
 *   eq   `=`   both sides were read and they agree
 *   ne   `≠`   both sides were read and they disagree
 *   na   blank one side could not be read, so no comparison was made
 *
 * `na` renders as an empty operator on purpose. Any glyph there — a dash, a question mark
 * in the same weight as the others — reads as a verdict, and "not compared" is not a verdict.
 * The unreadable side says so in words in its own cell instead.
 *
 * Nothing in this component decides a relation. Callers pass what the backend computed.
 */
import type { ReactNode } from 'react';
import styles from './Comparator.module.css';

export type Relation = 'eq' | 'ne' | 'na';

export interface ComparatorRow {
  field: string;
  observed: ReactNode;
  intended: ReactNode;
  relation: Relation;
  /** Marks the row as the drift itself, not merely a difference in passing. */
  drift?: boolean;
}

const OPERATOR: Readonly<Record<Relation, string>> = { eq: '=', ne: '≠', na: '' };

export function Comparator({
  rows,
  observedLabel = 'Observed',
  intendedLabel = 'Intended',
  /** Where the intended side comes from, set in mono beside the column heading. */
  source,
}: {
  rows: readonly ComparatorRow[];
  observedLabel?: string;
  intendedLabel?: string;
  source?: string | undefined;
}): JSX.Element {
  return (
    <div className={styles.dual} role="table" aria-label={`${observedLabel} against ${intendedLabel}`}>
      <div className={styles.head} role="columnheader">
        {observedLabel}
      </div>
      <div className={`${styles.head} ${styles.mid}`} role="columnheader">
        <span className={styles.srOnly}>Relation</span>
      </div>
      <div className={`${styles.head} ${styles.right}`} role="columnheader">
        {intendedLabel}
        {source ? <em>{source}</em> : null}
      </div>

      {rows.map((row, index) => (
        <div
          key={row.field}
          role="row"
          className={[
            styles.row,
            row.drift ? styles.isDrift : '',
            index === rows.length - 1 ? styles.last : '',
          ]
            .filter(Boolean)
            .join(' ')}
        >
          <div className={styles.cell} role="cell">
            <span className={styles.key}>{row.field}</span>
            <span className={`${styles.value} ${row.drift ? styles.drift : ''}`}>{row.observed}</span>
          </div>
          <div
            className={`${styles.op} ${styles[row.relation]}`}
            role="cell"
            aria-label={
              row.relation === 'eq'
                ? 'matches'
                : row.relation === 'ne'
                  ? 'differs'
                  : 'not compared'
            }
          >
            {OPERATOR[row.relation]}
          </div>
          <div className={`${styles.cell} ${styles.right}`} role="cell">
            <span className={styles.value}>{row.intended}</span>
          </div>
        </div>
      ))}
    </div>
  );
}
