/**
 * Reading systemd's own facts.
 *
 * The manager reports its timestamps as microseconds since the epoch, and uses `0` for
 * "never" rather than a null. Both conventions are handled here so no surface renders 1970
 * or reads a zero as a time.
 *
 * The design direction's Services table also carries MEMORY and TASKS columns.
 * `MemoryCurrent` and `TasksCurrent` are not in this build's unit contract, so they are
 * absent from this module entirely rather than present and always unknown.
 */
import type { SystemdUnit } from '@/api/types';
import { formatRelative } from './format';

/** systemd stores `0` for a timestamp that never happened. */
export function systemdTime(micros: number | null | undefined): string | null {
  if (micros === null || micros === undefined || micros === 0) return null;
  return new Date(micros / 1000).toISOString();
}

/** How long the unit has held its current active state. */
export function activeSince(unit: SystemdUnit): string | null {
  const entered = systemdTime(unit.timestamps['active_enter_timestamp']);
  if (!entered) return null;
  const relative = formatRelative(entered);
  return relative ? relative.replace(' ago', '') : null;
}

/**
 * A restrained note about a unit, from fields the manager actually reports.
 *
 * The design direction writes things like "restarted 2× in 24 h · signal 11". The count is
 * real (`n_restarts`); the *window* is not — nothing reports a rate — so the note says how
 * many times without implying over what period.
 */
export function unitNote(unit: SystemdUnit): string | null {
  const notes: string[] = [];
  const service = unit.service;

  if (service?.n_restarts) {
    notes.push(`restarted ${service.n_restarts}×`);
  }
  if (service?.result && service.result !== 'success') {
    notes.push(`result ${service.result}`);
  }
  if (unit.need_daemon_reload) {
    notes.push('needs daemon reload');
  }
  if (unit.socket?.refused) {
    notes.push(`${unit.socket.refused} refused`);
  }
  return notes.length > 0 ? notes.join(' · ') : null;
}
