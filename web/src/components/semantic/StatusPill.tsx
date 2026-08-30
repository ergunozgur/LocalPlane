/**
 * A semantic state, rendered so that its meaning survives without colour.
 *
 * Every pill carries three things: a glyph, a word, and a tone. The glyph and the word are
 * the message; the tone is emphasis. That ordering is deliberate — a colour-blind operator,
 * a greyscale screenshot in a ticket and a screen reader all get the same answer.
 *
 * `unknown` has a glyph of its own (`?`) and a dashed outline, so it cannot be mistaken for
 * `good` at a glance or for `bad` on a monochrome display. A missing fact and a negative
 * fact must never look alike.
 */
import type { Semantic, SemanticTone } from '@/domain/vocabulary';
import styles from './StatusPill.module.css';

const GLYPH: Record<SemanticTone, string> = {
  good: '✓',
  warn: '!',
  bad: '✕',
  attention: '≠',
  unknown: '?',
  neutral: '·',
};

/** Spoken prefix, so the tone is not conveyed to assistive technology by colour alone. */
const SPOKEN: Record<SemanticTone, string> = {
  good: 'ok',
  warn: 'warning',
  bad: 'problem',
  attention: 'needs attention',
  unknown: 'not known',
  neutral: 'neutral',
};

export function StatusPill({
  semantic,
  size = 'md',
  /** The backend token, shown in mono beside the word. This is what an operator searches for. */
  token,
  className,
}: {
  semantic: Semantic;
  size?: 'sm' | 'md';
  token?: string | null | undefined;
  className?: string | undefined;
}): JSX.Element {
  return (
    <span
      className={[styles.pill, styles[semantic.tone], styles[size], className ?? '']
        .filter(Boolean)
        .join(' ')}
      title={semantic.description}
    >
      <span className={styles.glyph} aria-hidden="true">
        {GLYPH[semantic.tone]}
      </span>
      <span className="visually-hidden">{SPOKEN[semantic.tone]}: </span>
      <span className={styles.label}>{semantic.label}</span>
      {token && token !== semantic.label ? (
        <span className={styles.token}>{token}</span>
      ) : null}
    </span>
  );
}

/**
 * A bare mark, for table cells where a full pill would be noise.
 *
 * Still never colour alone: the glyph carries the shape and the accessible name carries the
 * word.
 */
export function StatusMark({ semantic }: { semantic: Semantic }): JSX.Element {
  return (
    <span
      className={[styles.mark, styles[semantic.tone]].join(' ')}
      title={`${semantic.label} — ${semantic.description}`}
      role="img"
      aria-label={`${SPOKEN[semantic.tone]}: ${semantic.label}`}
    >
      <span aria-hidden="true">{GLYPH[semantic.tone]}</span>
    </span>
  );
}
