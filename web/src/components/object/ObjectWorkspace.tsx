/**
 * The object workspace.
 *
 * The detail model is not a set of long scrolling pages. It is one reusable workspace: a
 * lead plate naming the object, a pathline saying where it sits, a tab strip assembled from
 * what the object *is*, and a body that changes with the tab. The assembly rule is why it
 * matters: *the workspace is assembled from what the object is, not from a fixed menu…
 * that is why there is no global Wi-Fi page: those are not domains, they are properties
 * of one interface.*
 *
 * Two deliberate choices:
 *
 *  - **The tab lives in the URL** (`?tab=`), not in a module variable. A tab is a place, and
 *    in a routed application places have addresses — an operator can send someone the
 *    Evidence tab of one interface.
 *  - **Tabs are real tabs**, with roles, roving focus and keyboard movement.
 *
 * **Temporal seam.** This component never asks what time it is. `observedAt` describes *the
 * data being shown* and is supplied by the caller, so a future historical mode passes a
 * different value without this component learning that modes exist.
 */
import { useMemo, type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Plate, PlateHead } from '@/components/primitives/Plate';
import type { Semantic } from '@/domain/vocabulary';
import { Pathline, type PathSegment } from './Pathline';
import { ObjectTabs, type ObjectTab } from './ObjectTabs';
import { RecordRail } from './RecordRail';
import styles from './ObjectWorkspace.module.css';

export type { ObjectTab } from './ObjectTabs';
export type { PathSegment } from './Pathline';

export function ObjectWorkspace({
  objectId,
  name,
  kind,
  mark,
  meta,
  headline,
  chips,
  actions,
  path,
  tabs,
  observedAt,
  contextFact,
  tone,
}: {
  /** The object's own id, used to scope the record rail. */
  objectId: string;
  name: string;
  /** What kind of thing this is — a plain chip beside the name. */
  kind: string;
  mark?: Semantic | undefined;
  meta?: ReactNode;
  /** This object's headline: one sentence saying what it is, in the product's voice. */
  headline?: ReactNode;
  /** State chips: health, management, reconciliation. Never invented here. */
  chips?: ReactNode;
  /** Rendered only where a real action exists; absent is correct when none does. */
  actions?: ReactNode;
  path: readonly PathSegment[];
  tabs: readonly ObjectTab[];
  /** When the data on screen was read. Supplied, never derived — see the temporal seam. */
  observedAt: Date;
  contextFact?: ReactNode;
  /** The drifted/warned head treatment on the object's own plate. */
  tone?: 'attention' | 'warn' | 'good' | undefined;
}): JSX.Element {
  const [params, setParams] = useSearchParams();
  const requested = params.get('tab');
  const visible = useMemo(() => tabs.filter((tab) => !tab.hidden), [tabs]);
  const active = visible.find((tab) => tab.id === requested) ?? visible[0];

  const select = (id: string): void => {
    const next = new URLSearchParams(params);
    if (id === visible[0]?.id) next.delete('tab');
    else next.set('tab', id);
    setParams(next, { replace: true });
  };

  return (
    <>
      <Plate lead {...(tone ? { tone } : {})}>
        <PlateHead
          title={<span className={styles.name}>{name}</span>}
          {...(mark ? { mark } : {})}
          {...(meta ? { meta } : {})}
          asOf={observedAt.toLocaleTimeString()}
          chips={
            <>
              <span className={styles.kind}>{kind}</span>
              {chips}
            </>
          }
        >
          {actions}
        </PlateHead>

        {headline ? <p className={styles.headline}>{headline}</p> : null}

        <Pathline
          segments={[...path, ...(active ? [{ label: active.label, current: true }] : [])]}
          {...(contextFact ? { fact: contextFact } : {})}
        />
      </Plate>

      <ObjectTabs tabs={tabs} activeId={active?.id ?? ''} onSelect={select} />

      {/* The tab body and the machine record, side by side. The record
          is part of the workspace rather than a tab, because the question it answers — what
          has happened to this thing — applies to whatever tab is open. */}
      <div className={styles.layout}>
        <div className={styles.body} role="tabpanel" aria-label={active?.label ?? name}>
          {active?.render()}
        </div>
        <div className={styles.rail}>
          <RecordRail objectId={objectId} objectName={name} />
        </div>
      </div>
    </>
  );
}

/** A column pair for tab bodies that want the workspace's standard two-column reading. */
export function ObjectColumns({
  main,
  side,
}: {
  main: ReactNode;
  side?: ReactNode;
}): JSX.Element {
  if (!side) return <div className={styles.single}>{main}</div>;
  return (
    <div className={styles.columns}>
      <div className={styles.column}>{main}</div>
      <div className={styles.column}>{side}</div>
    </div>
  );
}
