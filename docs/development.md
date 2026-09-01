# Development

[Back to the repository overview](../README.md)

LocalPlane is a pre-release Python backend/agent with a React/Vite Operator Console. The
commands below are local development workflows, not production deployment instructions.

## Requirements

- Linux
- Python 3.11 or newer
- Node.js `^18.0.0`, `^20.0.0`, or `>=22.0.0` for the locked Vite/Vitest toolchain
- npm with the committed lockfile
- Optional access to Docker and systemd for those provider capabilities

Live tests and provider access can inspect or change a real host. Run only the explicitly
selected command whose scope you have reviewed.

## Backend and agent setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Create the restrictive default secret directory and initialize the master credential once:

```sh
install -d -m 700 var
.venv/bin/localplane-auth init
```

The initializer refuses overwrite and prints the new credential only on successful creation.
Normal backend startup never generates a replacement and fails closed if the configured file is
missing or unsafe.

Start the agent:

```sh
.venv/bin/localplane-agent
```

Start the backend in another terminal:

```sh
.venv/bin/localplane-backend
```

The backend defaults to `127.0.0.1:8080`. The agent socket defaults to
`$XDG_RUNTIME_DIR/localplane/agent.sock`.

Common settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LOCALPLANE_AGENT_SOCKET` | `$XDG_RUNTIME_DIR/localplane/agent.sock` | Agent/backend Unix socket |
| `LOCALPLANE_AGENT_TIMEOUT_S` | `10` | Backend timeout for an agent request |
| `LOCALPLANE_DB_PATH` | `var/localplane.db` | SQLite store |
| `LOCALPLANE_AUTH_SECRET_PATH` | `var/localplane-master.secret` | Restrictive local master-credential file |
| `LOCALPLANE_DEVELOPMENT_ORIGIN` | none | Exactly one additional Vite development Origin when required |
| `LOCALPLANE_HOST` | `127.0.0.1` | Backend bind address |
| `LOCALPLANE_PORT` | `8080` | Backend port |
| `LOCALPLANE_FRESHNESS_TTL_S` | `60` | Observation freshness horizon |
| `LOCALPLANE_OBSERVE_ON_STARTUP` | `1` | Take initial observations when the backend starts |
| `LOCALPLANE_DOCKER_SOCKET` | `/var/run/docker.sock` | Docker Engine Unix socket used by the agent |
| `LOCALPLANE_LOG_LEVEL` | `INFO` | Process log level |

Do not change `LOCALPLANE_HOST` to a non-loopback address as a substitute for TLS. Authentication
is implemented, but remote/non-loopback plain-HTTP browser sessions are deliberately refused.

## Optional privileged helper

The helper is required only for the MTU mutation. Start it as a module; there is deliberately
no console-script entry point:

```sh
.venv/bin/python -m localplane.helper
```

Its default socket is `/run/localplane/helper.sock`. A privileged deployment must explicitly
allow the unprivileged agent's UID or GID:

- `LOCALPLANE_HELPER_ALLOW_UID`
- `LOCALPLANE_HELPER_ALLOW_GID`

An empty allowlist refuses every peer. A root-started helper with neither variable set allows
root only, which normally means the unprivileged agent cannot use it. Review the privilege
boundary before enabling this component.

## Operator Console

Use the lockfile and run from `web/`:

```sh
cd web
npm ci
npm run dev
```

The development server listens on `http://127.0.0.1:5178` and proxies `/api` to
`http://127.0.0.1:8080` by default. Override only the development target when needed:

```sh
LOCALPLANE_API_ORIGIN=http://127.0.0.1:8080 npm run dev
```

The frontend always calls `/api/v1` on its own origin. There is no build-time API base URL.

## Validation

Backend non-live suite:

```sh
.venv/bin/python -m pytest -m 'not live'
```

Frontend checks:

```sh
cd web
npm test
npm run typecheck
npm run lint
npm run build
```

Tests marked `live` interact with real host providers and are excluded by the command above.
Do not broaden the selection casually, especially on a host whose state is not disposable.

## OpenAPI and generated types

`web/openapi.json` is a committed snapshot. `web/src/api/schema.d.ts` is generated from it.
With the intended backend running on loopback:

```sh
cd web
LOCALPLANE_API_ORIGIN=http://127.0.0.1:8080 \
  LOCALPLANE_API_BEARER='<master-secret>' npm run api:snapshot
npm run api:types
```

Both files are generated artifacts and should be refreshed and reviewed together. Contract
generation contacts the protected live backend, requires an explicit master Bearer credential,
and writes repository files. The script never writes the credential to either generated artifact;
do not use it as a read-only inspection command.

## Repository layout

```text
src/localplane/
  protocol/   shared closed wire vocabulary
  ipc.py      bounded Unix-socket framing and peer checks
  agent/      host observation and provider operations
  backend/    API, policy, persistence, Runs, Changes, verification
  helper/     narrowly privileged MTU executor
tests/        unit, integration, and explicitly marked live coverage
web/          React/Vite Operator Console and generated API contract
docs/         product and technical documentation
```

## Contribution expectations

- Prefer authoritative platform APIs and maintained libraries over command-output parsing or
  bespoke protocol implementations.
- Keep provider-specific semantics typed; do not add a generic execution escape hatch.
- Preserve `unknown` when evidence is incomplete.
- Keep capability, authorization, protection, verification, and recovery separate.
- Add numbered migrations rather than rewriting committed migrations.
- Treat generated contracts, third-party notices, and live-test boundaries as reviewed
  release surfaces.
