/**
 * A menu anchored to its trigger.
 *
 * Menus are centred under the control that opened them and point at it with a CSS
 * caret: an anchored panel reads as belonging to its control, which matters
 * when several sit side by side along a bar.
 *
 * The panel is a real `role="menu"`, Escape
 * returns focus to the trigger, and the open panel is clamped back inside the viewport.
 */
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import styles from './Menu.module.css';

export type MenuAlign = 'left' | 'right' | 'centre';

export function Menu({
  label,
  trigger,
  children,
  align = 'right',
  columns = 1,
  className,
  renderTrigger,
}: {
  label: string;
  trigger?: ReactNode;
  children: ReactNode;
  align?: MenuAlign;
  columns?: 1 | 2;
  className?: string | undefined;
  /** Lets a caller supply the whole trigger element — used by the domain nav. */
  renderTrigger?: (props: {
    open: boolean;
    toggle: () => void;
    ref: React.RefObject<HTMLButtonElement>;
    controls: string | undefined;
  }) => ReactNode;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const menuId = useId();

  const close = useCallback((restoreFocus: boolean) => {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent): void => {
      if (!rootRef.current?.contains(event.target as globalThis.Node)) close(false);
    };
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') close(true);
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open, close]);

  // A centred panel near the edge of a narrow window would hang off it.
  // Nudge it back rather than letting the browser clip it.
  useLayoutEffect(() => {
    const panel = panelRef.current;
    if (!open || !panel) return;
    panel.style.marginLeft = '';
    const box = panel.getBoundingClientRect();
    if (!box.width) return;
    const pad = 8;
    let shift = 0;
    if (box.right > window.innerWidth - pad) shift = window.innerWidth - pad - box.right;
    if (box.left + shift < pad) shift = pad - box.left;
    if (shift) panel.style.marginLeft = `${shift}px`;
  }, [open]);

  const toggle = useCallback(() => setOpen((value) => !value), []);

  return (
    <div className={styles.root} ref={rootRef}>
      {renderTrigger ? (
        renderTrigger({ open, toggle, ref: triggerRef, controls: open ? menuId : undefined })
      ) : (
        <button
          ref={triggerRef}
          type="button"
          className={[styles.trigger, className ?? ''].filter(Boolean).join(' ')}
          aria-expanded={open}
          aria-haspopup="menu"
          aria-controls={open ? menuId : undefined}
          aria-label={label}
          onClick={toggle}
        >
          {trigger}
        </button>
      )}
      {open ? (
        <div
          ref={panelRef}
          id={menuId}
          role="menu"
          aria-label={label}
          className={`${styles.menu} ${styles[align]}`}
        >
          <div className={columns === 2 ? styles.grid : styles.single}>{children}</div>
        </div>
      ) : null}
    </div>
  );
}

export function MenuLabel({ children }: { children: ReactNode }): JSX.Element {
  return <div className={styles.label}>{children}</div>;
}

export function MenuSeparator(): JSX.Element {
  return <div className={styles.separator} role="separator" />;
}
