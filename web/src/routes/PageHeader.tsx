/** A surface's heading: a title, a count, an optional annotation, and controls. */
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import styles from './PageHeader.module.css';

export function PageHeader({
  title,
  count,
  annotation,
  children,
  back,
}: {
  title: ReactNode;
  count?: number;
  annotation?: ReactNode;
  children?: ReactNode;
  back?: { to: string; label: string };
}): JSX.Element {
  return (
    <div className={styles.header}>
      {back ? (
        <Link to={back.to} className={styles.back}>
          ‹ {back.label}
        </Link>
      ) : null}
      <div className={styles.row}>
        <h1 className={styles.title}>{title}</h1>
        {count !== undefined ? <span className={styles.count}>{count}</span> : null}
        {children ? <div className={styles.actions}>{children}</div> : null}
      </div>
      {annotation ? <p className={`${styles.annotation} note`}>{annotation}</p> : null}
    </div>
  );
}
