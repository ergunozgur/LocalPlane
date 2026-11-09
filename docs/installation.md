# Versioned installation

[Overview](../README.md) · [Maintenance and recovery](maintenance-and-update.md)

This is the pre-release installation contract, not a production-readiness claim.
Installation requires a verified, platform-matching bundle containing `release.json`,
`src/localplane`, unpacked `vendor/` dependencies, compiled `console/`, `bin/`
entry points and exactly two service templates. A repository checkout or a directory
of wheel archives is not that bundle. Bundle construction, installed runtime tests
and the supported platform matrix must be verified before distributing a release.
The offline CLI does not establish that those checks have occurred.

## Prepare as an ordinary user

Use the public bundle's tested Python version, architecture and pinned dependency
payload. Its compiled console needs neither Node nor a Vite process at runtime.
The CLI compares the manifest's OS value with `ID:VERSION_ID` from `os-release`
(for example `ubuntu:24.04`), and requires the exact architecture and Python version.
Do not put wheel archives on `PYTHONPATH`, acquire dependencies during installation,
use editable packages from a developer checkout, or invoke build isolation as part
of the installation. If building from source, dependency preparation and frontend
compilation are explicit ordinary-user build steps, separate from system installation;
the resulting bundle must include its complete dependency contents and hashes.

Keep the build manifest: `source_commit` is the base commit, not an exact modified
candidate. A modified candidate has `source_dirty=true`, `source_tree=null`, and
content digests identifying its actual source and assets. Do not commit merely to
make a build appear clean.

Review the complete bundle and compare its build ID with the trusted delivery
receipt. Hash verification detects changed bytes; it is not a publisher signature
or a substitute for obtaining the bundle from a trusted source.

## Administrator preparation

Review concrete commands, identities, collisions and recovery before executing any
system change. Nothing in the maintenance CLI invokes elevation, `systemctl`, a
package manager, helper, Docker API or arbitrary shell command.

The normal layout is:

| Path | Owner / purpose |
| --- | --- |
| `/opt/localplane/releases/<build-id>` | root-owned immutable code, dependencies, console and wrappers |
| `/etc/localplane` | root-owned non-secret environment and protected TLS material |
| `/var/lib/localplane` | dedicated service identity; database and master credential |
| `/var/lib/localplane/maintenance` | shared external fence, never restored from backup |
| `/var/lib/localplane/backups`, `displaced` | protected local recovery state, mode 0700 |
| `/run/localplane` | private runtime socket directory, prepared by the reviewed units |

Use a dedicated non-login account and group, with no added Docker, sudo, helper or
PolicyKit authority. Both ordinary services may use that identity. Root-owned code
and configuration must not be writable by the service identity. Public console
files are readable, normally 0644; directories and wrappers normally 0755.
All installed wrappers must disable Python bytecode writes (for example with
`PYTHONDONTWRITEBYTECODE=1`) before importing application/vendor code. Runtime
caches must never add unmanifested files to the immutable release.
The administrator's restrictive umask must not make new public code ancestors
unreadable: staging sets the requested 0755 mode on newly created code directories
only. Existing directories are never chmodded or silently repaired; an existing
incorrect parent requires its own explicitly reviewed administrator repair.

Prepare the data root as service-owned 0700. Prepare the shared maintenance root
with the service group and setgid, normally 2770, initially **empty**. Before
creating application configuration, store, credential, backups or runtime state,
run `localplane-maintenance initialize` as the dedicated service identity with
the explicit CLI roots/UID/GID and a reviewed restrictive umask (0007). It creates
the genesis closed witness through the shared controller. It refuses preexisting
application state or even a pre-created lock file; it is not a repair/reset command.
A missing witness after initialization is unknown and fails closed.

Only after initialization may stable `control.lock`, `runtime.lock` and `cli.lock`
be present. The controller/CLI creates required locks without replacing existing
inodes; shared locks need service-group read/write access (normally 0660). Review
their actual modes and ownership before starting services or invoking the
administrator's CLI. Never unlink/recreate locks or reinitialize a damaged root
to resolve a refusal. No unrelated identities may belong to the service group.

Set `LOCALPLANE_ROOT`, `LOCALPLANE_ETC`, `LOCALPLANE_VAR`, `LOCALPLANE_RUN`,
`LOCALPLANE_MAINTENANCE_DIR`, `LOCALPLANE_SERVICE_UID` and `LOCALPLANE_SERVICE_GID`
explicitly for CLI invocations. The last two are the numeric service identity,
not an authority grant. The maintenance root must be exactly `VAR/maintenance`.
All four installation roots must be absolute, disjoint and free of symlinks.
Use a 64-hex release directory slug, without `sha256:`; the manifest build ID
retains its `sha256:` prefix. A colon in a release directory would split Python's
module search path. Offline CLI verification uses inspection identity, never
running component identity.

From the verified bundle's maintenance wrapper, explicitly invoke
`localplane-maintenance install /absolute/path/to/bundle` as the administrator.
It stages and verifies root-owned immutable code. It does **not** create an account,
initialize a credential, overwrite existing configuration, install units or start
services. Repeating the same install verifies existing code without replacing state;
a different configured release is refused as installation drift (use update).

## Non-secret environment

Create `/etc/localplane/environment` explicitly, root-owned and not group-writable.
Use unique literal `LOCALPLANE_NAME=value` assignments; no substitutions, exports,
continuations or embedded credentials. Quote single values when necessary. Include:

```text
LOCALPLANE_DB_PATH=/var/lib/localplane/localplane.db
LOCALPLANE_AUTH_SECRET_PATH=/var/lib/localplane/localplane-master.secret
LOCALPLANE_MAINTENANCE_DIR=/var/lib/localplane/maintenance
LOCALPLANE_AGENT_SOCKET=/run/localplane/agent.sock
LOCALPLANE_RELEASE_MANIFEST=/opt/localplane/releases/<build-id>/release.json
LOCALPLANE_CONSOLE_DIR=/opt/localplane/releases/<build-id>/console
LOCALPLANE_TLS_CERT_PATH=/etc/localplane/tls.crt
LOCALPLANE_TLS_KEY_PATH=/etc/localplane/tls.key
LOCALPLANE_HOST=<reviewed-listen-address>
LOCALPLANE_PORT=<reviewed-unused-port>
```

Replace every placeholder with the reviewed exact value. Never use a `current`
symlink in release, console or certificate input paths. Supply a trusted certificate
and matching private key; issuance, DNS and external trust provisioning are separate.
The key remains service-owned 0400/0600. Store all TLS dependencies beneath the
configuration root so the recovery snapshot contains the matching set. No reverse
proxy terminates the operator connection; non-loopback access requires backend TLS.

Initialize the credential **once**, invoking the installed release's `localplane-auth
init` wrapper as the service identity with the exact configured secret path. Store
its one-time output privately; never include it in command packets, logs, captures
or source control. Existing credentials are never regenerated by install/update.

## Install the two units and verify

Inspect the release's two ordinary service templates. Each `ExecStart` must name
that exact immutable release wrapper, load the intended EnvironmentFile, use the
dedicated service identity and share the same maintenance/socket roots. Review the
unit's writable paths and runtime directory lifetime. No extra maintenance daemon,
socket activation, privileged helper or provider access grants are implied.

The administrator explicitly installs the reviewed agent and backend units under
`/etc/systemd/system`, reloads systemd, then starts agent followed by backend.
These are separate administrator commands, not CLI side effects. A different
installation needs distinct account/unit/path/socket names and an unused port;
never reuse another installation's state or authentication material.

Sign in over direct same-origin HTTPS with the private master credential. Visit a
deep link and Settings → Installation. Compare the backend's own build, the live
agent's own build, loaded console digest and applied schema with `release.json`.
Unknown, recorded or unreachable evidence is not a successful runtime comparison.
Exercise protected API access and review actual provider refusals without granting
new permissions. Offline `check`/`status` never proves services or providers healthy.

Preserve the exact bundle, source/dependency/platform receipt and installation
verification results. An isolated installation on an existing OS is not a clean-OS
installation claim, nor evidence for other architectures or Python versions.

## Minimal administrator example: new installation only

The following are **explicit privileged examples**, not commands executed by the
CLI. Review the complete packet, installed unit templates and collision checks
first. Substitute a verified public bundle path and existing, operator-provisioned
TLS files. Do not run this new-account bootstrap over an existing installation.
Keep credential output off logs, terminal recordings and command receipts.

`BUNDLE` below means a reviewed **root-owned trusted staging directory**, with
root-owned contents and no symlinks or group/world-writable ancestors, for example
`/opt/localplane-staging/<verified-build-slug>`. Placing and verifying that payload
is a separate explicit administrator preparation step. Do not execute an ordinary
user-writable build tree as administrator. A bundle under an ordinary user's HOME
is not necessarily trusted or traversable under the new service identity: the CLI
can refuse it with `unsafe_owner` or `unsafe_path`. Select the approved trusted
staging location; never loosen a user's parent-directory permissions, bypass the
checks or treat a source build directory as an installed release.

In an administrator shell, first check the proposed account and roots are absent:

```sh
set -eu
umask 0007
BUNDLE=/opt/localplane-staging/verified-build-slug
test -d "$BUNDLE"
if getent passwd localplane >/dev/null || getent group localplane >/dev/null; then
  echo 'Existing identity: stop and review the existing installation' >&2
  exit 1
fi
for path in /opt/localplane /etc/localplane /var/lib/localplane /run/localplane; do
  test ! -e "$path" && test ! -L "$path"
done
test ! -e /etc/systemd/system/localplane-agent.service
test ! -e /etc/systemd/system/localplane-backend.service
BUILD_ID=$(python3 -c 'import json,re,sys; b=json.load(open(sys.argv[1]))["build_id"]; assert re.fullmatch("sha256:[0-9a-f]{64}",b); print(b)' "$BUNDLE/release.json")
RELEASE=/opt/localplane/releases/${BUILD_ID#sha256:}
groupadd --system localplane
useradd --system --gid localplane --home-dir /var/lib/localplane \
  --no-create-home --shell /usr/sbin/nologin localplane
install -d -o root -g root -m 0755 /opt/localplane
install -d -o root -g localplane -m 0750 /etc/localplane
install -d -o localplane -g localplane -m 0700 /var/lib/localplane
install -d -o localplane -g localplane -m 2770 /var/lib/localplane/maintenance
export LOCALPLANE_ROOT=/opt/localplane LOCALPLANE_ETC=/etc/localplane
export LOCALPLANE_VAR=/var/lib/localplane LOCALPLANE_RUN=/run/localplane
export LOCALPLANE_MAINTENANCE_DIR=/var/lib/localplane/maintenance
export LOCALPLANE_SERVICE_UID=$(id -u localplane)
export LOCALPLANE_SERVICE_GID=$(id -g localplane)
"$BUNDLE/bin/localplane-maintenance" install "$BUNDLE"
runuser -u localplane -- env LOCALPLANE_ROOT=/opt/localplane \
  LOCALPLANE_ETC=/etc/localplane LOCALPLANE_VAR=/var/lib/localplane \
  LOCALPLANE_RUN=/run/localplane \
  LOCALPLANE_MAINTENANCE_DIR=/var/lib/localplane/maintenance \
  LOCALPLANE_SERVICE_UID="$LOCALPLANE_SERVICE_UID" \
  LOCALPLANE_SERVICE_GID="$LOCALPLANE_SERVICE_GID" \
  "$RELEASE/bin/localplane-maintenance" initialize
```

Initialization comes before configuration, TLS files, authentication and the store.
Code-only staging in an otherwise empty installation creates no maintenance lock.
After genesis, create the non-secret environment and supply TLS material. This
example uses direct HTTPS on loopback port 9443; check the port is unused first.
Non-loopback binding needs a separately reviewed exposure/address and trusted TLS.

```sh
cat > /etc/localplane/environment <<EOF
LOCALPLANE_DB_PATH=/var/lib/localplane/localplane.db
LOCALPLANE_AUTH_SECRET_PATH=/var/lib/localplane/localplane-master.secret
LOCALPLANE_MAINTENANCE_DIR=/var/lib/localplane/maintenance
LOCALPLANE_AGENT_SOCKET=/run/localplane/agent.sock
LOCALPLANE_RELEASE_MANIFEST=$RELEASE/release.json
LOCALPLANE_CONSOLE_DIR=$RELEASE/console
LOCALPLANE_TLS_CERT_PATH=/etc/localplane/tls.crt
LOCALPLANE_TLS_KEY_PATH=/etc/localplane/tls.key
LOCALPLANE_HOST=127.0.0.1
LOCALPLANE_PORT=9443
EOF
chown root:localplane /etc/localplane/environment
chmod 0640 /etc/localplane/environment
install -o root -g localplane -m 0644 /path/to/operator-tls.crt /etc/localplane/tls.crt
install -o localplane -g localplane -m 0600 /path/to/operator-tls.key /etc/localplane/tls.key
# Run only once, privately; the credential is printed once. Never redirect to a log.
runuser -u localplane -- env \
  LOCALPLANE_AUTH_SECRET_PATH=/var/lib/localplane/localplane-master.secret \
  "$RELEASE/bin/localplane-auth" init
```

Install **only the authoritative templates shipped in the verified release**.
The next example substitutes their documented tokens without replacing or removing
any directives. In particular it preserves `UMask=0077`, both runtime-directory
declarations and `RuntimeDirectoryPreserve=yes`, `Restart=no`, all supplied
`ProtectSystem`, `ProtectHome`, kernel/control-group protections and writable-path
limits. It adds no `Requires` or alternative weaker unit. Do not enable a helper,
Docker-group access, PolicyKit rule or firewall exception alongside the templates.

```sh
python3 - "$RELEASE" <<'PY'
import os, re, stat, sys
from pathlib import Path

if os.geteuid() != 0:
    raise SystemExit('Explicit administrator invocation required')
release = Path(sys.argv[1])
if release.parent != Path('/opt/localplane/releases') or not re.fullmatch(r'[0-9a-f]{64}', release.name):
    raise SystemExit('Expected exact immutable public release path')
mapping = {
    '@UNIT_PREFIX@': 'localplane',
    '@SERVICE_USER@': 'localplane',
    '@SERVICE_GROUP@': 'localplane',
    '@CONFIG_ROOT@': '/etc/localplane',
    '@RELEASE_ROOT@': str(release),
    '@RUNTIME_NAME@': 'localplane',
    '@STATE_ROOT@': '/var/lib/localplane',
    '@RUNTIME_ROOT@': '/run/localplane',
}
unit_dir = Path('/etc/systemd/system')
rendered = {}
for component in ('agent', 'backend'):
    template = release / 'packaging' / ('localplane-' + component + '.service')
    for path in (template, *template.parents, unit_dir, *unit_dir.parents):
        info = path.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise SystemExit('Untrusted template or unit-directory path')
    if not stat.S_ISREG(template.lstat().st_mode) or template.lstat().st_nlink != 1:
        raise SystemExit('Template must be a single-link regular file')
    text = template.read_text(encoding='utf-8')
    for token, value in mapping.items():
        text = text.replace(token, value)
    if re.search(r'@[A-Z_]+@', text):
        raise SystemExit('Unresolved template token; nothing installed')
    target = unit_dir / ('localplane-' + component + '.service')
    if target.exists() or target.is_symlink():
        raise SystemExit('Existing unit: stop and review; no overwrite')
    rendered[target] = text.encode('utf-8')
# Validate both templates before creating either unit. If a later write fails,
# keep services stopped and inspect the partial installation; do not force retry.
for target, body in rendered.items():
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, 'wb') as output:
        os.fchown(output.fileno(), 0, 0)
        os.fchmod(output.fileno(), 0o644)
        output.write(body)
        output.flush()
        os.fsync(output.fileno())
directory_fd = os.open(unit_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
systemctl daemon-reload
systemctl start localplane-agent.service
systemctl start localplane-backend.service
```

These commands do not enable automatic boot startup or alter provider permissions.
Sign in privately at the certificate's matching address and verify the exact loaded
identities. The immutable wrapper in **each unit's ExecStart**, not a release path
written into the environment, dictates that process's imported code and manifest.
The environment selects the compiled console and durable-state paths. Updates,
reverts and restores therefore require **both exact unit ExecStarts and the matching
environment** in the same offline administrator step. A mixed-unit/environment
installation is a failed verification: do not accept it or resume authority.
