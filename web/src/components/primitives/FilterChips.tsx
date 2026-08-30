/**
 * A chip filter bar.
 *
 * A row of pressable pills, one per value, with the count beside each. It replaces a
 * `<select>` for a reason that is not decoration: a select hides both the available values
 * and how much is in each of them behind a click, and on an operations surface those two
 * facts are most of what an operator wants — *are there any failed changes at all* is
 * answered by looking, not by opening a menu.
 *
 * Counts are optional per option, and the caller is expected to leave them off rather than
 * guess. When filtering happens on the backend, only the active option's count is actually
 * known; a number under the others would be a claim about rows nobody fetched.
 */
import styles from './FilterChips.module.css';

export interface FilterOption {
  value: string;
  label: string;
  /** Omit when the count is not known — never pass a guess. */
  count?: number | undefined;
  /** A tone dot, for values that carry a state colour. */
  tone?: 'good' | 'warn' | 'bad' | 'attention' | 'neutral' | undefined;
}

export function FilterChips({
  legend,
  options,
  value,
  onChange,
}: {
  legend: string;
  options: readonly FilterOption[];
  value: string;
  onChange: (next: string) => void;
}): JSX.Element {
  return (
    <div className={styles.bar} role="group" aria-label={legend}>
      {options.map((option) => {
        const pressed = option.value === value;
        return (
          <button
            key={option.value || 'all'}
            type="button"
            className={styles.chip}
            aria-pressed={pressed}
            onClick={() => onChange(pressed && option.value !== '' ? '' : option.value)}
          >
            {option.tone ? <span className={`${styles.dot} ${styles[option.tone]}`} /> : null}
            {option.label}
            {option.count === undefined ? null : <span className={styles.n}>{option.count}</span>}
          </button>
        );
      })}
    </div>
  );
}
