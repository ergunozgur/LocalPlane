import type { StorageCounters } from '@/api/types';

/** Preserve readable path distinctions without interpreting control/bidi characters. */
export function displayPath(value: string): string {
  return [...value].map((character) => {
    if (character === '\\') return '\\\\';
    if (character === '\n') return '\\n';
    if (character === '\t') return '\\t';
    const code = character.charCodeAt(0);
    if (code < 32 || code === 127 || (code >= 0x202a && code <= 0x202e) || (code >= 0x2066 && code <= 0x2069)) {
      return `\\u${code.toString(16).padStart(4, '0')}`;
    }
    return character;
  }).join('');
}

export function availablePercent(counters: StorageCounters | null): number | null {
  if (!counters || BigInt(counters.total) === 0n) return null;
  return Number(BigInt(counters.available) * 10000n / BigInt(counters.total)) / 100;
}

export function size(value: string | undefined): string {
  if (value === undefined) return 'Unknown';
  const number = BigInt(value);
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB'];
  let unit = 1n;
  let index = 0;
  while (number >= unit * 1024n && index < units.length - 1) { unit *= 1024n; index++; }
  return `${Number(number * 10n / unit) / 10} ${units[index]}`;
}
