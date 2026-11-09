# Offline maintenance, updates and bounded recovery

[Installation](installation.md)

The CLI never stops/starts services, installs units, runs migrations, initializes
authentication or performs host-action recovery. Administrator commands and
authenticated runtime verification are deliberate separate steps. Use the
installation's explicit CLI roots and numeric service UID/GID throughout.

## Read-only inspection

`localplane-maintenance check`, `status` and `plan <action>` do not open the
database, read credentials, initialize auth, create locks, change modes or contact
services. They are offline configuration/release evidence, not running identity
or provider health. `--dry-run <action> ...` returns a plan without mutation.
If interrupted restore temporarily leaves configuration absent, check/status still
returns the external maintenance window and pending/refusal evidence. Configuration
and installed identity are explicitly unavailable/unknown; no database or credential
is opened to manufacture an identity or clear the pending guard.

## Update sequence

1. Review the target bundle, exact immutable paths, platform/dependency matrix,
   current authenticated diagnostics and explicit administrator command packet.
   Preserve existing Runs, Changes, outcomes, holds, confirmations and history.
2. Run `localplane-maintenance open`; retain the returned random window ID.
   The shared controller drains writers and establishes the durable external
   fence. It does not expire, disappear on restart, disarm guards or release holds.
3. Separately stop backend and agent with the reviewed administrator commands.
   Do not equate a service-stop request or lost IPC response with executor
   quiescence. An offline lease must prove both runtime owners are absent and
   the controller must refuse unresolved executor/guard uncertainty.
4. Run `localplane-maintenance backup --window <window-id>`. Retain its returned
   snapshot ID. The CLI takes an offline exclusive lease, uses SQLite's backup
   API, seals a standalone database, checks integrity/foreign keys/schema, and
   copies matching configuration, TLS and master credential. It never backs up
   or restores the witness. Backup credentials remain in protected local storage.
   CLI snapshots use `format_version=2`, including explicit database and credential
   ownership metadata. Older format-1 snapshots are refused explicitly; do not edit
   their version field to force restore. This does not change `release.json`, the
   external window witness or the interrupted-restore marker formats.
5. Run `localplane-maintenance update /absolute/bundle --window <window-id>
   --snapshot <snapshot-id>` explicitly as administrator. It verifies and stages
   immutable code, requires a verified same-window snapshot, checks CURRENT schema
   compatibility, retains pre-activation configuration and atomically selects
   the exact release/console paths in the non-secret environment. The fence stays
   active. No `current` symlink is created.
   This changes environment selection only: each process still imports the code
   and manifest fixed by its unit's immutable wrapper. Change BOTH exact units and
   the matching environment while offline; never accept mixed versions merely
   because the environment names the intended release.
6. Separately install the exact target release's reviewed unit templates, reload
   systemd and start agent then backend. During a known active window, startup may
   intentionally apply forward migrations, but skips interrupted-record settlement
   and initial observation. Unknown/missing witness state refuses startup before
   opening the database. Never invoke startup merely to inspect a store.
7. Reauthenticate: the same master credential survives, but in-memory browser
   sessions do not survive backend restart. Compare authenticated backend/agent/
   console identities, applied schema checksums, TLS/assets and preserved records.
   Reaching a process or seeing cached capabilities is not successful verification.
8. Only after those checks run `localplane-maintenance accept --window <window-id>
   --verified`. This explicit operator acknowledgement actually closes that exact
   external window through the shared controller. The closed witness is retained,
   not deleted. Normal admission can resume; old snapshots are now ineligible.

The generic plan text is not an executable administrator packet. Before actual
installation changes, enumerate the environment, identity, exact account/path/
unit/file commands, collision checks, expected effects and recovery disposition.
Never auto-run a plan, assume privilege from CLI availability, or automatically
retry an ambiguous action. Preserve failures and investigate them while fenced.

## Compatible code-only revert

With both services stopped under the same active window, run:

```text
localplane-maintenance revert <installed-build-id> --window <window-id> --snapshot <snapshot-id>
```

The target must already exist as verified root-owned immutable code. Compatibility
is checked against the **CURRENT** store, including migration checksums and ordered
schema prefix, before changing the environment. An older release that lacks a
currently applied migration is refused. There is no reverse SQL migration or
rewriting of migration files. Reinstall its exact unit templates separately,
restart, verify and explicitly accept. This changes code selection, not Linux,
Docker or systemd outcomes.

## Failed-update restore, before resumption only

Stop both services separately, then run:

```text
localplane-maintenance restore --window <window-id> --snapshot <snapshot-id>
```

The snapshot's random window ID and generation must match the **same still-active**
external window. Verification checks the full saved file set, original release,
snapshot schema and saved configuration before active-state mutation. Compatibility
uses the snapshot schema, not the failed update's potentially newer CURRENT schema.

The shared controller first writes a persistent pending-restore marker outside the
database, under offline exclusion. It binds the exact snapshot name, window and
generation. While pending or corrupt/unknown, startup refuses **before opening the
store** and accept, backup, update and revert are refused. An active window alone
is not sufficient protection during a multi-file restore. Without this controller
contract the CLI refuses restore; it never falls back to an ordinary runtime lease.

Before active changes, the CLI retains all current raw database/WAL/shm evidence,
configuration and credential in a private stable transaction directory. Raw
displaced evidence is not a restorable backup. A corrupt failed store is retained
without opening it. A small durable journal binds those originals to the exact
snapshot and records progress; originals are never overwritten on manual resume.

The complete configuration is copied, ownership-checked and fsynced to a sibling
of its destination on the configuration filesystem **before database replacement**.
The old configuration is displaced to another sibling, never renamed across to
the data filesystem. A configuration mount point or other cross-device transition
is refused before active changes. Database and credential stages likewise reside
beside their destinations. Only after final byte/schema/config/ownership validation
may the controller mark the restore complete; the normal window remains active.

If interrupted, leave both services stopped. Explicitly rerun the **same** restore
command with the same snapshot/window: matching pending transactions reuse stable
retention/journal metadata, including when current database/configuration paths
are absent after displacement. A mismatched or corrupt transaction, changed
original evidence or incomplete retention/stage safely refuses without overwriting
originals. No automatic retry, guard clearing, general abort or force command is
provided. The CLI does not restore the window witness or lock files.

Install the saved release's exact unit templates separately, start agent/backend,
verify the same authenticated/runtime/schema/preservation checks and explicitly
accept. Do not delete backups/displaced evidence without a separate disposition.

Restore is refused after acceptance, after normal operation resumes, and after a
closed/reopened window. General point-in-time restore needs a separate authority-
reconciliation design: it could lose later records or revive consumed approvals.
No restore replays actions, retries a Change, releases a hold, reissues a confirmation
or undoes host-side changes. A durable window is not universal rollback.

## Indefinite executor uncertainty

A lost executor response may leave an uncertainty marker indefinitely. Process
death, a stopped service, a terminal database record or elapsed time does **not**
prove that an external action finished. This procedure offers no generic marker
resolution, expiry, force or abort. Preserve the evidence and leave authority
fenced; matching verified restore/accept is available only when the shared guard
can prove its own prerequisites. If it refuses because execution remains unknown,
stop and investigate rather than deleting the marker or manufacturing completion.

The release-wide file inventory verifies dependency bytes. A dependency's separate
hash field is build evidence, not a signature or independently revalidated package
provenance statement; do not infer stronger supply-chain guarantees from it.

## Validation limits

Filesystem/SQLite fixture tests are not service/executor exclusion proof. Release
acceptance also requires the coordinated controller/backend, real installed update/
failure/revert/restore runs, restart fence durability, direct-HTTPS authentication,
session invalidation and rendered desktop/mobile diagnostics checks. Do not treat
source presence, a plan, or a skipped integration test as a successful installation.
