/**
 * Encoded paths for durable operational records.
 *
 * These are the canonical Run / Change / Finding detail URLs used by Search,
 * the machine-record rail, and Composer. Each helper encodes the identifier
 * exactly once and returns a ready-to-navigate path; callers do not decode or
 * re-encode.
 */

export function runUrl(runId: string): string {
  return `/operations/runs/${encodeURIComponent(runId)}`;
}

export function changeUrl(changeId: string): string {
  return `/operations/changes/${encodeURIComponent(changeId)}`;
}

export function findingUrl(findingId: string): string {
  return `/operations/findings/${encodeURIComponent(findingId)}`;
}
