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
| Operator Console | **CURRENT** | Substantial read-only interface, including cross-domain search over observed interfaces, systemd units and Docker containers; no record-writing or host-writing controls |
| OpenAPI integration | **CURRENT** | The committed snapshot and generated TypeScript types reflect this integrated backend tree |
| Relationship model | **FOUNDATION** | Evidence-backed network, Docker, systemd, containment, and operational relationships; coverage follows implemented providers |
| Host explorer breadth | **FOUNDATION** | Object workspaces and provider seams exist; files, storage, packages, users, processes, and general logs are not implemented |
| Authentication and browser sessions | **CURRENT** | One local master Bearer credential, derived 12-hour non-sliding in-memory browser sessions, logout, and router-level enforcement |
| Cookie Origin/CSRF boundary | **CURRENT** | Cookie-authenticated unsafe requests require exact accepted Origin; Bearer requests are exempt |
| Frontend write workflow | **ACCEPTED-NOT-IMPLEMENTED** | Authentication exists, but no current console control calls record-writing or host-writing routes |
| Production console delivery and TLS | **UNRESOLVED** | No accepted asset-serving topology; non-loopback TLS is not configured |
| Docker management-path protection | **UNRESOLVED** | Container lifecycle cannot prove whether a particular container carries the operator route |
| Database request concurrency | **UNRESOLVED** | The shared SQLite connection needs a bounded concurrency proof; production readiness is not established |
| Broader infrastructure control plane | **FUTURE** | More systems, networks, workloads, applications, and providers are direction, not current capability |
| Unified metrics, logs, and historical replay | **FUTURE** | Bounded container samples and operational records exist; general series, log aggregation, and replay do not |
| Fleet and automation | **FUTURE** | Not implemented and not a near-term commitment stated by this document |

## Object search

The authenticated shell includes a read-only Search objects palette. It reads the existing typed
list endpoints for network interfaces, loaded systemd units and Docker containers only when
opened, filters the returned observations by name or stable object ID, and links to the existing
object detail routes. It does not create Applications or issue operations. The typed Run/composer
workflow remains deferred and is not represented by this search control.

## What the screenshot means

The V4.2 image is an approved product and interface direction built from illustrative data.
It is not a current-product screenshot, a release checklist, or a promise that every pictured
surface will ship. Source and generated contract decide what is **CURRENT**.

## Current limitations that affect safe use

- Authentication uses one local credential; there are no users, roles, RBAC, or named identity.
- Browser sessions are process-local, expire absolutely after 12 hours, and disappear on restart.
- Plain-HTTP browser use remains loopback-only; TLS and production remote exposure are not implemented.
- Rootful Docker socket access is effectively root-equivalent.
- The Operator Console is read-only and has no accepted production serving topology.
- Container lifecycle lacks authoritative operator-route protection.
- systemd lifecycle fails closed when containment, effect, authorization, or verification
  evidence cannot be established.
- The current database concurrency boundary is not production-proven.

## Direction without commitments

LocalPlane is intended to make heterogeneous infrastructure operable through a coherent
model while retaining provider-specific truth. Likely areas of expansion include deeper
Linux exploration, relationships, storage, packages, processes, logs, metrics, applications,
additional runtimes, fleet operation, and automation. Each area requires its own provider,
authority, safety, retention, and recovery decisions before it can move to **CURRENT**.

No ordering or delivery date is implied here.
