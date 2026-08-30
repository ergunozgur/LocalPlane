/**
 * The relationship plane: Upstream → Path → Host → Attached.
 *
 * This is the composition that makes LocalPlane read as a control plane rather than a
 * metrics dashboard. Where the nodes come from — and which node kinds are deliberately
 * absent — is `topology-model.ts`.
 *
 * Four things the plane does that a diagram usually does not, and that are the reason
 * this is worth building rather than drawing:
 *
 *  - **A node is a selection, not a link.** Selecting one lights its edges, dims the rest,
 *    and writes what it is into the evidence strip. The strip carries the link. An operator
 *    finds out *why* an edge exists before leaving the page it explains.
 *  - **An edge label is a first-class element**, placed against the other labels and against
 *    every node. When one will not fit it is dropped, the count of dropped labels is stated,
 *    and the relationship strip below carries every relationship in words — so shortening a
 *    label never loses a fact.
 *  - **Density is elementwise.** Narrowing takes away the descriptive tail, then the kind,
 *    then the sparkline slot; identity is never touched, and type never shrinks.
 *  - **There is no flow animation.** No dashed overlay animates along an edge to suggest
 *    traffic: this build has no throughput series, and an animated line implying live
 *    traffic would be the most persuasive lie on the page.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { StatusDot } from '@/components/primitives/Plate';
import { COLUMNS, type TopologyEdge, type TopologyNode } from './topology-model';
import styles from './Topology.module.css';

interface PlacedEdge {
  id: string;
  from: string;
  to: string;
  kind: TopologyEdge['kind'];
  d: string;
  label?: string | undefined;
  /** Where the label was placed, when it was placed at all. */
  at?: { x: number; y: number; w: number } | undefined;
}

/** Element density, decided by the plane's own measured width — never the viewport's. */
function densityFor(width: number): 'full' | 'tight' | 'compact' | 'min' {
  if (width >= 1000) return 'full';
  if (width >= 800) return 'tight';
  if (width >= 620) return 'compact';
  return 'min';
}

export function Topology({
  nodes,
  columns = 4,
}: {
  nodes: readonly TopologyNode[];
  columns?: number;
}): JSX.Element {
  const firstColumn = 4 - columns;
  const shown = useMemo(
    () => nodes.filter((node) => node.column >= firstColumn),
    [nodes, firstColumn],
  );

  const containerRef = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState<PlacedEdge[]>([]);
  const [dropped, setDropped] = useState(0);
  const [density, setDensity] = useState<'full' | 'tight' | 'compact' | 'min'>('full');
  const [selected, setSelected] = useState<string | null>(null);

  const measure = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;
    const origin = container.getBoundingClientRect();
    setDensity(densityFor(origin.width));

    const box = (id: string): DOMRect | null =>
      container.querySelector<HTMLElement>(`[data-node="${CSS.escape(id)}"]`)?.getBoundingClientRect() ??
      null;

    const placed: PlacedEdge[] = [];
    // Every rectangle a label must avoid: the nodes themselves, then each label already
    // placed. Checked in order, so an earlier edge keeps its label and a later one gives it
    // up — which is why the model lists the load-bearing relationships first.
    const occupied: Array<{ x: number; y: number; w: number; h: number }> = [];
    for (const node of shown) {
      const rect = box(node.id);
      if (rect) {
        occupied.push({
          x: rect.left - origin.left,
          y: rect.top - origin.top,
          w: rect.width,
          h: rect.height,
        });
      }
    }

    let lost = 0;
    for (const node of shown) {
      for (const edge of node.edges ?? []) {
        const from = box(edge.from);
        const to = box(node.id);
        if (!from || !to) continue;
        const x1 = from.right - origin.left;
        const y1 = from.top + from.height / 2 - origin.top;
        const x2 = to.left - origin.left;
        const y2 = to.top + to.height / 2 - origin.top;
        const mid = x1 + (x2 - x1) / 2;

        const entry: PlacedEdge = {
          id: `${edge.from}->${node.id}`,
          from: edge.from,
          to: node.id,
          kind: edge.kind,
          d: `M ${x1} ${y1} C ${mid} ${y1} ${mid} ${y2} ${x2} ${y2}`,
          label: edge.label,
        };

        // 5.1px per character at 9px mono, plus the label's own padding. Approximate on
        // purpose: measuring text per frame costs more than the occasional dropped label.
        if (edge.label && density !== 'min' && x2 - x1 > 46) {
          const w = edge.label.length * 5.1 + 8;
          const x = mid - w / 2;
          const y = (y1 + y2) / 2 - 7;
          const rect = { x, y, w, h: 13 };
          const clash = occupied.some(
            (other) =>
              rect.x < other.x + other.w &&
              rect.x + rect.w > other.x &&
              rect.y < other.y + other.h &&
              rect.y + rect.h > other.y,
          );
          if (clash || w > x2 - x1) lost += 1;
          else {
            entry.at = { x: mid, y: (y1 + y2) / 2, w };
            occupied.push(rect);
          }
        } else if (edge.label) {
          lost += 1;
        }

        placed.push(entry);
      }
    }
    setEdges(placed);
    setDropped(lost);
  }, [shown, density]);

  useEffect(() => {
    measure();
    const container = containerRef.current;
    if (!container || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => measure());
    observer.observe(container);
    return () => observer.disconnect();
  }, [measure]);

  // A fixed min-height leaves a sparse column as a tall empty box. The plane is sized from
  // whichever column actually has the most nodes, so it is as tall as it needs to be and no
  // taller — which is what keeps it reading as a diagram rather than as padding.
  const busiest = Math.max(
    1,
    ...COLUMNS.map((_, index) => shown.filter((node) => node.column === index).length),
  );

  const selectedNode = shown.find((node) => node.id === selected) ?? null;
  const touches = (edge: PlacedEdge): boolean =>
    selected !== null && (edge.from === selected || edge.to === selected);

  /** Every relationship touching the selection, written out. Nothing here is abbreviated. */
  const relationships = useMemo(() => {
    if (!selected) return [];
    const rows: Array<{ id: string; from: string; to: string; why: string; kind: TopologyEdge['kind'] }> = [];
    for (const node of shown) {
      for (const edge of node.edges ?? []) {
        if (edge.from !== selected && node.id !== selected) continue;
        const from = shown.find((n) => n.id === edge.from);
        rows.push({
          id: `${edge.from}->${node.id}`,
          from: from?.name ?? edge.from,
          to: node.name,
          why: edge.why,
          kind: edge.kind,
        });
      }
    }
    return rows;
  }, [selected, shown]);

  return (
    <>
      <div
        className={styles.topo}
        ref={containerRef}
        data-columns={columns}
        data-density={density}
        style={{ '--rows': busiest } as React.CSSProperties}
      >
        <svg className={styles.svg} aria-hidden="true">
          {edges.map((edge) => {
            const lit = touches(edge);
            const dimmed = selected !== null && !lit;
            return (
              <g key={edge.id}>
                <path
                  className={[
                    styles.edge,
                    styles[edge.kind] ?? '',
                    lit ? styles.lit : '',
                    dimmed ? styles.dimmed : '',
                  ]
                    .filter(Boolean)
                    .join(' ')}
                  d={edge.d}
                />
                {edge.at && edge.label ? (
                  <>
                    <rect
                      className={`${styles.labelBg} ${lit ? styles.litBg : ''}`}
                      x={edge.at.x - edge.at.w / 2}
                      y={edge.at.y - 6.5}
                      width={edge.at.w}
                      height={13}
                      rx={2}
                      opacity={dimmed ? 0.22 : undefined}
                    />
                    <text
                      className={`${styles.label} ${lit ? styles.litLabel : ''}`}
                      x={edge.at.x}
                      y={edge.at.y}
                      textAnchor="middle"
                      opacity={dimmed ? 0.22 : undefined}
                    >
                      {edge.label}
                    </text>
                  </>
                ) : null}
              </g>
            );
          })}
        </svg>

        <div className={styles.columns}>
          {COLUMNS.slice(firstColumn).map((columnName, offset) => {
            const index = firstColumn + offset;
            const columnNodes = shown.filter((node) => node.column === index);
            return (
              <div className={styles.column} key={columnName}>
                <div className={styles.columnHead}>{columnName}</div>
                <div className={styles.stack}>
                  {columnNodes.length === 0 ? (
                    <p className={styles.emptyColumn}>nothing observed</p>
                  ) : (
                    columnNodes.map((node) => (
                      <Node
                        key={node.id}
                        node={node}
                        selected={node.id === selected}
                        dimmed={selected !== null && node.id !== selected}
                        onSelect={() => setSelected(node.id === selected ? null : node.id)}
                      />
                    ))
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* The evidence strip. Present whether or not anything is selected, because a strip
          that appears on selection moves the diagram every time it is used. */}
      <div className={styles.evidence}>
        {selectedNode ? (
          <>
            <span className={styles.what}>{selectedNode.name}</span>
            <span className={styles.rel}>{selectedNode.kind}</span>
            <span className={styles.evidenceText}>{selectedNode.evidence ?? 'No further evidence is published for this node.'}</span>
            <span className={styles.spacer} />
            {selectedNode.sources && selectedNode.sources.length > 0 ? (
              <span className={styles.sources}>{selectedNode.sources.join(' · ')}</span>
            ) : null}
            {selectedNode.to ? (
              <Link className={styles.open} to={selectedNode.to}>
                Open ›
              </Link>
            ) : null}
          </>
        ) : (
          <span className={styles.evidenceIdle}>
            Select anything in the plane to see what proves it.
            {dropped > 0 ? (
              <>
                {' '}
                <b>{dropped}</b> edge label{dropped === 1 ? '' : 's'} did not fit and{' '}
                {dropped === 1 ? 'was' : 'were'} omitted; every relationship is written out
                below when selected.
              </>
            ) : null}
          </span>
        )}
      </div>

      {/* The relationship fallback: the one place an edge is stated in full. At narrow
          widths, where labels are dropped wholesale, this is the diagram's legend. */}
      {relationships.length > 0 ? (
        <div className={styles.relationships}>
          {relationships.map((row) => (
            <div
              key={row.id}
              className={`${styles.relationship} ${row.kind === 'drift' ? styles.driftRow : ''}`}
            >
              <span className={styles.relEnd}>{row.from}</span>
              <span className={styles.arrow} aria-hidden="true">
                ─→
              </span>
              <span className={styles.relEnd}>{row.to}</span>
              <span className={styles.why}>{row.why}</span>
            </div>
          ))}
        </div>
      ) : null}
    </>
  );
}

function Node({
  node,
  selected,
  dimmed,
  onSelect,
}: {
  node: TopologyNode;
  selected: boolean;
  dimmed: boolean;
  onSelect: () => void;
}): JSX.Element {
  return (
    <div
      className={[styles.nodeWrap, dimmed ? styles.nodeDimmed : ''].filter(Boolean).join(' ')}
      data-node={node.id}
    >
      <button
        type="button"
        className={[
          styles.node,
          node.id === 'host' ? styles.host : '',
          node.drifted ? styles.drifted : '',
          selected ? styles.selected : '',
        ]
          .filter(Boolean)
          .join(' ')}
        aria-pressed={selected}
        onClick={onSelect}
      >
        <span className={styles.nodeTop}>
          <StatusDot semantic={node.mark} />
          <span className={styles.nodeName}>{node.name}</span>
          <span className={styles.nodeKind}>{node.kind}</span>
        </span>
        {node.detail ? <span className={styles.nodeDetail}>{node.detail}</span> : null}
        {node.note ? <span className={styles.nodeNote}>{node.note}</span> : null}
        {node.facts && node.facts.length > 0 ? (
          <span className={styles.nodeFacts}>
            {node.facts.map((fact) => (
              <span key={fact}>{fact}</span>
            ))}
          </span>
        ) : null}
      </button>

      {node.ports && node.ports.length > 0 ? (
        <div className={styles.ports}>
          <div className={styles.portHead}>
            <span>{node.portsLabel ?? 'attachments'}</span>
            <span className={styles.spacer} />
            <span>address</span>
          </div>
          {node.ports.map((port) => (
            <Link key={port.objectId} className={styles.port} to={`/network/${port.objectId}`}>
              <span className={`${styles.portDot} ${styles[`dot_${port.tone}`] ?? ''}`} />
              <span className={styles.portName}>{port.name}</span>
              {port.drifted ? (
                <span className={styles.portDrift} title="drifted from its intent">
                  ≠
                </span>
              ) : null}
              {/* The sparkline slot. Reserved and empty on purpose: LocalPlane keeps no
                  traffic history, so there is no series to draw here and nothing is drawn.
                  The space is held so a real series can arrive without moving the row. */}
              <span
                className={styles.spark}
                aria-hidden="true"
                title="No traffic history is kept, so there is no line to draw."
              />
              <span className={styles.portValue}>{port.detail}</span>
            </Link>
          ))}
        </div>
      ) : null}
    </div>
  );
}
