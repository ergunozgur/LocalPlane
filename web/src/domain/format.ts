/**
 * Value formatting.
 *
 * One rule governs this file: **`null` is a fact about knowledge, not a value.** A null MTU
 * is not 0, a null carrier is not "no", and a null speed is not "unknown speed" — it is the
 * kernel declining to answer. Every formatter here returns `null` for a null input and lets
 * the presentation layer render an explicit unknown, so a missing fact can never be styled
 * like a negative one.
 */

export function formatBytes(value: number | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  if (value === 0) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  const exponent = Math.min(Math.floor(Math.log(Math.abs(value)) / Math.log(1024)), units.length - 1);
  const scaled = value / 1024 ** exponent;
  const unit = units[exponent] ?? 'B';
  return `${scaled.toFixed(exponent === 0 ? 0 : scaled >= 100 ? 0 : 1)} ${unit}`;
}

export function formatCount(value: number | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  return value.toLocaleString();
}

/** A duration in seconds, rendered at the coarsest useful precision. */
export function formatDuration(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined) return null;
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

/** How long ago, phrased for a glance. Never rounds an age down into "now". */
export function formatAge(seconds: number | null | undefined): string | null {
  const rendered = formatDuration(seconds);
  return rendered === null ? null : `${rendered} ago`;
}

export function parseTimestamp(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** Absolute time, in the operator's locale, to the second. Evidence deserves precision. */
export function formatTimestamp(value: string | null | undefined): string | null {
  const date = parseTimestamp(value);
  if (!date) return null;
  return date.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

export function formatRelative(value: string | null | undefined, now: Date = new Date()): string | null {
  const date = parseTimestamp(value);
  if (!date) return null;
  return formatAge((now.getTime() - date.getTime()) / 1000);
}

/** systemd reports its timestamps as microseconds since the epoch; 0 means "never". */
export function formatSystemdTimestamp(micros: number | null | undefined): string | null {
  if (micros === null || micros === undefined || micros === 0) return null;
  return formatTimestamp(new Date(micros / 1000).toISOString());
}

/**
 * A boolean fact, where `null` stays null.
 *
 * Deliberately not `Boolean(value)` anywhere in this codebase: that would turn "the kernel
 * refused the read" into "false".
 */
export function formatBoolean(
  value: boolean | null | undefined,
  words: { yes: string; no: string } = { yes: 'yes', no: 'no' },
): string | null {
  if (value === null || value === undefined) return null;
  return value ? words.yes : words.no;
}

/**
 * A value as the write boundary models one.
 *
 * Field changes carry booleans and integers; actions carry a state name, so `expected_after`
 * is widened to include strings. All three render as themselves, and only absence is absent.
 */
export function formatTypedValue(
  value: boolean | number | string | null | undefined,
): string | null {
  if (value === null || value === undefined) return null;
  // Rendered exactly as the backend holds it, with no grouping separator. These are
  // configuration values, not magnitudes: an MTU shown as "1,500" reads as a different
  // number from the one an operator would type, and this is the value a write is compared
  // against. `formatCount` is the one for quantities.
  return String(value);
}

/** Trim a long identifier for display while keeping both ends, which are what differ. */
export function abbreviateId(value: string, keep = 8): string {
  if (value.length <= keep * 2 + 1) return value;
  return `${value.slice(0, keep)}…${value.slice(-4)}`;
}
