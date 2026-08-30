/**
 * Primary navigation.
 *
 * This is a discrete raised pill centred on the bar, in which **one control per
 * domain** carries the label, a drift dot, a count and a chevron, and opens that domain's
 * index. Selecting a destination happens in the menu. That one-control grammar is what makes
 * the bar read as an index of the product rather than a row of links.
 *
 * The control splits two ways: a domain with
 * sub-surfaces opens a menu and carries a chevron (`Network`, `Workloads`, `System`); a
 * domain that *is* one surface navigates straight there and carries none (`Storage`,
 * `Overview`). A one-item menu is a click charged for
 * nothing, and the chevron is what tells an operator which kind of control they are about to
 * press.
 *
 * Overflow follows one intent: when the pill cannot
 * fit, the lowest-priority domains move into a `···` menu — and **the current domain is never
 * the one that moves**, which is the property that makes the behaviour tolerable rather than
 * disorienting. Below the point where even two fit, the whole pill becomes one menu.
 */
import { useLayoutEffect, useRef, useState } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import { Menu, MenuLabel } from './Menu';
import styles from './DomainNav.module.css';
import navStyles from './NavMenu.module.css';

export interface DomainEntry {
  to: string;
  label: string;
  count?: number | null | undefined;
  description: string;
  /** The domain's own index surface. Matched exactly, so a child route does not light it. */
  root?: boolean;
}

export interface Domain {
  id: string;
  label: string;
  to: string;
  /** Lower sorts earlier and survives overflow longer. */
  priority: number;
  attention?: boolean;
  count?: number | null | undefined;
  entries: readonly DomainEntry[];
}

function isCurrent(pathname: string, to: string): boolean {
  if (to === '/') return pathname === '/';
  return pathname === to || pathname.startsWith(`${to}/`);
}

export function DomainNav({ domains }: { domains: readonly Domain[] }): JSX.Element {
  const location = useLocation();
  const railRef = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(domains.length);

  // How many domains fit. Measured from the rail's own box so the end groups changing width
  // (a longer hostname, a wider clock) re-runs it without a resize event.
  useLayoutEffect(() => {
    const rail = railRef.current;
    if (!rail || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? 0;
      if (width <= 0) return;
      // ~104px per domain control plus the overflow control; deliberately generous, because
      // a domain that overflows is better than one that is clipped.
      const fits = Math.floor((width - 44) / 104);
      setVisible(Math.max(1, Math.min(domains.length, fits)));
    });
    observer.observe(rail);
    return () => observer.disconnect();
  }, [domains.length]);

  const ordered = [...domains].sort((a, b) => a.priority - b.priority);
  const currentId = ordered.find((d) => isCurrent(location.pathname, d.to))?.id;

  // The current domain is promoted into the visible set if overflow would have hidden it.
  let shown = ordered.slice(0, visible);
  let hidden = ordered.slice(visible);
  if (currentId && !shown.some((d) => d.id === currentId)) {
    const current = hidden.find((d) => d.id === currentId);
    if (current) {
      shown = [...shown.slice(0, -1), current];
      hidden = ordered.filter((d) => !shown.some((s) => s.id === d.id));
    }
  }

  const compact = visible <= 1 && domains.length > 1;

  return (
    <div className={navStyles.rail} ref={railRef}>
      <nav className={navStyles.pill} aria-label="Primary">
        {compact ? (
          <CompactNav domains={ordered} currentId={currentId} />
        ) : (
          <>
            {shown.map((domain) => (
              <DomainControl
                key={domain.id}
                domain={domain}
                current={domain.id === currentId}
              />
            ))}
            {hidden.length > 0 ? <OverflowControl domains={hidden} /> : null}
          </>
        )}
      </nav>
    </div>
  );
}

function DomainControl({ domain, current }: { domain: Domain; current: boolean }): JSX.Element {
  // A domain that is a single surface is a link, not a menu — see the note at the top.
  if (domain.entries.length <= 1) {
    return (
      <div className={navStyles.group}>
        <NavLink
          to={domain.to}
          end={domain.to === '/'}
          className={[navStyles.navbtn, current ? navStyles.current : ''].filter(Boolean).join(' ')}
          aria-current={current ? 'page' : undefined}
        >
          {domain.label}
          {domain.attention ? (
            <span className={navStyles.dot} title="Something in this domain wants attention" />
          ) : null}
          {domain.count === undefined ? null : (
            <span className={navStyles.count}>
              {domain.count === null ? '—' : domain.count.toLocaleString()}
            </span>
          )}
        </NavLink>
      </div>
    );
  }

  return (
    <div className={navStyles.group}>
      <Menu
        label={`${domain.label} sections`}
        align="centre"
        columns={domain.entries.length > 3 ? 2 : 1}
        renderTrigger={({ open, toggle, ref, controls }) => (
          <button
            ref={ref}
            type="button"
            className={[navStyles.navbtn, current ? navStyles.current : '']
              .filter(Boolean)
              .join(' ')}
            aria-expanded={open}
            aria-haspopup="menu"
            aria-controls={controls}
            aria-current={current ? 'page' : undefined}
            onClick={toggle}
          >
            {domain.label}
            {domain.attention ? (
              <span className={navStyles.dot} title="Something in this domain wants attention" />
            ) : null}
            {domain.count === undefined ? null : (
              <span className={navStyles.count}>
                {domain.count === null ? '—' : domain.count.toLocaleString()}
              </span>
            )}
            <span className={navStyles.chev} aria-hidden="true">
              ▾
            </span>
          </button>
        )}
      >
        {domain.entries.map((entry) => (
          <DomainMenuItem key={entry.to} entry={entry} />
        ))}
      </Menu>
    </div>
  );
}

function DomainMenuItem({ entry }: { entry: DomainEntry }): JSX.Element {
  return (
    <NavLink to={entry.to} className={styles.item ?? ''} role="menuitem" end={entry.root ?? false}>
      <span className={styles.itemTitle}>
        {entry.label}
        {entry.count === undefined ? null : (
          <span className={styles.itemCount}>
            {entry.count === null ? '—' : entry.count.toLocaleString()}
          </span>
        )}
      </span>
      <span className={styles.itemDescription}>{entry.description}</span>
    </NavLink>
  );
}

function OverflowControl({ domains }: { domains: readonly Domain[] }): JSX.Element {
  return (
    <div className={navStyles.group}>
      <Menu
        label="More sections"
        align="centre"
        columns={1}
        renderTrigger={({ open, toggle, ref, controls }) => (
          <button
            ref={ref}
            type="button"
            className={`${navStyles.navbtn} ${navStyles.more}`}
            aria-expanded={open}
            aria-haspopup="menu"
            aria-controls={controls}
            aria-label={`${domains.length} more sections`}
            onClick={toggle}
          >
            ···
            {domains.some((d) => d.attention) ? <span className={navStyles.dot} /> : null}
          </button>
        )}
      >
        {domains.map((domain) => (
          <div key={domain.id}>
            <MenuLabel>{domain.label}</MenuLabel>
            {domain.entries.map((entry) => (
              <DomainMenuItem key={entry.to} entry={entry} />
            ))}
          </div>
        ))}
      </Menu>
    </div>
  );
}

/** At genuinely narrow widths the whole of navigation becomes one menu — never a sidebar. */
function CompactNav({
  domains,
  currentId,
}: {
  domains: readonly Domain[];
  currentId: string | undefined;
}): JSX.Element {
  const current = domains.find((d) => d.id === currentId);
  return (
    <div className={navStyles.group}>
      <Menu
        label="Navigate"
        align="centre"
        columns={1}
        renderTrigger={({ open, toggle, ref, controls }) => (
          <button
            ref={ref}
            type="button"
            className={`${navStyles.navbtn} ${navStyles.current}`}
            aria-expanded={open}
            aria-haspopup="menu"
            aria-controls={controls}
            onClick={toggle}
          >
            <span aria-hidden="true">☰</span>
            {current?.label ?? 'Navigate'}
            <span className={navStyles.chev} aria-hidden="true">
              ▾
            </span>
          </button>
        )}
      >
        {domains.map((domain) => (
          <div key={domain.id}>
            <MenuLabel>{domain.label}</MenuLabel>
            {domain.entries.map((entry) => (
              <DomainMenuItem key={entry.to} entry={entry} />
            ))}
          </div>
        ))}
      </Menu>
    </div>
  );
}
