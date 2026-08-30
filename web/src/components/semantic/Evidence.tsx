/**
 * Evidence, disclosed progressively.
 *
 * LocalPlane's backend publishes a great deal of evidence, and dumping it as JSON would be
 * the laziest possible reading of "show your working". The order below is the order an
 * operator actually asks in, and it is the order the API already supplies:
 *
 *   conclusion   what LocalPlane concluded
 *   why          the typed reason it concluded it
 *   authority    which source is entitled to say so
 *   evidence     what was consulted, and what each source answered
 *   gaps         what would have settled it and did not
 *
 * A gap is rendered as prominently as a finding. "Nobody could tell us" is a result, and
 * hiding it behind a chevron would make an incomplete answer look complete.
 */
import { type ReactNode } from 'react';
import type { Semantic } from '@/domain/vocabulary';
import { humanise } from '@/domain/vocabulary';
import { StatusPill } from './StatusPill';
import { Tag } from '@/components/primitives/Chip';
import styles from './Evidence.module.css';

export function Conclusion({
  semantic,
  token,
  why,
  children,
}: {
  semantic: Semantic;
  token?: string | null;
  /** The backend's typed reason code, rendered as a sentence with the token beside it. */
  why?: string | null;
  children?: ReactNode;
}): JSX.Element {
  return (
    <div className={styles.conclusion}>
      <div className={styles.conclusionHead}>
        <StatusPill semantic={semantic} {...(token ? { token } : {})} />
        {why ? (
          <span className={styles.why}>
            {humanise(why)} <code className={styles.code}>{why}</code>
          </span>
        ) : null}
      </div>
      <p className={styles.description}>{semantic.description}</p>
      {children}
    </div>
  );
}

/**
 * Named gaps.
 *
 * Always visible when non-empty, never collapsed. The whole point of the list is that the
 * absence is legible.
 */
export function Gaps({
  items,
  label = 'Missing evidence',
  /** Long lists (systemd effect graphs run to dozens) are capped with an honest remainder. */
  limit = 12,
}: {
  items: readonly string[];
  label?: string;
  limit?: number;
}): JSX.Element | null {
  if (items.length === 0) return null;
  const shown = items.slice(0, limit);
  const remainder = items.length - shown.length;
  return (
    <div className={styles.gaps}>
      <div className={`${styles.gapsLabel} label`}>
        {label} · {items.length}
      </div>
      <ul className={styles.gapList}>
        {shown.map((item) => (
          <li key={item}>
            <Tag title={item}>{item}</Tag>
          </li>
        ))}
        {remainder > 0 ? (
          <li className={styles.remainder}>and {remainder} more</li>
        ) : null}
      </ul>
    </div>
  );
}

/**
 * A collapsed drill-down.
 *
 * `<details>` rather than a hover popover: it works on touch, it works from the keyboard,
 * and it can hold focusable content. Nothing safety-critical is placed inside one —
 * disclosure is for depth, not for the answer.
 */
export function Disclosure({
  summary,
  children,
  count,
  defaultOpen = false,
}: {
  summary: ReactNode;
  children: ReactNode;
  count?: number;
  defaultOpen?: boolean;
}): JSX.Element {
  return (
    <details className={styles.disclosure} open={defaultOpen}>
      <summary className={styles.summary}>
        <span className={styles.chevron} aria-hidden="true">
          ▸
        </span>
        <span className={styles.summaryText}>{summary}</span>
        {count !== undefined ? <span className={styles.count}>{count}</span> : null}
      </summary>
      <div className={styles.disclosureBody}>{children}</div>
    </details>
  );
}

/**
 * Raw evidence, bounded.
 *
 * A secondary developer view, never the primary experience. The body is capped so that a
 * large evidence object cannot turn an operator's screen into a wall of JSON.
 */
export function RawEvidence({ value, maxChars = 20000 }: { value: unknown; maxChars?: number }): JSX.Element {
  let text: string;
  try {
    text = JSON.stringify(value, null, 2) ?? 'null';
  } catch {
    text = '(this evidence could not be rendered as JSON)';
  }
  const truncated = text.length > maxChars;
  return (
    <div>
      <pre className={styles.raw}>{truncated ? `${text.slice(0, maxChars)}\n…` : text}</pre>
      {truncated ? (
        <p className={styles.truncated}>
          Truncated at {maxChars.toLocaleString()} characters. This is a debug view; the
          summarised evidence above is the authoritative reading.
        </p>
      ) : null}
    </div>
  );
}
