import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import { useLocation, useNavigate } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import styles from './GlobalSearch.module.css';

type SearchDomain = 'Network' | 'System' | 'Workloads';

type SearchItem = {
  id: string;
  name: string;
  domain: SearchDomain;
  kind: string;
  to: string;
};

type Source = {
  label: SearchDomain;
  status: 'loading' | 'success' | 'failed';
  items: SearchItem[];
};

const MAX_RESULTS = 40;
const SOURCE_LABELS: readonly SearchDomain[] = ['Network', 'System', 'Workloads'];

function loadingSources(): Source[] {
  return SOURCE_LABELS.map((label) => ({ label, status: 'loading', items: [] }));
}

function resultId(index: number): string {
  return `global-search-result-${index}`;
}

export function GlobalSearch(): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const locationRef = useRef(location.key);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [sources, setSources] = useState<Source[]>(loadingSources);
  const [activeIndex, setActiveIndex] = useState(0);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const instanceId = useId();

  const openSearch = useCallback(() => {
    if (open) return;
    const active = document.activeElement;
    previousFocusRef.current = active instanceof HTMLElement ? active : triggerRef.current;
    setQuery('');
    setActiveIndex(0);
    setOpen(true);
  }, [open]);

  const dismiss = useCallback(
    (restoreFocus = true) => {
      if (!open) return;
      if (!restoreFocus) previousFocusRef.current = null;
      setOpen(false);
    },
    [open],
  );

  useEffect(() => {
    if (locationRef.current !== location.key) {
      locationRef.current = location.key;
      dismiss(false);
    }
  }, [location.key, dismiss]);

  useEffect(() => {
    const onShortcut = (event: globalThis.KeyboardEvent): void => {
      if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        openSearch();
      }
    };
    document.addEventListener('keydown', onShortcut);
    return () => document.removeEventListener('keydown', onShortcut);
  }, [openSearch]);

  useEffect(() => {
    if (!open) {
      const previous = previousFocusRef.current;
      previousFocusRef.current = null;
      if (previous && document.contains(previous)) previous.focus();
      return;
    }

    inputRef.current?.focus();
    const controller = new AbortController();
    let live = true;
    setSources(loadingSources());

    const requests: readonly Promise<SearchItem[]>[] = [
      endpoints.interfaces({ signal: controller.signal }).then((list) =>
        list.interfaces.map((item) => ({
          id: item.object_id,
          name: item.name,
          domain: 'Network',
          kind: 'interface',
          to: `/network/${encodeURIComponent(item.object_id)}`,
        })),
      ),
      endpoints.systemdUnits({ signal: controller.signal }).then((list) =>
        list.units.map((item) => ({
          id: item.object_id,
          name: item.canonical_id,
          domain: 'System',
          kind: 'systemd unit',
          to: `/system/${encodeURIComponent(item.object_id)}`,
        })),
      ),
      endpoints.containers({ signal: controller.signal }).then((list) =>
        list.containers.map((item) => ({
          id: item.object_id,
          name: item.name,
          domain: 'Workloads',
          kind: 'container',
          to: `/workloads/${encodeURIComponent(item.object_id)}`,
        })),
      ),
    ];

    requests.forEach((request, index) => {
      const settle = (status: Source['status'], items: SearchItem[]): void => {
        if (!live) return;
        setSources((current) => current.map((source, position) =>
          position === index ? { ...source, status, items } : source));
      };
      void request.then((items) => settle('success', items), () => settle('failed', []));
    });

    return () => {
      live = false;
      controller.abort();
    };
  }, [open]);

  const allItems = sources.flatMap((source) => source.items);
  const needle = query.trim().toLowerCase();
  const matches = needle
    ? allItems.filter(
        (item) => item.name.toLowerCase().includes(needle) || item.id.toLowerCase().includes(needle),
      )
    : allItems;
  const visibleMatches = matches.slice(0, MAX_RESULTS);
  const loading = sources.some((source) => source.status === 'loading');
  const failed = sources.filter((source) => source.status === 'failed');
  const successful = sources.filter((source) => source.status === 'success');
  const fullFailure = !loading && successful.length === 0;
  const selectedIndex = Math.min(activeIndex, Math.max(0, visibleMatches.length - 1));

  useEffect(() => {
    setActiveIndex(0);
  }, [query]);

  useEffect(() => {
    if (open) document.getElementById(resultId(selectedIndex))?.scrollIntoView?.({ block: 'nearest' });
  }, [open, selectedIndex]);

  const selectItem = (item: SearchItem): void => {
    dismiss(false);
    navigate(item.to);
  };

  const onPanelKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>): void => {
    // Confirming an IME composition must not select a result or dismiss the palette.
    if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      dismiss();
      return;
    }

    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (visibleMatches.length === 0) return;
      event.preventDefault();
      setActiveIndex((index) =>
        event.key === 'ArrowDown'
          ? (index + 1) % visibleMatches.length
          : (index - 1 + visibleMatches.length) % visibleMatches.length,
      );
      inputRef.current?.focus();
      return;
    }

    if (event.key === 'Enter' && document.activeElement === inputRef.current) {
      const selected = visibleMatches[selectedIndex];
      if (selected) {
        event.preventDefault();
        selectItem(selected);
      }
      return;
    }

    if (event.key !== 'Tab') return;
    const focusable = panelRef.current?.querySelectorAll<HTMLElement>(
      'button:not([disabled]):not([tabindex="-1"]), input:not([disabled]), [href], select:not([disabled]), textarea:not([disabled])',
    );
    if (!focusable || focusable.length === 0) return;
    const first = focusable[0]!;
    const last = focusable[focusable.length - 1]!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={styles.trigger}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? `${instanceId}-dialog` : undefined}
        onClick={openSearch}
      >
        <span aria-hidden="true">⌕</span>
        <span>Search objects</span>
        <kbd>Ctrl / ⌘ K</kbd>
      </button>

      {open ? createPortal(
        <div className={styles.overlay}>
          <button
            type="button"
            className={styles.backdrop}
            aria-label="Close object search"
            tabIndex={-1}
            onClick={() => dismiss()}
          />
          <div
            ref={panelRef}
            id={`${instanceId}-dialog`}
            className={styles.dialog}
            role="dialog"
            aria-modal="true"
            aria-label="Search objects"
            onKeyDown={onPanelKeyDown}
          >
            <label className={styles.queryLabel}>
              <span className={styles.queryGlyph} aria-hidden="true">⌕</span>
              <span className="visually-hidden">Search by object name or ID</span>
              <input
                ref={inputRef}
                className={styles.query}
                type="search"
                value={query}
                placeholder="Search objects by name or stable ID"
                autoComplete="off"
                role="combobox"
                aria-autocomplete="list"
                aria-expanded="true"
                aria-controls={`${instanceId}-results`}
                aria-activedescendant={visibleMatches[selectedIndex] ? resultId(selectedIndex) : undefined}
                onChange={(event) => setQuery(event.target.value)}
              />
              <button
                type="button"
                className={styles.close}
                aria-label="Close object search"
                onClick={() => dismiss()}
              >
                Esc
              </button>
            </label>

            <div className={styles.status} aria-live="polite">
              {loading ? <p>Reading observed interfaces, units and containers…</p> : null}
              {!loading && fullFailure ? (
                <p role="alert">Object search is unavailable. None of the observed domains could be read.</p>
              ) : null}
              {!fullFailure && failed.length > 0 ? (
                <p className={styles.partial} role="status">
                  Partial coverage: {failed.map((source) => source.label).join(', ')} could not be read. Results include only successful reads.
                </p>
              ) : null}
            </div>

            <div className={styles.results}>
              {loading ? <p className={styles.state}>Loading observed objects…</p> : null}
              {!loading && !fullFailure && !needle && allItems.length === 0 ? (
                <p className={styles.state}>No objects were returned by the available domains.</p>
              ) : null}
              {!loading && !fullFailure && needle && visibleMatches.length === 0 ? (
                <p className={styles.state}>No observed objects match “{query.trim()}”.</p>
              ) : null}
              <div id={`${instanceId}-results`} role="listbox" aria-label="Search results">
              {!fullFailure
                ? visibleMatches.map((item, index) => (
                    <button
                      key={`${item.domain}:${item.id}`}
                      id={resultId(index)}
                      type="button"
                      role="option"
                      tabIndex={-1}
                      aria-selected={index === selectedIndex}
                      aria-label={`Open ${item.domain} ${item.kind} ${item.name}, ${item.id}`}
                      className={`${styles.result} ${index === selectedIndex ? styles.active : ''}`}
                      onClick={() => selectItem(item)}
                    >
                      <span className={styles.resultKind} title={item.kind}>{item.kind}</span>
                      <span className={styles.resultName} title={item.name}>{item.name}</span>
                      <span className={styles.resultMeta} title={`${item.domain} · ${item.id}`}>
                        {item.domain} · {item.id}
                      </span>
                    </button>
                  ))
                : null}
              </div>
              {!loading && !fullFailure && matches.length > MAX_RESULTS ? (
                <p className={styles.truncated}>
                  Showing the first {MAX_RESULTS} matches. Refine the query to see a specific object.
                </p>
              ) : null}
            </div>
            <div className={styles.footer}>
              <span><kbd>↑</kbd><kbd>↓</kbd> select</span>
              <span><kbd>Enter</kbd> open</span>
              <span><kbd>Esc</kbd> close</span>
              <span>Read-only</span>
            </div>
          </div>
        </div>, document.body,
      ) : null}
    </>
  );
}
