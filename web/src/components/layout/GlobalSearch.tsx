import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import { useLocation, useNavigate } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { OPERATIONS_WITH_UI_CONTROLS } from '@/domain/execution';
import { serviceIntentUrl, type ServiceOperation } from '@/domain/serviceIntent';
import styles from './GlobalSearch.module.css';

type SearchDomain = 'Network' | 'System' | 'Workloads' | 'Runs' | 'Changes' | 'Findings';

type SearchItem = {
  id: string;
  name: string;
  domain: SearchDomain;
  kind: string;
  to: string;
  fields?: string[];
  state?: string;
  service?: boolean;
  operation?: ServiceOperation;
};

type Source = {
  label: SearchDomain;
  status: 'loading' | 'success' | 'failed';
  items: SearchItem[];
};

const MAX_RESULTS = 40;
const SOURCE_LABELS: readonly SearchDomain[] = ['Network', 'System', 'Workloads', 'Runs', 'Changes', 'Findings'];

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
  const [activeIdentity, setActiveIdentity] = useState<Pick<SearchItem, 'domain' | 'id' | 'operation'> | null>(null);
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
    setActiveIdentity(null);
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
          service: item.unit_type === 'service' && item.load_state === 'loaded',
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
      endpoints.runs({ state: 'all', limit: 50 }, { signal: controller.signal }).then((list) =>
        list.runs.map((item) => ({
          id: item.run_id,
          name: `${item.object_name} · ${item.operation}`,
          domain: 'Runs',
          kind: 'run',
          state: item.state,
          fields: [item.object_id, item.object_name, item.operation, item.state],
          to: `/operations/runs/${encodeURIComponent(item.run_id)}`,
        })),
      ),
      endpoints.changes({ limit: 100 }, { signal: controller.signal }).then((list) =>
        list.changes.map((item) => ({
          id: item.change_id,
          name: `${item.object_name} · ${item.operation}`,
          domain: 'Changes',
          kind: 'change',
          state: item.result,
          fields: [item.object_id, item.object_name, item.operation, item.result],
          to: `/operations/changes/${encodeURIComponent(item.change_id)}`,
        })),
      ),
      endpoints.findings({ status: 'all', limit: 100 }, { signal: controller.signal }).then((list) =>
        list.findings.map((item) => ({
          id: item.finding_id,
          name: `${item.object_name} · ${item.summary}`,
          domain: 'Findings',
          kind: 'finding',
          state: item.status,
          fields: [item.object_id, item.object_name, item.summary, item.finding_type, item.subject, item.status],
          to: `/operations/findings/${encodeURIComponent(item.finding_id)}`,
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

  const runMode = query.trim().startsWith('>');
  const relevantSources = runMode ? sources.filter((source) => source.label === 'System') : sources;
  const allItems = relevantSources.flatMap((source) => source.items).flatMap((item): SearchItem[] =>
    !runMode ? [item] : item.service ? OPERATIONS_WITH_UI_CONTROLS.map((operation) => ({
      ...item, operation, kind: operation.split('.').at(-1)!,
      name: `${operation.split('.').at(-1)} ${item.name}`,
      to: serviceIntentUrl(item.id, operation),
    })) : []);
  const needle = (runMode ? query.trim().slice(1) : query).trim().toLowerCase();
  const matches = needle
    ? allItems.filter(
        (item) => {
          if (runMode) {
            const [action = '', ...target] = needle.split(/\s+/);
            return item.kind.startsWith(action) && target.every((part) =>
              `${item.name} ${item.id}`.toLowerCase().includes(part));
          }
          return [item.name, item.id, ...(item.fields ?? [])].some((field) => field.toLowerCase().includes(needle));
        },
      )
    : allItems;
  const visibleMatches = matches.slice(0, MAX_RESULTS);
  const loading = relevantSources.some((source) => source.status === 'loading');
  const failed = relevantSources.filter((source) => source.status === 'failed');
  const successful = relevantSources.filter((source) => source.status === 'success');
  const fullFailure = !loading && successful.length === 0;
  // Until explicit keyboard intent, follow the first result. A displaced explicit
  // identity selects nothing rather than silently opening a replacement or hidden item.
  const selectedIndex = activeIdentity
    ? visibleMatches.findIndex((item) => item.domain === activeIdentity.domain && item.id === activeIdentity.id && item.operation === activeIdentity.operation)
    : visibleMatches.length > 0 ? 0 : -1;

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
      const nextIndex = event.key === 'ArrowDown'
        ? (selectedIndex + 1) % visibleMatches.length
        : (selectedIndex < 0 ? visibleMatches.length - 1 : (selectedIndex - 1 + visibleMatches.length) % visibleMatches.length);
      const next = visibleMatches[nextIndex]!;
      setActiveIdentity(next);
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
        <span>Search objects & records</span>
        <kbd>Ctrl / ⌘ K</kbd>
      </button>

      {open ? createPortal(
        <div className={styles.overlay}>
          <button
            type="button"
            className={styles.backdrop}
            aria-label="Close object and record search"
            tabIndex={-1}
            onClick={() => dismiss()}
          />
          <div
            ref={panelRef}
            id={`${instanceId}-dialog`}
            className={styles.dialog}
            role="dialog"
            aria-modal="true"
            aria-label="Search objects and records"
            onKeyDown={onPanelKeyDown}
          >
            <label className={styles.queryLabel}>
              <span className={styles.queryGlyph} aria-hidden="true">⌕</span>
              <span className="visually-hidden">Search objects and records by name, ID, operation, summary or state</span>
              <input
                ref={inputRef}
                className={styles.query}
                type="search"
                value={query}
                placeholder="Search — or type > for a service action"
                autoComplete="off"
                role="combobox"
                aria-autocomplete="list"
                aria-expanded="true"
                aria-controls={`${instanceId}-results`}
                aria-activedescendant={visibleMatches[selectedIndex] ? resultId(selectedIndex) : undefined}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setActiveIdentity(null);
                }}
              />
              {runMode ? <span className={styles.resultKind}>run</span> : null}
              <button
                type="button"
                className={styles.close}
                aria-label="Close object and record search"
                onClick={() => dismiss()}
              >
                Esc
              </button>
            </label>

            <div className={styles.status} aria-live="polite">
              {runMode
                ? <p>Start, stop or restart a loaded service. Selection stages an action; explicit Plan creates a Run. Protection and execution are assessed by the backend.</p>
                : <p>Recent records only: up to 50 Runs, 100 Changes and 100 Findings (open and resolved). Older records may be excluded; returned counts are not history totals.</p>}
              {loading ? <p>{runMode ? 'Reading service targets…' : 'Reading objects and recent records…'}</p> : null}
              {!loading && fullFailure ? (
                <p role="alert">{runMode ? 'Service actions are unavailable. System units could not be read.' : 'Search is unavailable. None of the sources could be read.'}</p>
              ) : null}
              {!fullFailure && failed.length > 0 ? (
                <p className={styles.partial} role="status">
                  Partial coverage: {failed.map((source) => source.label).join(', ')} could not be read. Results include only successful reads.
                </p>
              ) : null}
            </div>

            <div className={styles.results}>
              {loading ? <p className={styles.state}>{runMode ? 'Loading service targets…' : 'Loading objects and recent records…'}</p> : null}
              {!loading && !fullFailure && !needle && allItems.length === 0 ? (
                <p className={styles.state}>{runMode ? 'No loaded services were returned. Other unit types and unloaded services are not action targets.' : 'No objects or recent records were returned by the available sources.'}</p>
              ) : null}
              {!loading && !fullFailure && needle && visibleMatches.length === 0 ? (
                <p className={styles.state}>{runMode ? `No loaded service actions match “${needle}”. Try > start, > stop or > restart followed by a service name or ID.` : `No loaded objects or records match “${query.trim()}”.`}</p>
              ) : null}
              <div id={`${instanceId}-results`} role="listbox" aria-label="Search results">
              {!fullFailure
                ? visibleMatches.map((item, index) => (
                    <button
                      key={JSON.stringify([item.domain, item.id, item.operation])}
                      id={resultId(index)}
                      type="button"
                      role="option"
                      tabIndex={-1}
                      aria-selected={index === selectedIndex}
                      aria-label={item.operation ? `Stage ${item.name}, ${item.id}` : `Open ${item.domain} ${item.kind} ${item.name}, ${item.id}${item.state ? `, ${item.state}` : ''}`}
                      className={`${styles.result} ${index === selectedIndex ? styles.active : ''}`}
                      onClick={() => selectItem(item)}
                    >
                      <span className={styles.resultKind} title={item.kind}>{item.kind}</span>
                      <span className={styles.resultName} title={item.name}>{item.name}</span>
                      <span className={styles.resultMeta} title={`${item.state ?? item.domain} · ${item.id}`}>
                        {item.state ?? item.domain} · {item.id}
                      </span>
                    </button>
                  ))
                : null}
              </div>
              {!loading && !fullFailure && matches.length > MAX_RESULTS ? (
                <p className={styles.truncated}>
                  Showing the first {MAX_RESULTS} loaded matches. Refine the query to see a specific {runMode ? 'service action' : 'object or record'}.
                </p>
              ) : null}
            </div>
            <div className={styles.footer}>
              <span><kbd>↑</kbd><kbd>↓</kbd> select</span>
              <span><kbd>Enter</kbd> {runMode ? 'stage' : 'open'}</span>
              <span><kbd>Esc</kbd> close</span>
              <span>{runMode ? 'Plan before running' : <><kbd>&gt;</kbd> service action · Read-only</>}</span>
            </div>
          </div>
        </div>, document.body,
      ) : null}
    </>
  );
}
