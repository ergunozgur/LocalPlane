# Project status

[Back to the repository overview](../README.md)

LocalPlane is in early development and pre-release. This page separates what exists from
the foundations it provides, designs that are accepted but not built, longer-term direction,
and questions that remain unresolved.

## Status vocabulary

| Status | Meaning |
| --- | --- |
| **CURRENT** | Implemented in this tree and supported by current source or generated contract |
| **FOUNDATION** | Implemented structure intended to support more capability, without claiming that capability exists |
| **ACCEPTED-NOT-IMPLEMENTED** | A design decision exists, but the code and user-facing behavior do not |
| **FUTURE** | Direction only; not a release promise or current roadmap commitment |
| **UNRESOLVED** | Evidence, product scope, or deployment design is still open |

## Current product position

| Area | Status | Truth today |
| --- | --- | --- |
| Release maturity | **CURRENT** | Pre-release, local development software; APIs and packaging may change |
| Linux networking | **CURRENT** | Observation, provider evidence, management/intent, reconciliation, MTU plans and guarded execution |
| Docker | **CURRENT** | Container observation, bounded logs/current stats, and start/stop/restart through the Engine API |
| systemd | **CURRENT** | Bounded loaded-unit observation and eligible service start/stop/restart through D-Bus |
| Runs and Changes | **CURRENT** | Immutable previews, confirmation, typed apply, fresh verification, and durable recovery holds |
| Operator Console | **CURRENT** | Observation, cross-domain search, evidence-backed Relationships tabs, and a limited authenticated systemd service Composer |
| OpenAPI integration | **CURRENT** | The committed snapshot and generated TypeScript types reflect this integrated backend tree |
| Relationship model | **CURRENT** | Object-scoped Relationships tabs on network interfaces and Docker containers show only identifier-backed Docker network, container, master-interface and guarded management-path joins; unread streams and unresolved targets stay explicit |
| Host explorer breadth | **FOUNDATION** | Object workspaces and provider seams exist; files, storage, packages, users, processes, and general logs are not implemented |
| Authentication and browser sessions | **CURRENT** | One local master Bearer credential, derived 12-hour non-sliding in-memory browser sessions, logout, and router-level enforcement |
| Cookie Origin/CSRF boundary | **CURRENT** | Cookie-authenticated unsafe requests require exact accepted Origin; Bearer requests are exempt |
| Frontend write workflow | **CURRENT** | Loaded systemd services: plan start/stop/restart, inspect immutable preview, confirm, apply, inspect outcome and cancel before write; other operation controls remain absent |
| Production console delivery and TLS | **UNRESOLVED** | No accepted asset-serving topology; non-loopback TLS is not configured |
| Docker management-path protection | **UNRESOLVED** | Container lifecycle cannot prove whether a particular container carries the operator route |
| Database request concurrency | **CURRENT** | A process-local reentrant lock serializes runtime use of the shared SQLite connection, holding transactions through commit or rollback and reads through row retrieval; forced-contention request regressions cover overlapping refreshes and reads during rollback. Multi-process deployment is not validated |
| Broader infrastructure control plane | **FUTURE** | More systems, networks, workloads, applications, and providers are direction, not current capability |
| Unified metrics, logs, and historical replay | **FUTURE** | Bounded container samples and operational records exist; general series, log aggregation, and replay do not |
| Fleet and automation | **FUTURE** | Not implemented and not a near-term commitment stated by this document |

## Object and operational-record search

The authenticated shell includes a read-only palette for objects and operational records. On
opening, it reads existing typed lists for network interfaces, loaded systemd units, Docker
containers, and recent Runs, Changes and Findings, including resolved Findings. Results open
the existing detail views using their stable object or record IDs. Search does not create
Applications, plan operations or execute them. Service actions enter Composer from the service
Lifecycle tab; typed action search remains deferred.

Record search covers the latest 50 Runs by creation time, 100 Changes by creation time and
100 Findings by episode opening time, not exhaustive history. It matches only the returned
records by record or target identity/name, operation or Finding description, and recorded
state/outcome. At most 40 matches are displayed; refining the query searches the loaded lists,
not older history. Loading and failed sources remain explicit, and query misses do not prove
that no older record exists. Reopening reads the lists again.

## What the screenshot means

The V4.2 image is an approved product and interface direction built from illustrative data.
It is not a current-product screenshot, a release checklist, or a promise that every pictured
surface will ship. Source and generated contract decide what is **CURRENT**.

## Current limitations that affect safe use

- Authentication uses one local credential; there are no users, roles, RBAC, or named identity.
- Browser sessions are process-local, expire absolutely after 12 hours, and disappear on restart.
- Plain-HTTP browser use remains loopback-only; TLS and production remote exposure are not implemented.
- Rootful Docker socket access is effectively root-equivalent.
- Console writes are limited to systemd service Composer; production serving topology remains unresolved.
- Container lifecycle lacks authoritative operator-route protection.
- systemd lifecycle fails closed when containment, effect, authorization, or verification
  evidence cannot be established.
- Shared-connection request concurrency is covered for the supported single-process shape;
  this does not establish production readiness or validate multi-process deployment.

## Direction without commitments

LocalPlane is intended to make heterogeneous infrastructure operable through a coherent
model while retaining provider-specific truth. Likely areas of expansion include deeper
Linux exploration, relationships, storage, packages, processes, logs, metrics, applications,
additional runtimes, fleet operation, and automation. Each area requires its own provider,
authority, safety, retention, and recovery decisions before it can move to **CURRENT**.

No ordering or delivery date is implied here.
