/**
 * The three marker shapes, kept apart on purpose.
 *
 * Each semantic axis has its own shape, and the distinction is stated where the shapes are
 * defined — a square, deliberately unlike the health dot. Health is a
 * circle, management is a square, reconciliation is a mono operator (`=` `≠` `≈` `?`). An
 * operator reading a row can therefore tell the three answers apart before reading a word,
 * which one shared pill shape cannot do.
 *
 * Colour never carries the meaning alone: every glyph is paired with a word (or, in a dense
 * table, an accessible name that supplies it).
 */
import type { Semantic } from '@/domain/vocabulary';
import { management as managementOf, reconciliation as reconciliationOf } from '@/domain/vocabulary';
import styles from './SemanticGlyph.module.css';

/** Health — a filled circle. `inactive` is a ring; `unknown` is a dotted ring. */
export function HealthMark({ semantic }: { semantic: Semantic }): JSX.Element {
  return (
    <span
      className={`${styles.health} ${styles[semantic.tone] ?? ''}`}
      role="img"
      aria-label={`${semantic.label} — ${semantic.description}`}
      title={`${semantic.label} — ${semantic.description}`}
    />
  );
}

/**
 * Management — a square, deliberately unlike the health dot.
 *
 * The three levels are the backend's own: `managed`, `observed`, `observe_only`.
 */
export function ManagementChip({
  state,
  size = 'md',
}: {
  state: string | null | undefined;
  size?: 'sm' | 'md';
}): JSX.Element {
  const semantic = managementOf(state);
  return (
    <span
      className={`${styles.chip} ${styles.management} ${styles[`m_${state ?? 'unknown'}`] ?? ''} ${styles[size]}`}
      title={semantic.description}
    >
      <i className={styles.square} aria-hidden="true" />
      {semantic.label}
    </span>
  );
}

/** The operator a reconciliation state reads as. `unknown` is `?`, never `=`. */
const OPERATOR: Readonly<Record<string, string>> = {
  in_sync: '=',
  drifted: '≠',
  applying: '≈',
  unknown: '?',
};

/**
 * The operator for a state, totally.
 *
 * Three cases and they are not the same. **No state at all** gets no operator: an observed
 * object retains no intent, so there is nothing for it to differ from and any glyph would be
 * a claim nobody made. A **recognised** state gets its own operator. An **unrecognised** one
 * gets `?` — a token this build does not know is an unknown, and it must never fall through
 * to nothing, where it would look exactly like "not tracked".
 */
function operatorFor(state: string | null | undefined): string | undefined {
  if (state === null || state === undefined || state === '') return undefined;
  return OPERATOR[state] ?? '?';
}

/**
 * Reconciliation — a mono operator.
 *
 * Only ever shown for a managed object: an observed object has no retained intent, so there
 * is nothing for it to differ from, and rendering `=` there would be a claim nobody made.
 */
export function ReconciliationChip({
  state,
  size = 'md',
}: {
  state: string | null | undefined;
  size?: 'sm' | 'md';
}): JSX.Element {
  const semantic = reconciliationOf(state);
  const operator = operatorFor(state);
  return (
    <span
      className={`${styles.chip} ${styles.reconciliation} ${styles[`r_${state ?? 'none'}`] ?? ''} ${styles[size]}`}
      title={semantic.description}
    >
      {operator ? (
        <i className={styles.operator} aria-hidden="true">
          {operator}
        </i>
      ) : null}
      {semantic.label}
    </span>
  );
}

/**
 * A confidence ladder.
 *
 * Confidence is drawn as three rising bars. The backend publishes real confidence on
 * identity (`high`) and on ownership claims (`confirmed` / `corroborated`), so this renders
 * a fact rather than a mood. An unrecognised value lights no bars.
 */
export function ConfidenceLadder({ level }: { level: string | null | undefined }): JSX.Element {
  const rank =
    level === 'high' || level === 'confirmed'
      ? 'high'
      : level === 'medium' || level === 'corroborated'
        ? 'medium'
        : level === 'low'
          ? 'low'
          : 'none';
  return (
    <span
      className={`${styles.confidence} ${styles[`c_${rank}`] ?? ''}`}
      role="img"
      aria-label={level ? `confidence: ${level}` : 'confidence not stated'}
      title={level ? `confidence: ${level}` : 'confidence not stated'}
    >
      <i />
      <i />
      <i />
    </span>
  );
}
