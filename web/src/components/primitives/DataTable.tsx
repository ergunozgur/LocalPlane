/**
 * A dense table.
 *
 * Scrolls inside its own container so that a wide table never makes the page scroll
 * sideways, and keeps a real `<table>` so that row and column relationships reach assistive
 * technology. The table is set at 12.5 px.
 */
import type { ReactNode } from 'react';
import styles from './DataTable.module.css';

export interface Column<T> {
  key: string;
  header: ReactNode;
  render: (row: T) => ReactNode;
  /** `right` for magnitudes, `center` for marks. Identifiers stay left. */
  align?: 'left' | 'right' | 'center';
  width?: string;
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  caption,
  onRowActivate,
  rowTone,
  emptyState,
}: {
  columns: ReadonlyArray<Column<T>>;
  rows: readonly T[];
  rowKey: (row: T) => string;
  caption?: string;
  /**
   * Makes the whole row a pointer target.
   *
   * The keyboard path stays the real link inside the row: making the row itself tabbable
   * would put two tab stops on one destination, and the second one would announce the whole
   * row as its name. So this is a convenience for the pointer, never the only way in.
   */
  onRowActivate?: (row: T) => void;
  /** A per-row tone. `attention` is the drifted row — the row itself carries the state. */
  rowTone?: (row: T) => 'attention' | 'warn' | undefined;
  emptyState?: ReactNode;
}): JSX.Element {
  if (rows.length === 0 && emptyState) return <>{emptyState}</>;

  return (
    <div className={styles.wrap}>
      <table className={styles.table}>
        {caption ? <caption className="visually-hidden">{caption}</caption> : null}
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={column.align ? styles[column.align] : undefined}
                style={column.width ? { width: column.width } : undefined}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={rowKey(row)}
              className={[
                onRowActivate ? styles.clickable : '',
                rowTone ? (styles[rowTone(row) ?? ''] ?? '') : '',
              ]
                .filter(Boolean)
                .join(' ')}
              onClick={
                onRowActivate
                  ? (event) => {
                      // A click that landed on the row's own link has already navigated;
                      // handling it again would push the same entry twice.
                      if ((event.target as HTMLElement).closest('a, button')) return;
                      onRowActivate(row);
                    }
                  : undefined
              }
            >
              {columns.map((column) => (
                <td key={column.key} className={column.align ? styles[column.align] : undefined}>
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
