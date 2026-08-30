/** A 17 px mono token, for identifiers and short machine values. */
import type { ReactNode } from 'react';
import styles from './Chip.module.css';

export function Tag({ children, title }: { children: ReactNode; title?: string }): JSX.Element {
  return (
    <span className={styles.tag} {...(title ? { title } : {})}>
      {children}
    </span>
  );
}

/** A count paired with a word, as the attention rail uses. */
export function Counter({
  count,
  word,
  tone = 'neutral',
}: {
  count: number;
  word: string;
  tone?: 'neutral' | 'good' | 'attention' | 'warn';
}): JSX.Element {
  return (
    <span className={`${styles.counter} ${styles[tone]}`}>
      <span className={styles.count}>{count}</span>
      <span className={styles.word}>{word}</span>
    </span>
  );
}
