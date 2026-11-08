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
| Docker | **CURRENT** | Explicit read-only container-inventory observation with retained-record/latest-membership context, bounded logs/current stats, Docker-reported networking and mount facts, and start/stop/restart through the Engine API |
| systemd | **CURRENT** | Bounded loaded-unit observation, readable service execution/restart context, explicit consent-gated bounded current-boot service journal reads, and eligible service start/stop/restart through D-Bus; incomplete potential-effect evidence blocks rather than clearing protection |
| Storage pressure | **CURRENT** | Transient mounted filesystem byte/inode availability, exact mount evidence and immediate kernel block backing; ext4/xfs only in the agent mount namespace |
| Runs and Changes | **CURRENT** | Immutable previews, confirmation, typed apply, fresh verification, and durable recovery holds |
| Operator Console | **CURRENT** | Scoped attention including unresolved holds, explicit inventory observation with stored-reread recovery, membership-qualified summaries/relationships, cross-domain search, and a limited authenticated systemd service Composer |
| OpenAPI integration | **CURRENT** | The committed snapshot and generated TypeScript types reflect this integrated backend tree |
| Relationship model | **CURRENT** | Object-scoped Relationships tabs on network interfaces and Docker containers show only identifier-backed Docker network, container, master-interface and guarded management-path joins; unread streams and unresolved targets stay explicit |
| Host explorer breadth | **FOUNDATION** | Object workspaces and provider seams exist; files, broader storage management/topology, packages, users, processes, and general logs are not implemented |
| Authentication and browser sessions | **CURRENT** | One local master Bearer credential, derived 12-hour non-sliding in-memory browser sessions, logout, and router-level enforcement |
| Cookie Origin/CSRF boundary | **CURRENT** | Cookie-authenticated unsafe requests require exact accepted Origin; Bearer requests are exempt |
| Frontend write workflow | **CURRENT** | Loaded systemd services: plan start/stop/restart, inspect immutable preview, confirm, apply, inspect outcome and cancel before write; other operation controls remain absent |
| Direct HTTPS console delivery | **CURRENT** | Optional single-process backend HTTPS with one configured built same-origin console snapshot; default remains numeric-loopback HTTP with no console mount |
| Docker management-path protection | **UNRESOLVED** | Container lifecycle cannot prove whether a particular container carries the operator route |
| Database request concurrency | **CURRENT** | A process-local reentrant lock serializes runtime use of the shared SQLite connection, holding transactions through commit or rollback and reads through row retrieval; forced-contention request regressions cover overlapping refreshes and reads during rollback. Multi-process deployment is not validated |
| Broader infrastructure control plane | **FUTURE** | More systems, networks, workloads, applications, and providers are direction, not current capability |
| Unified metrics, logs, and historical replay | **FUTURE** | Bounded container samples and operational records exist; general series, log aggregation, and replay do not |
| Fleet and automation | **FUTURE** | Not implemented and not a near-term commitment stated by this document |

### Service incident evidence

From an individual service workspace, the Journal tab performs no request until the operator
acknowledges the sensitive-message warning and explicitly reads. Results are ephemeral, limited
to the current boot and recent service messages, and are cleared when the workspace/tab closes
or identity changes. Access follows the agent's effective OS permissions and the fixed set of
system journal files the agent can enumerate/read; installed native libsystemd support is an
optional access mechanism, not a privilege increase. Coverage is reported as complete only for
that bounded query/file enumeration, partial when some files are inaccessible, or unknown when
the provider cannot establish coverage. Completeness does not mean all history or a frozen view.
System-manager service reads do not aggregate user-manager journals. Exact systemd unit matching
can omit manager messages; journal text may contain secrets and is not reliably redacted, and
matching messages do not establish cause.
Metadata-only records are retained: an absent MESSAGE field is distinct from present but empty
message text and neither is treated as an empty journal result.
Journal access does not determine whether Start or Restart is eligible: those actions remain in
Lifecycle and use the existing immutable preview, confirmation, durable Run and verification path.

## Inventory truth and refresh

Overview distinguishes unresolved recovery holds, open Findings, and reported failed-service
evidence. An incomplete or stale assessment is not an all-clear, and zero Findings is not
proof that nothing needs attention. Holds link to their existing Change records; this workflow
does not release or retry them.

Network, System and Docker explicitly collect provider evidence, show the provider result,
then reread stored snapshots. Stored rereads can be retried independently after collection;
failed collection leaves prior facts with their original observation age. Counts identify
latest recorded membership versus retained or unknown membership, not live presence. Fresh
host identity or an answering agent does not make the inventories current. Retained objects
and historical relationships remain available for investigation.

The global record reread does not collect inventories, resample filesystems/container usage,
reprobe the agent, or read log/journal content. Those controls are separate and explicit.
Address lifetimes preserve unknown, finite (including zero), and explicitly infinite values;
ambiguous historical nulls remain unknown.

## Mounted filesystem pressure

Storage answers which listed mount has little or no usable byte or inode capacity, with
independent available percentages, sorting/filtering, exact counters, reserved-space accounting
and immediate backing-device evidence. It samples only on explicit request through the
authenticated read-only `POST /api/v1/storage/filesystems/observe`; cookie Origin checks still
apply. The agent method is `storage.observe_filesystems`. Neither accepts caller paths.

Only ext4/xfs with supported ancestors are sampled. Other filesystems remain visible with
unknown counters. Btrfs may span devices; its topology is not inferred. The namespace is the
agent's, not necessarily the host's. Observation IDs scope mount IDs and selection resets on a
new response; paths and reusable kernel IDs are not durable storage objects. There is no
database retention, host total across bind mounts, directory scan, cleanup, SMART, backup,
health, growth or root-cause claim. Device names come only from a kernel major:minor sysfs join;
device-mapper/RAID lower topology is not enumerated and missing backing remains unknown.

Collection reads at most 1 MiB per mount table, displays at most 128 rows, limits framed output
to 512 KiB, allows 500 ms per sampled mount and a 3-second total parent collection wait. Paths
have at most 64 components and 4096 characters. A disposable child pins components with O_PATH,
rejects symlinks/unsupported ancestors, verifies fdinfo mount ID and device against the exact
current mount table before/after statistics, and revalidates namespace/table evidence. This
detects changes; it is not a namespace freeze or proof of zero possible path-resolution race.
Timeout returns unavailable, not partial guessed counters. One process-wide slot remains held
until actual child exit/reap; SIGKILL does not guarantee cancellation of blocked kernel I/O.
Concurrent requests and retries while held return busy without replacement work.

Capability discovery reports the mechanism as degraded with filesystem access unverified;
startup does not probe mounted filesystems. Actual per-request coverage, denial, unsupported
ancestors, malformed/truncated evidence and refresh failure remain explicit. Decimal strings
preserve exact kernel counters across the API; zero/sentinel/inconsistent inode accounting is
unknown, not zero available inodes.

Synthetic validation uses `tests/storage_fixture.py` and an injected storage provider. Its
`FixtureStorageService` can also serve a disposable agent socket for rendered development
checks, with backend startup observation disabled and normal authentication retained. It
performs no startup host probes and refuses other methods. There is no production fixture
switch or authentication bypass.

## Recovery console and durable continuity

Systemd service Changes expose object-scoped recovery beside the original immutable outcome.
Open the exact Change from its Run or recovery alert. **Refresh recorded status** only reads
the existing Change, original Run and authoritative unresolved-hold count. **Assess current
service state** is a separate explicit non-mutating assessment, even when a retry grant is
outstanding. An inconclusive assessment keeps the hold; running alone cannot prove a restart.

When the backend permits another write, review the original service/action and published
recovery confirmation policy. Acknowledge it and type the exact service name when required,
then grant one recovery retry. **Retry original action once** is a separate deliberate action;
navigation, refresh and assessment never grant or spend authority. Every recovery action
cross-checks the reviewed revision; retry also names the exact single-use grant. Backend
executor, job, protection, freshness and authorization refusals remain authoritative.

Manual release has a separate typed-name acknowledgment and optional recorded note. It does
not mutate the host, verify or repair the service, or upgrade the original uncertain outcome.
Chronological attempts retain pre-write evidence, dispatch, host effect, post-write verification,
grant references, refusals and release statements. Current lock state is separate from the
historical recovery-required result, and host hold counts are reread rather than decremented.

Closing a view detaches from the same durable Run; it does not cancel it. The console retains
write-attempt intent in session storage before dispatch and refuses dispatch if retention is
unavailable. Lost apply, confirmation or recovery responses are reconciled through reads,
never automatic write replay. A missing grant in one read is not proof a confirmation was
never delivered. Intent markers survive reload in the same tab, not cleared browser storage
or independent browser sessions; backend single-use/revision guards remain necessary.
A structured, recognized pre-handler authentication refusal is different from lost delivery:
after sign-in and a fresh successful read of the same record, only that definitively refused
intent can be released for another deliberate click. Reattachment never resends the request.
Transport failures, malformed or unrecognized authentication responses, and unrelated historical
or legacy apply markers remain blocking.
The same fresh-read rule applies to the recovery backend's exact pre-dispatch revision/grant
mismatch refusals. Generic conflicts or server errors do not establish safe non-delivery.
If completed retry history positively records consumption of the prior grant together with
its mutation dispatch marker, a held operation can receive a new deliberate single-use grant.
The operator must acknowledge and type again under the current policy; this does not retry
automatically. An assessment or revision change alone never renews a lost confirmation.
Visible attached views watch active or ambiguous records with non-overlapping GETs, bounded
to 20 reads at three-second intervals. Watching stops on detach, hidden views, authentication
failure, settled state or budget exhaustion; manual refresh remains available. Sign-in returns
to the same URL and rereads its records without reissuing writes. Missing recovery metadata
or unreadable current status cannot enable recovery controls.

Lifecycle completion receipts are bounded to 4096 entries and held in process memory.
Missing, old-agent or post-restart receipts fail closed and can prevent automatic proof or manual release.
A completion receipt proves executor completion, not service verification.
At capacity, new lifecycle dispatches are refused without evicting existing receipts. An exact
correlated refusal records `not_written`; a lost or unreadable refusal remains uncertain and
can keep a hold unresolved because no receipt was registered for that refused attempt.
LocalPlane does not automatically restart the agent or renew recovery authority.

## Object and operational-record search

The authenticated shell includes a read-only palette for objects and operational records. On
opening, it reads existing typed lists for network interfaces, loaded systemd units, Docker
containers, and recent Runs, Changes and Findings, including resolved Findings. Results open
the existing detail views using their stable object or record IDs. Search does not create
Applications, plan operations or execute them. Type `>` for loaded-service actions, limited to
`start`, `stop` and `restart`; follow the action with a service name or stable ID to narrow targets.
Selection opens Lifecycle with that exact action and target staged, not a new Run. Detail is
re-read and invalid, inapplicable, stale or unknown target evidence cannot enable the staged
action. Only an explicit **Plan** creates a Run, whose immutable preview and confirmation are
handled by the existing Composer. Navigation, Back and reload do not plan or execute anything.
Loaded-service discovery is not proof of protection clearance or execution eligibility.

Record search covers the latest 50 Runs by creation time, 100 Changes by creation time and
100 Findings by episode opening time, not exhaustive history. It matches only the returned
records by record or target identity/name, operation or Finding description, and recorded
state/outcome. At most 40 matches are displayed; refining the query searches the loaded lists,
not older history. Loading and failed sources remain explicit, and query misses do not prove
that no older record exists. Reopening reads the lists again.

## Object-scoped operational records

Network interfaces, systemd units and containers share a Machine record view. It reads up to
12 Runs and 12 Changes by creation time and 12 Findings by episode opening time for the object's
stable ID, including resolved Findings. The combined view shows at most 12 entries; kind
filters expose each source's loaded records. These are bounded reads, not exhaustive history.
Loading, refresh and failed streams remain explicit; failed streams can be retried independently.

Each record links to its existing detail view. Runs remain visible even when they produced a
Change, including when the Changes stream is unavailable. Runs and Changes can describe the
same attempt, not two separate mutations. Recorded run state and host effect remain distinct;
a resolved Finding does not by itself prove repair. This view observes records and does not
plan, confirm, apply or cancel an operation.

## What the screenshot means

The V4.2 image is an approved product and interface direction built from illustrative data.
It is not a current-product screenshot, a release checklist, or a promise that every pictured
surface will ship. Source and generated contract decide what is **CURRENT**.

## Current limitations that affect safe use

- Authentication uses one local credential; there are no users, roles, RBAC, or named identity.
- Browser sessions are process-local, expire absolutely after 12 hours, and disappear on restart.
- Plain-HTTP browser use requires a numeric loopback bind. Optional direct HTTPS console startup validates operator-supplied certificate/key files and snapshots a newly installed protected built-console copy; group/other-writable build paths or ancestors fail closed. Restart replaces either. It is not proxy support, public exposure, or production readiness.
- Rootful Docker socket access is effectively root-equivalent.
- Console host writes are limited to systemd service Composer and separately authorized exact service recovery retries. The narrow pre-release direct HTTPS serving shape is accepted; broader production packaging and exposure remain unresolved.
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
