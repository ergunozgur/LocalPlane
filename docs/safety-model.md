# Safety model

[Back to the repository overview](../README.md)

LocalPlane's safety model is built around closed operations, explicit authority, fresh
evidence, truthful uncertainty, and a durable record of what can and cannot be proved.

## Current write surface

The backend exposes many record and observation routes, but only two HTTP endpoints can
result in a host write:

- `POST /api/v1/runs/{run_id}/apply`
- `POST /api/v1/changes/{change_id}/recovery/retry`

Neither accepts a body. The target, operation, desired end state, provider, and evidence are
already fixed by the Run or Change. There is no generic execute route.

Seven operation types dispatch through three provider mechanisms: one MTU operation through
the helper, three container lifecycle actions through Docker, and three service lifecycle
actions through systemd.

## Layered gates

```mermaid
flowchart TD
    Request[Typed operation request] --> Plan[Plan against fresh evidence]
    Plan --> Capability{Mechanism available?}
    Capability -->|no or unknown| Refuse[Publish blocker]
    Capability -->|yes| Ownership{Authority respected?}
    Ownership -->|no or unknown| Refuse
    Ownership -->|yes| Protection{Protection established?}
    Protection -->|blocked or unknown| Refuse
    Protection -->|clear or guarded path| Confirm[Required confirmation]
    Confirm --> Dispatch[Closed provider dispatch]
    Dispatch --> Observe[Fresh ordinary observation]
    Observe --> Verify{End state proved?}
    Verify -->|yes| Success[Verified result]
    Verify -->|no or unknown| Recovery[Rollback, guard, or recovery hold]
```

Capability, authorization, ownership, protection, confirmation, and verification answer
different questions. Availability never implies permission; authentication never proves an
operation is safe; and an accepted mutation never proves the intended result.

## Closed operations and privilege

Every operation name and target shape is a closed vocabulary. Caller input cannot select a
shell command, executable, Docker endpoint, D-Bus method, unit object path, netlink field,
or privileged-helper primitive.

The helper exposes only interface MTU mutation and validates Unix peer credentials before
parsing. systemd lifecycle uses the Unit object's `Start`, `Stop`, or `Restart` method with
the mode fixed internally to `fail`; PolicyKit decides authorization at dispatch. Docker
lifecycle uses the Engine API and a validated container identity.

Docker socket access remains a broad host-authority boundary in a rootful deployment. The
narrow helper does not reduce the authority already carried by that socket.

## Management-path protection

For an operator connection, LocalPlane derives the accepted TCP peer and local endpoint from
the socket itself. It does not trust forwarded headers, body fields, or query parameters as
connection evidence. A kernel route query and current observations must agree on the object
carrying that path.

If the path is unresolved, protection is `unknown` for every candidate object and execution
stays blocked. Absence of proof is never treated as proof that an object is off the path.

An eligible MTU change on the proven management interface may use a connection guard. The
agent arms a bounded host-side reversal before the write. A later request must arrive over
the changed path to keep the change; otherwise the guard attempts its compare-and-set
reversal. Only fresh evidence can prove the final state.

Systemd lifecycle planning also evaluates whether a service's bounded effect graph contains
the operator path, the LocalPlane agent, or the backend runtime. Partial, changing, or
unsupported graph evidence stays `unknown` and blocks. A narrowly defined, single-use
self-impact override exists only for one proven backend interruption hazard; it does not
rewrite protection truth or substitute for the ordinary confirmation.

The graph records bounded *potential* effects for the requested job kind, not a promise of
the transaction systemd will ultimately run. Start/restart may pull in starts, requisite
checks, or conflict stops; stop/restart may propagate genuine reverse stop/restart effects.
It is not a universal inverse-dependency closure. Conditional handlers, queued work,
release-driven stopping, and other incomplete runtime evidence remain gaps that block rather
than clearing a real management-path or agent coupling.

Container lifecycle does not yet prove whether a particular container carries the
operator's route. This is an unresolved safety limitation, separate from authentication.

## Verification and uncertain outcomes

Dispatch completion is evidence, not verified success. LocalPlane observes through the
ordinary provider path after a mutation:

- MTU reconciliation verifies the intended value;
- container start/stop verifies runtime state;
- container restart also verifies a new Docker start record;
- service start/stop verifies active state;
- service restart also requires a changed systemd `InvocationID`.

If dispatch began and the answer is lost, the mutation outcome is `write_unknown`. Reading
the desired value later cannot prove whether LocalPlane's write caused it. An unresolved
outcome ends in `recovery_required` and holds the object's write lock.

Recovery is append-only. A retry first observes and writes only if the required end state is
not already proved and fresh authority permits another attempt. A manual resolution releases
the hold without touching the host. Neither rewrites the original Change's history.

## Authentication boundary

Authentication is implemented and deliberately separated from operation safety:

- a one-shot local command creates one 256-bit master credential in a restrictive file;
- normal startup fails closed rather than generating or replacing that credential;
- CLI and automation use it as a Bearer credential verified with constant-time comparison;
- a browser presents it once to create a separate random, 12-hour, non-sliding in-memory session;
- only the SHA-256 session-token hash and absolute expiry remain server-side;
- the browser receives the raw session token only in an `HttpOnly`, `SameSite=Strict`, host-only cookie;
- cookie-authenticated POST, PUT, PATCH, and DELETE require an exact accepted `Origin`;
- bearer-authenticated requests are Origin-exempt, and an explicit invalid Bearer never falls back to a cookie;
- authentication is applied at the FastAPI router boundary, including documentation and OpenAPI.

There is one credential and no user, role, RBAC, named identity, password database, or persistent
session store. New confirmation records say `authenticated_request`; that states only that the
request crossed this boundary. Historical `unauthenticated_request` evidence is retained unchanged.
The Operator Console offers start/stop/restart Composer controls for loaded systemd services, using
the existing Run API. Docker, MTU and self-impact override controls remain absent. Closing the
Composer detaches its view; uncertain apply responses require inspection of the same Run and Change,
not automatic redispatch. Authentication cannot waive planning, preview binding,
confirmation, protection, provider authority, helper/systemd authorization, verification, recovery,
or write locks.

## Deployment constraints

The backend binds to loopback by default and mounts no console assets. Plain HTTP is accepted only
for a numeric loopback bind; `localhost` is not treated as bind proof. The supported non-loopback
shape is one backend process terminating HTTPS itself and serving a configured built console on
the same origin. It requires paired `LOCALPLANE_TLS_CERT_PATH` and `LOCALPLANE_TLS_KEY_PATH`, plus
`LOCALPLANE_CONSOLE_DIR` for a newly installed protected copy of the repository `web/dist` build.
Normal checkout build permissions are not automatically trusted: group/other-writable console
files, directories, or ancestors fail closed rather than widening permissions in place. The private key is an
unencrypted, service-owned regular PEM file at mode `0400` or `0600`; certificate/key paths reject
links and hard links and require trusted ancestors that are not group/other writable (apart from a
root-owned sticky directory). Certificate trust, hostname coverage, and expiry remain
operator/browser responsibilities. LocalPlane neither issues nor uploads either. The backend loads
the files into its in-process TLS context memory and does not copy their contents into LocalPlane
records or return them through the API or console.

This shape snapshots only a bounded index and flat fingerprinted build assets at startup; requests
do not read filesystem paths. Restart for an installed-build replacement or certificate/key
rotation. The index is `no-store`; assets use ETag revalidation. There is no reverse-proxy/header trust,
arbitrary file serving, or source-tree mount. API, documentation, and OpenAPI paths keep their
in-process authentication and normal error behavior; only real console deep links may use the
built index fallback. HTTPS and authentication establish neither management-path protection nor
permission to execute. The single-process shape remains pre-release: it is not public exposure, a
proxy deployment contract, or a production-readiness claim.
