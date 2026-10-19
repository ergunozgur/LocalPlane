// @vitest-environment node
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { execFileSync } from 'node:child_process';
import { expect, it } from 'vitest';

it.each(['integer', 'integral-float'])('snapshot script produces exact committed bytes from %s bounds', (representation) => {
  const committed = readFileSync(resolve('openapi.json'), 'utf8');
  const input = representation === 'integer' ? committed : committed.replace(
    /("(?:exclusiveMaximum|exclusiveMinimum|minimum)": )(9007199254740992|0)(?=,|\n)/g,
    '$1$2.0',
  );
  if (representation === 'integral-float') expect(input).not.toBe(committed);
  const scratch = mkdtempSync(join(tmpdir(), 'openapi-snapshot-'));
  let passed = false;
  try {
    mkdirSync(join(scratch, 'scripts'));
    // Execute the unchanged production script; only fetch is replaced. Its relative
    // output stays in scratch, never overwriting the committed file during tests.
    const script = join(scratch, 'scripts', 'fetch-openapi.mjs');
    copyFileSync(resolve('scripts/fetch-openapi.mjs'), script);
    execFileSync(process.execPath, ['--input-type=module', '-e', `
      import { readFileSync } from 'node:fs';
      import { pathToFileURL } from 'node:url';
      import assert from 'node:assert/strict';
      const body = readFileSync(0, 'utf8');
      globalThis.fetch = async (url, options) => {
        assert.equal(url.href, 'http://snapshot.invalid/openapi.json');
        assert.equal(options.headers.authorization, 'Bearer synthetic-snapshot-test');
        return new Response(body, { status: 200 });
      };
      await import(pathToFileURL(process.argv[1]).href);
    `, script], {
      input, timeout: 10000,
      env: { ...process.env, LOCALPLANE_API_ORIGIN: 'http://snapshot.invalid',
        LOCALPLANE_API_BEARER: 'synthetic-snapshot-test' },
    });
    expect(readFileSync(join(scratch, 'openapi.json'), 'utf8')).toBe(committed);
    passed = true;
  } finally {
    // Keep failed evidence; successful tests remove only their own directory.
    if (passed) rmSync(scratch, { recursive: true, force: true });
  }
});
