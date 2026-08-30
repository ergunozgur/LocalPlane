/**
 * Render a dashboard layout.
 *
 * The grid is CSS Grid over twelve columns rather than an absolutely-positioned engine.
 * Pixel placement exists to serve an editor that drags and resizes widgets; without an
 * editor, CSS Grid gives the same arrangement, reflows sensibly when the viewport narrows,
 * and needs no measurement pass. When the editor arrives it can take over placement without
 * the widgets or the layout type changing.
 *
 * Widget heights are not forced. `h` is honoured as a *minimum* so a widget cannot
 * collapse, while content longer than its allotment grows the row rather than being clipped —
 * an operator losing the last two rows of a table to a layout constant is worse than a
 * slightly taller page.
 */
import { WIDGETS } from './registry';
import { GRID_COLUMNS, GRID_ROW_HEIGHT, visibleWidgets, type DashboardLayout } from './model';
import styles from './DashboardGrid.module.css';

export function DashboardGrid({ layout }: { layout: DashboardLayout }): JSX.Element {
  return (
    <>
      {layout.sections.map((section) => {
        const widgets = visibleWidgets(section, layout, WIDGETS);
        if (widgets.length === 0) return null;

        return (
          <section key={section.id} className={styles.section} aria-label={section.title ?? undefined}>
            {section.title ? (
              <div className={styles.band}>
                <h2 className={styles.bandTitle}>{section.title}</h2>
              </div>
            ) : null}

            <div className={styles.grid}>
              {widgets.map((entry) => {
                const definition = WIDGETS.get(entry.widget);
                if (!definition) return null;
                return (
                  <div
                    key={entry.widget}
                    className={styles.cell}
                    style={{
                      gridColumn: `span ${Math.min(entry.placement.w, GRID_COLUMNS)}`,
                      minHeight: entry.placement.h * GRID_ROW_HEIGHT,
                    }}
                    data-widget={entry.widget}
                    data-focal={definition.focal ? 'true' : undefined}
                  >
                    {definition.render()}
                  </div>
                );
              })}
            </div>
          </section>
        );
      })}
    </>
  );
}
