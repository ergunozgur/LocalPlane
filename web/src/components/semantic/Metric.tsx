/**
 * Meters and chart shells.
 *
 * The design direction is full of measurement: `.bar` meters in table rows, sparklines
 * in port rows, and `.chart` panels with axes, gridlines and a hover readout. This build
 * has **no time-series
 * contract of any kind** — no host metrics, no throughput history, no load or memory series.
 *
 * The structure is kept anyway, and this is the deliberate part. A console that silently
 * omits every chart teaches an operator that LocalPlane does not measure things; a console
 * that draws a chart from nothing teaches them something worse. So the shell is rendered,
 * with its frame and its axis, and it says in words what is missing and what would fill it.
 * When a series arrives, the shell takes it and nothing above it changes.
 *
 * A meter, by contrast, is drawn only where a real proportion was published. There is no
 * such thing as an honest empty meter — a bar at zero is a measurement — so an absent
 * proportion renders as a stated gap instead.
 */
import type { ReactNode } from 'react';
import styles from './Metric.module.css';

export type MeterTone = 'good' | 'warn' | 'bad' | 'neutral';

/**
 * A proportion bar.
 *
 * `percent === null` means the value was not read. It renders as a hatched track rather than
 * an empty one: an empty bar and a zero bar look identical, and they are opposite claims.
 */
export function Meter({
  percent,
  tone = 'neutral',
  wide = false,
  thin = false,
  /** Positions (0–100) to mark on the track — a limit, a threshold, a previous value. */
  ticks,
  label,
}: {
  percent: number | null;
  tone?: MeterTone;
  wide?: boolean;
  thin?: boolean;
  ticks?: readonly number[];
  label?: string;
}): JSX.Element {
  const classes = [styles.bar, wide ? styles.wide : '', thin ? styles.thin : '']
    .filter(Boolean)
    .join(' ');

  if (percent === null) {
    return (
      <span
        className={`${classes} ${styles.unread}`}
        role="img"
        aria-label={label ? `${label}: not read` : 'not read'}
        title="This proportion was not read. An empty bar would say zero, which is a different claim."
      />
    );
  }

  const clamped = Math.max(0, Math.min(100, percent));
  return (
    <span
      className={classes}
      role="img"
      aria-label={`${label ? `${label}: ` : ''}${clamped.toFixed(1)} percent`}
    >
      <i className={styles[tone]} style={{ width: `${clamped}%` }} />
      {ticks?.map((at) => (
        <span key={at} className={styles.tick} style={{ left: `${Math.max(0, Math.min(100, at))}%` }} />
      ))}
    </span>
  );
}

/** A metric row: a label, a meter, the number, and what qualifies it. */
export function MetricRow({
  label,
  percent,
  tone,
  value,
  qualifier,
  ticks,
}: {
  label: string;
  percent: number | null;
  tone?: MeterTone;
  value: ReactNode;
  qualifier?: ReactNode;
  ticks?: readonly number[];
}): JSX.Element {
  return (
    <div className={styles.row}>
      <span className={styles.label}>{label}</span>
      <Meter percent={percent} {...(tone ? { tone } : {})} {...(ticks ? { ticks } : {})} label={label} />
      <span className={styles.value}>{value}</span>
      {qualifier ? <span className={styles.qualifier}>{qualifier}</span> : null}
    </div>
  );
}

export interface Series {
  id: string;
  label: string;
  tone: MeterTone;
  /** Ordered points. An empty array is a series that was read and had nothing in it. */
  points: ReadonlyArray<{ at: string; value: number }>;
}

/**
 * A chart, or an honest statement of why there is not one.
 *
 * `series === null` is "nobody kept this" — the state every chart in this build is in — and
 * is distinct from a series that exists and is empty, which is "we looked and there was
 * nothing". `absence` says which, in the product's voice, and `wouldFill` names the contract
 * that would make the chart real.
 */
export function ChartShell({
  title,
  unit,
  series,
  absence,
  wouldFill,
  height = 96,
}: {
  title: string;
  unit?: string;
  series: readonly Series[] | null;
  absence: string;
  wouldFill?: string;
  height?: number;
}): JSX.Element {
  const drawable = series?.filter((line) => line.points.length > 1) ?? [];

  if (drawable.length === 0) {
    return (
      <figure className={styles.chart} style={{ minHeight: height }}>
        <figcaption className={styles.chartHead}>
          <span className={styles.chartTitle}>{title}</span>
          {unit ? <span className={styles.chartUnit}>{unit}</span> : null}
        </figcaption>
        {/* The frame is drawn even with nothing in it, so the shape of the missing thing is
            visible and a reader can see that a chart belongs here. */}
        <div className={styles.chartEmpty} style={{ height }}>
          <svg className={styles.chartGrid} aria-hidden="true" preserveAspectRatio="none">
            <line x1="0" y1="1" x2="100%" y2="1" className={styles.gridline} />
            <line x1="0" y1="50%" x2="100%" y2="50%" className={styles.gridline} />
            <line x1="0" y1="99%" x2="100%" y2="99%" className={`${styles.gridline} ${styles.base}`} />
          </svg>
          <p className={styles.absence}>
            {absence}
            {wouldFill ? <span className={styles.wouldFill}>{wouldFill}</span> : null}
          </p>
        </div>
      </figure>
    );
  }

  const all = drawable.flatMap((line) => line.points.map((point) => point.value));
  const max = Math.max(...all, 0);
  const min = Math.min(...all, 0);
  const span = max - min || 1;

  return (
    <figure className={styles.chart}>
      <figcaption className={styles.chartHead}>
        <span className={styles.chartTitle}>{title}</span>
        {unit ? <span className={styles.chartUnit}>{unit}</span> : null}
      </figcaption>
      <svg className={styles.chartSvg} viewBox={`0 0 100 ${height}`} preserveAspectRatio="none" height={height}>
        <line x1="0" y1={height - 1} x2="100" y2={height - 1} className={`${styles.gridline} ${styles.base}`} />
        {drawable.map((line) => (
          <polyline
            key={line.id}
            className={`${styles.line} ${styles[line.tone] ?? ''}`}
            points={line.points
              .map((point, index) => {
                const x = (index / (line.points.length - 1)) * 100;
                const y = height - ((point.value - min) / span) * (height - 2) - 1;
                return `${x},${y}`;
              })
              .join(' ')}
          />
        ))}
      </svg>
      <div className={styles.legend}>
        {drawable.map((line) => (
          <span key={line.id} className={styles.legendItem}>
            <i className={styles[line.tone] ?? ''} />
            {line.label}
          </span>
        ))}
      </div>
    </figure>
  );
}
