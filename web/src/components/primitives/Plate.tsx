/**
 * A plate: the surface panel, with the head and foot chrome that give it its character.
 *
 * One hairline rule, an 8 px radius and a flat surface — not a large rounded card. The head
 * is the part that carries the product's voice, and its composition is:
 *
 *   [mark] Title            meta                      as-of   [actions]
 *    9px   serif 15.5       serif italic 13           mono 10.5
 *
 * and the foot names the evidence the panel rests on. `lead` promotes the title to 20 px for
 * the one plate a view is built around.
 */
import type { ReactNode } from 'react';
import type { Semantic } from '@/domain/vocabulary';
import styles from './Plate.module.css';

export function Plate({
  children,
  className,
  as: Element = 'section',
  tone,
  lead = false,
  quiet = false,
  style,
  ...rest
}: {
  children: ReactNode;
  className?: string | undefined;
  as?: 'section' | 'div' | 'article';
  /** An accent along the left edge, used only where a state genuinely needs the emphasis. */
  tone?: 'attention' | 'warn' | 'good' | undefined;
  /** The plate a view is built around. Larger title, a touch more presence. */
  lead?: boolean;
  /** A supporting plate: the table header sits on the page ground rather than the surface. */
  quiet?: boolean;
  style?: React.CSSProperties | undefined;
} & { 'aria-label'?: string; 'aria-labelledby'?: string }): JSX.Element {
  return (
    <Element
      className={[
        styles.plate,
        tone ? styles[tone] : '',
        lead ? styles.lead : '',
        quiet ? styles.quiet : '',
        className ?? '',
      ]
        .filter(Boolean)
        .join(' ')}
      {...(style ? { style } : {})}
      {...rest}
    >
      {children}
    </Element>
  );
}

/**
 * A plate's head.
 *
 * `mark` is the 9 px status dot before a title — a glance-level read that the accessible
 * name still spells out, so it is never colour alone. `asOf` is the freshness stamp that
 * appears on every widget; it is the difference between a number and a number somebody
 * read at a particular moment.
 */
export function PlateHead({
  title,
  titleId,
  meta,
  mark,
  asOf,
  chips,
  children,
  level = 2,
}: {
  title: ReactNode;
  titleId?: string;
  meta?: ReactNode;
  mark?: Semantic | undefined;
  asOf?: string | null | undefined;
  /** State chips, which belong beside the subject rather than out at the controls. */
  chips?: ReactNode;
  children?: ReactNode;
  level?: 2 | 3;
}): JSX.Element {
  const Heading = level === 2 ? 'h2' : 'h3';
  return (
    <div className={styles.head}>
      {mark ? <StatusDot semantic={mark} /> : null}
      <Heading className={styles.title} {...(titleId ? { id: titleId } : {})}>
        {title}
      </Heading>
      {meta ? <span className={styles.meta}>{meta}</span> : null}
      {chips ? <span className={styles.chips}>{chips}</span> : null}
      <span className={styles.spacer} />
      {asOf ? (
        <span className={styles.asOf} title="when this was read">
          {asOf}
        </span>
      ) : null}
      {children ? <span className={styles.actions}>{children}</span> : null}
    </div>
  );
}

/**
 * The 9 px status dot.
 *
 * Filled for a settled state, a hollow ring for inactive, and a *dotted* ring for unknown —
 * the same rule the pills follow, so a shape carries the meaning when colour cannot.
 */
export function StatusDot({ semantic }: { semantic: Semantic }): JSX.Element {
  return (
    <span
      className={`${styles.mark} ${styles[`mark_${semantic.tone}`] ?? ''}`}
      role="img"
      aria-label={`${semantic.label} — ${semantic.description}`}
      title={`${semantic.label} — ${semantic.description}`}
    />
  );
}

/**
 * A ruled sub-heading inside a plate.
 *
 * Serif, 14 px, dim. Deliberately *not* the uppercase micro-label used
 * for field names: inside a plate the editorial voice separates groups, and the uppercase
 * treatment is reserved for the band headings that separate sections of the page.
 */
export function PlateSection({
  title,
  children,
  actions,
}: {
  title: ReactNode;
  children: ReactNode;
  actions?: ReactNode;
}): JSX.Element {
  return (
    <>
      <div className={styles.sectionHead}>
        <span className={styles.sectionTitle}>{title}</span>
        {actions ? (
          <>
            <span className={styles.spacer} />
            {actions}
          </>
        ) : null}
      </div>
      <div className={styles.sectionBody}>{children}</div>
    </>
  );
}

export function PlateBody({
  children,
  className,
  tight = false,
}: {
  children: ReactNode;
  className?: string;
  tight?: boolean;
}): JSX.Element {
  return (
    <div className={[styles.body, tight ? styles.tight : '', className ?? ''].filter(Boolean).join(' ')}>
      {children}
    </div>
  );
}

/**
 * The evidence footer.
 *
 * A panel ends by naming what it rests on, and that habit is most of what separates
 * this product from a dashboard: a number without a source is decoration. `source` is the
 * provider and method the backend reported, never a phrase this frontend composed about
 * how reliable something is.
 */
export function PlateFoot({
  source,
  children,
  label = 'evidence',
}: {
  source?: ReactNode;
  children?: ReactNode;
  label?: string;
}): JSX.Element {
  return (
    <div className={styles.foot}>
      {source ? (
        <>
          <span className={styles.evidenceKey}>{label}</span>
          <span className={styles.evidenceSource}>{source}</span>
        </>
      ) : null}
      {children}
    </div>
  );
}
