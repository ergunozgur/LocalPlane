# LocalPlane Operator Console

[Back to the repository overview](../README.md)

The read-only browser interface for LocalPlane, a local-first operations control plane for
understanding Linux systems and making guarded, evidence-backed changes.

**Observe everything. Manage what you choose.**

> [!IMPORTANT]
> **Experimental and pre-release.** The console covers a subset of the product direction.
> Interfaces, data shapes, and layout may change.

> [!WARNING]
> The console requires a derived browser session. Plain-HTTP browser sessions are supported only
> for the configured loopback development topology; production TLS and asset serving remain
> unresolved.

## Current behavior

The console presents stored and explicitly sampled backend data for host overview,
networking, workloads, systemd, topology, Runs, Changes, and findings. It preserves evidence,
freshness, and `unknown` values instead of filling gaps with fallback data.

It is deliberately **read-only**:

- the API adapter binds no record-writing or host-writing endpoint;
- it renders no Start, Stop, Restart, Apply, Confirm, Adopt, Release, or intent-revision
  control;
- `OPERATIONS_WITH_UI_CONTROLS` is empty;
- recovery actions are displayed as records, never as controls.

The backend implements seven typed host-mutation operations. Authentication is now enforced,
but the console still does not expose those operations: authentication does not grant frontend
write authority or bypass the Change Engine.

The committed OpenAPI snapshot and generated types are integrated with the backend in this
tree, including systemd lifecycle planning and execution records. Contract availability does
not make a console control exist.

## Current surfaces

- **Overview** — host identity, estate counts, operational summary, attention, and recent
  records.
- **Network** — interface list and detail with addresses, state, ownership, provenance,
  intent, reconciliation, protection, and route evidence.
- **Workloads** — Docker container list/detail, provider-derived Compose-project grouping, bounded
  logs, and on-demand current statistics.
- **System** — bounded loaded systemd units with state, enablement, relationships, and typed
  service/socket/timer detail.
- **Operations** — Runs, Changes, and findings with detail views and backend-backed filters.
- **Topology** — evidence-backed network and container relationships inside the overview.
- **Settings** — appearance preferences, authenticated-session truth, and no invented user identity.

Some frames intentionally state that data is unavailable: host resource charts have no
metrics contract, network traffic has no time series, and Docker runtime information does
not include a system-info contract. These are not zero values or simulated data.

## Not implemented

- Users, roles, RBAC, named identity, or persistent/multi-process browser sessions
- Record-writing or host-writing controls
- A production static-asset and TLS serving topology
- Polling or a claim that a one-time read is live
- Historical replay controls
- General host metrics, unified logs, storage, packages, users, or processes
- Search, dashboard editing, terminal access, fleet operation, or automation

The V4.2 image in the repository overview is product/interface direction, not a capture of
this application and not a current feature matrix.

## Backend relationship

The console is a client of the LocalPlane HTTP API. It has no direct connection to the
agent, helper, Docker, systemd, or the host.

It calls `/api/v1` on its own origin. Because the backend registers no CORS middleware, the
Vite development server proxies `/api` to the backend without rewriting the request origin.
The proxy is a development convenience, not a security boundary.

Container logs and current statistics use `POST` because the backend declares those
on-demand provider reads that way. They do not write the host or LocalPlane records.

## Requirements

- Node.js `^18.0.0`, `^20.0.0`, or `>=22.0.0`
- npm with the committed lockfile
- A running loopback LocalPlane backend with an initialized master credential

## Quick start

Initialize authentication and start the backend as described in the repository quick start.
Then, from `web/`:

```sh
npm ci
npm run dev
```

On first load, enter the master credential. The console sends it only in the Bearer header of the
session-exchange request, clears its React input state after submission, and thereafter uses the
derived `HttpOnly` cookie. It does not store the master in browser storage.

The development server listens on `http://127.0.0.1:5178`. It proxies `/api` to
`http://127.0.0.1:8080` by default:

```sh
LOCALPLANE_API_ORIGIN=http://127.0.0.1:8080 npm run dev
```

Build production assets:

```sh
npm run build
```

Output is written to `web/dist`. Building assets does not establish a production serving or
security topology.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `LOCALPLANE_API_ORIGIN` | `http://127.0.0.1:8080` | Backend origin proxied by the development server and read by `api:snapshot` |
| `LOCALPLANE_API_BEARER` | none | Required master Bearer credential for `api:snapshot`; never written to generated files |

There is no build-time API-base setting. Production code calls `/api/v1` on the origin that
served it.

## Development commands

| Command | Purpose |
| --- | --- |
| `npm run dev` | Start the development server |
| `npm run build` | Type-check and build production assets |
| `npm run preview` | Serve built assets locally for inspection |
| `npm run typecheck` | Run the full TypeScript project check |
| `npm run lint` | Run ESLint |
| `npm test` | Run the test suite once |
| `npm run test:watch` | Run tests in watch mode |
| `npm run api:snapshot` | Replace the committed OpenAPI snapshot from a running backend |
| `npm run api:types` | Regenerate TypeScript declarations from the snapshot |

## OpenAPI and generated types

`openapi.json` is the committed backend contract snapshot. `src/api/schema.d.ts` is generated
from it with `openapi-typescript`, allowing a clean checkout and CI to type-check without a
running backend.

To refresh both against the intended loopback backend:

```sh
LOCALPLANE_API_ORIGIN=http://127.0.0.1:8080 \
  LOCALPLANE_API_BEARER='<master-secret>' npm run api:snapshot
npm run api:types
```

Both commands write generated files. Refresh and review the pair together whenever the
backend contract changes.

## Project structure

```text
web/
  src/
    api/           request boundary, endpoint adapter, and generated schema
    components/    layout, primitives, object workspaces, and semantic views
    dashboard/     overview grid and density model
    domain/        vocabulary, formatting, and execution presentation
    hooks/         request state and layout hooks
    auth/          authenticated boot, login, expiry, and logout boundary
    identity/      no-user authenticated attribution seam
    preferences/   appearance preferences
    routes/        domain pages and object detail routes
    styles/        design tokens and base styles
    assets/fonts/  self-hosted third-party webfonts
  openapi.json     committed backend contract snapshot
  scripts/         contract refresh tooling
```

## Security and licences

The console establishes a browser session but adds no user, role, or client-side authorization
model. Authentication remains separate from the backend's operation-safety and privilege
boundaries. Plain HTTP remains loopback-development-only; serving built assets does not create a
production TLS or remote-exposure topology. Read the repository [safety model](../docs/safety-model.md)
before changing deployment.

LocalPlane is licensed under the [Apache License 2.0](../LICENSE). The bundled Inter,
JetBrains Mono, and Instrument Serif files, their immutable sources, hashes, copyrights, and
licence text are recorded in [NOTICE](../NOTICE).
