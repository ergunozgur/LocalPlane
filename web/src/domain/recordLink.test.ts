import { describe, expect, it } from 'vitest';
import { changeUrl, findingUrl, runUrl } from './recordLink';

describe('record links', () => {
  it.each([
    ['run', 'run/id ?#%', '/operations/runs/run%2Fid%20%3F%23%25'],
    ['change', 'change/id ?#%', '/operations/changes/change%2Fid%20%3F%23%25'],
    ['finding', 'finding/id ?#%', '/operations/findings/finding%2Fid%20%3F%23%25'],
  ] as const)('encodes a %s identifier once with all reserved characters', (kind, id, expected) => {
    const url = kind === 'run' ? runUrl(id) : kind === 'change' ? changeUrl(id) : findingUrl(id);
    expect(url).toBe(expected);
  });
});
