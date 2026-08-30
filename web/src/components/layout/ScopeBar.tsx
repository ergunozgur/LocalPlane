/**
 * The second navigation layer.
 *
 * A strip under the app bar carries `∕ helios ∕ workloads ∕ All workloads 7 ·
 * Deployments 4 · …` on the left and `observed <ts> utc · live · ⟳` on the right. It is the
 * only place a page says where it sits in the product and when what it shows was read, and
 * both are worth having on every surface.
 *
 * Three details that are not decoration:
 *
 *  - The separator is `∕` (U+2215), not a solidus. It sits higher and thinner, which is why a
 *    path of five segments reads as one path rather than as five fragments.
 *  - **The current crumb keeps its whole name.** Earlier segments ellipsise; the thing you
 *    are looking at never does, because that is the one you needed to read.
 *  - The tab strip scrolls, with edge fades and arrows that appear only when there is
 *    something past the edge — never a wrap onto a second line, which would move the whole
 *    page down by a row every time a count grew.
 *
 * **These tabs are not ARIA tabs and are deliberately not marked as such.** They are links
 * that change the route. `role="tab"` promises a panel controlled inside this view, and a
 * screen reader following that promise would be told the page had not navigated when it had.
 * The design direction's tabs are buttons switching a client-side view, so its
 * `role="tablist"` is correct there and would be a lie here. The selection *grammar* is
 * shared; the role is not.
 *
 * Sub-navigation tabs appear only where a real surface exists. Deployments,
 * Templates, Kernel, Packages, Users, Logs and Time have no backend contract here, and a tab
 * that leads nowhere is worse than an absent one.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { NavLink } from 'react-router-dom';
import styles from './ScopeBar.module.css';

export interface ScopeTab {
  to: string;
  label: string;
  count?: number | null | undefined;
  /** The oxide dot on a tab whose surface holds a drifted object. */
  drift?: boolean;
  end?: boolean;
}

export interface Crumb {
  label: string;
  to?: string | undefined;
  /** Identifiers take the mono face wherever they appear, including in a breadcrumb. */
  mono?: boolean;
}

export function ScopeBar({
  crumbs,
  tabs,
  observedAt,
  children,
}: {
  crumbs: readonly Crumb[];
  tabs?: readonly ScopeTab[];
  /** When the data on this page was read. Omitted rather than guessed. */
  observedAt?: Date | null | undefined;
  children?: ReactNode;
}): JSX.Element {
  const stripRef = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState('');

  // Which edges have something past them. This is kept in `data-of`, and both the fades
  // and the arrows are driven from it, so they can never disagree.
  const measure = useCallback(() => {
    const strip = stripRef.current;
    if (!strip) return;
    const left = strip.scrollLeft > 1;
    const right = strip.scrollLeft + strip.clientWidth < strip.scrollWidth - 1;
    setOverflow(`${left ? 'l' : ''}${left && right ? ' ' : ''}${right ? 'r' : ''}`);
  }, []);

  useEffect(() => {
    measure();
    const strip = stripRef.current;
    if (!strip || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => measure());
    observer.observe(strip);
    return () => observer.disconnect();
  }, [measure, tabs]);

  const scroll = (direction: -1 | 1): void => {
    stripRef.current?.scrollBy({ left: direction * 180, behavior: 'smooth' });
  };

  return (
    <div className={styles.bar}>
      <nav className={styles.crumbs} aria-label="Breadcrumb">
        {crumbs.map((crumb, index) => {
          const last = index === crumbs.length - 1;
          return (
            <span key={`${crumb.label}-${index}`} className={styles.crumbWrap}>
              <span className={styles.slash} aria-hidden="true">
                ∕
              </span>
              {crumb.to ? (
                <NavLink
                  to={crumb.to}
                  className={[styles.crumb, crumb.mono ? 'mono' : ''].filter(Boolean).join(' ')}
                >
                  {crumb.label}
                </NavLink>
              ) : (
                <span
                  className={[styles.crumb, last ? styles.current : '', crumb.mono ? 'mono' : '']
                    .filter(Boolean)
                    .join(' ')}
                  aria-current={last ? 'page' : undefined}
                >
                  {crumb.label}
                </span>
              )}
            </span>
          );
        })}
      </nav>

      {tabs && tabs.length > 0 ? (
        <div className={styles.tabRegion} data-of={overflow}>
          <button
            type="button"
            className={`${styles.arrow} ${styles.left}`}
            aria-label="Scroll sections left"
            tabIndex={-1}
            onClick={() => scroll(-1)}
          >
            ‹
          </button>
          <div className={styles.tabs} ref={stripRef} onScroll={measure}>
            {tabs.map((tab) => (
              <NavLink
                key={tab.to}
                to={tab.to}
                end={tab.end ?? false}
                className={({ isActive }) =>
                  [styles.tab, isActive ? styles.tabActive : ''].filter(Boolean).join(' ')
                }
              >
                {tab.label}
                {tab.count === undefined ? null : (
                  <span className={styles.tabCount}>
                    {tab.count === null ? '—' : tab.count.toLocaleString()}
                  </span>
                )}
                {tab.drift ? (
                  <span className={styles.driftDot} title="something on this surface has drifted" />
                ) : null}
              </NavLink>
            ))}
          </div>
          <button
            type="button"
            className={`${styles.arrow} ${styles.right}`}
            aria-label="Scroll sections right"
            tabIndex={-1}
            onClick={() => scroll(1)}
          >
            ›
          </button>
        </div>
      ) : null}

      <span className={styles.spacer} />
      {children}
      {observedAt ? (
        <span className={styles.observed}>read {observedAt.toLocaleTimeString()}</span>
      ) : null}
    </div>
  );
}
