/**
 * The object tab strip.
 *
 * Same selection grammar as the primary nav and the scope tabs — an inset accent underline
 * drawn by `::after`, with the label at weight 650. Three strips, one system.
 *
 * These are real tabs, not plain buttons: `role="tablist"`, `aria-selected`,
 * roving `tabIndex` and Home/End/arrow-key movement, so the strip is usable from the
 * keyboard. That is an improvement on the design, not a departure from it.
 */
import { useRef, type ReactNode } from 'react';
import styles from './ObjectTabs.module.css';

export interface ObjectTab {
  id: string;
  label: string;
  /** A count beside the label, when one is real. */
  count?: number | null | undefined;
  /** An amber dot — the tab holds something worth reading. */
  warn?: boolean;
  /** Assembled-away rather than rendered disabled: a tab that cannot apply is not a tab. */
  hidden?: boolean;
  render: () => ReactNode;
}

export function ObjectTabs({
  tabs,
  activeId,
  onSelect,
}: {
  tabs: readonly ObjectTab[];
  activeId: string;
  onSelect: (id: string) => void;
}): JSX.Element {
  const visible = tabs.filter((tab) => !tab.hidden);
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});

  const move = (delta: number): void => {
    const index = visible.findIndex((tab) => tab.id === activeId);
    const next = visible[(index + delta + visible.length) % visible.length];
    if (!next) return;
    onSelect(next.id);
    refs.current[next.id]?.focus();
  };

  return (
    <div className={styles.tabs} role="tablist" aria-label="Object sections">
      {visible.map((tab) => {
        const selected = tab.id === activeId;
        return (
          <button
            key={tab.id}
            ref={(element) => {
              refs.current[tab.id] = element;
            }}
            type="button"
            role="tab"
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            className={[styles.tab, selected ? styles.selected : ''].filter(Boolean).join(' ')}
            onClick={() => onSelect(tab.id)}
            onKeyDown={(event) => {
              if (event.key === 'ArrowRight') { event.preventDefault(); move(1); }
              if (event.key === 'ArrowLeft') { event.preventDefault(); move(-1); }
              if (event.key === 'Home') { event.preventDefault(); const f = visible[0]; if (f) { onSelect(f.id); refs.current[f.id]?.focus(); } }
              if (event.key === 'End') { event.preventDefault(); const l = visible[visible.length - 1]; if (l) { onSelect(l.id); refs.current[l.id]?.focus(); } }
            }}
          >
            {tab.label}
            {tab.count === undefined ? null : (
              <span className={styles.count}>
                {tab.count === null ? '—' : tab.count.toLocaleString()}
              </span>
            )}
            {tab.warn ? <span className={styles.warnDot} aria-hidden="true" /> : null}
          </button>
        );
      })}
    </div>
  );
}
