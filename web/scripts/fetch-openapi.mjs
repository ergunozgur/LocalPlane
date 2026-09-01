#!/usr/bin/env node
/**
 * Refresh the committed OpenAPI snapshot from a running backend.
 *
 * The snapshot is committed so that type generation, CI and a clean checkout never require a
 * live backend. This script is how the snapshot is updated when the backend contract moves.
 *
 *   LOCALPLANE_API_ORIGIN=http://127.0.0.1:8080 npm run api:snapshot && npm run api:types
 */
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const origin = process.env.LOCALPLANE_API_ORIGIN ?? 'http://127.0.0.1:8080';
const bearer = process.env.LOCALPLANE_API_BEARER;
const target = join(dirname(dirname(fileURLToPath(import.meta.url))), 'openapi.json');

if (!bearer) {
  console.error('LOCALPLANE_API_BEARER is required to read the protected OpenAPI document');
  process.exit(1);
}

const response = await fetch(new URL('/openapi.json', origin), {
  headers: { authorization: `Bearer ${bearer}` },
});
if (!response.ok) {
  console.error(`GET ${origin}/openapi.json -> ${response.status}`);
  process.exit(1);
}
const document = await response.json();
writeFileSync(target, `${JSON.stringify(document, null, 2)}\n`);

const paths = Object.keys(document.paths ?? {}).length;
const schemas = Object.keys(document.components?.schemas ?? {}).length;
console.log(`wrote ${target} — ${paths} paths, ${schemas} schemas`);
