/**
 * The attention rail — the first element on the home view.
 *
 * A slim full-width strip above the grid, not a panel inside it. That placement is the
 * point: what needs looking at is read before the machine is, and a rail that has nothing to
 * say collapses to one quiet line rather than occupying a card.
 *
 * The 2 px left border keyed to state (oxide = drift, amber = findings, verdigris = quiet)
 * and the pill vocabulary come from the design direction. What differs is the flow: the
 * design queues its pills in a single drag-scrolled lane, which on a wide display spends
 * the extra width on the gap between segments while the items stay behind a scrollbar.
 * Here the two segments share the width and their pills wrap into it, so a wide viewport
 * shows more rather than
 * scrolling more.
 *
 * The quiet line states what was checked. "Nothing needs attention" without that is a
 * reassurance; with it, it is a claim an operator can weigh.
 */
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import styles from './AttentionRail.module.css';

export interface AttentionItem {
  id: string;
  name: string;
  detail: string;
  to?: string | undefined;
}

export function AttentionRail({
  drifted,
  findings,
  quietSummary,
  unresolved = false,
  reviewTo = '/operations',
}: {
  drifted: readonly AttentionItem[];
  findings: readonly AttentionItem[];
  /** What was actually checked, when nothing needs attention. Never omitted. */
  quietSummary: ReactNode;
  /** The reads behind this could not be made. Silence would read as "all clear". */
  unresolved?: boolean;
  reviewTo?: string;
}): JSX.Element {
  // A rail that disappears when its reads fail is a rail that says "nothing needs attention"
  // by omission — the one thing it must never say without having looked.
  if (unresolved) {
    return (
      <div className={styles.rail} data-state="unknown" aria-label="Attention">
        <span className={styles.unknownMark} aria-hidden="true" />
        <b className={styles.quietTitle}>Attention could not be assessed</b>
        <span className={styles.quietSummary}>
          Drift and findings could not be read. This is not a statement that nothing needs
          attention.
        </span>
      </div>
    );
  }

  const state = drifted.length > 0 ? 'drift' : findings.length > 0 ? 'finding' : 'quiet';

  if (state === 'quiet') {
    return (
      <div className={styles.rail} data-state="quiet" aria-label="Attention">
        <span className={styles.quietMark} aria-hidden="true" />
        <b className={styles.quietTitle}>Nothing needs attention</b>
        <span className={styles.quietSummary}>{quietSummary}</span>
        <Link to={reviewTo} className={styles.end}>
          History ›
        </Link>
      </div>
    );
  }

  return (
    <div className={styles.rail} data-state={state} aria-label="Attention">
      <div className={styles.segments}>
        <Segment kind="drift" count={drifted.length} word="drifted" items={drifted} />
        <Segment kind="finding" count={findings.length} word="findings" items={findings} />
      </div>
      <Link to={reviewTo} className={styles.end}>
        Review ›
      </Link>
    </div>
  );
}

function Segment({
  kind,
  count,
  word,
  items,
}: {
  kind: 'drift' | 'finding';
  count: number;
  word: string;
  items: readonly AttentionItem[];
}): JSX.Element | null {
  if (count === 0) {
    return (
      <div className={styles.segment}>
        <span className={styles.headline} data-empty="true">
          <span className={styles.count}>0</span>
          <span className={styles.word}>{word}</span>
        </span>
      </div>
    );
  }
  return (
    <div className={styles.segment}>
      <span className={styles.headline} data-kind={kind}>
        <span className={styles.count}>{count}</span>
        <span className={styles.word}>{word}</span>
      </span>
      <div className={styles.items}>
        {items.map((item) =>
          item.to ? (
            <Link key={item.id} to={item.to} className={`${styles.item} ${styles[kind]}`}>
              <span className={styles.itemName}>{item.name}</span>
              <span className={styles.itemDetail}>{item.detail}</span>
            </Link>
          ) : (
            <span key={item.id} className={`${styles.item} ${styles[kind]}`}>
              <span className={styles.itemName}>{item.name}</span>
              <span className={styles.itemDetail}>{item.detail}</span>
            </span>
          ),
        )}
      </div>
    </div>
  );
}
