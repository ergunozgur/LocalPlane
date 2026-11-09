# Release bundles

The build command runs as an ordinary user. It does not install dependencies,
download packages, elevate privileges, change services, or initialize a database
or credential. Prepare its inputs explicitly before building:

```sh
python3.12 -m venv "$HOME/localplane-buildenv"
"$HOME/localplane-buildenv/bin/python" -m pip install -r packaging/runtime-constraints.txt
cd web
npm ci
cd ..
"$HOME/localplane-buildenv/bin/python" scripts/build_release.py --output "$HOME/localplane-bundles"
```

These are operator-run preparation commands, not actions hidden in the builder.
An already prepared environment with the exact pins can be reused instead.
The default builder runs `npm run build`; `--compiled-console DIRECTORY` instead
uses an explicitly supplied compiled build. Do not substitute assets from another
source revision when validating an update.

The current dependency profile is Linux aarch64 with CPython 3.12. The manifest
records the actual build OS, architecture and Python version. A successful build
does not establish installation support on another ABI or platform, or on a clean
OS. Target-side prerequisite/import checks and installation validation are separate.

Each result contains:

- `src/localplane`: the runtime package and unchanged numbered SQL migrations;
- `vendor`: unpacked, version-pinned dependencies with licenses and distribution
  metadata, copied only after checking their installed RECORD hashes;
- `console`: compiled same-origin assets, excluding source maps;
- `bin`: launchers bound to their own immutable release directory;
- `packaging`: two service templates and the dependency constraints;
- `release.json`: component/source/dependency hashes and build identity.

No development server, editable checkout, build environment or Node runtime is
needed by the resulting services. Installed code is root-owned and separate from
the service-owned durable state. The service templates grant neither Docker group
membership, a privileged helper, nor systemd lifecycle policy.

Build IDs use `sha256:…` in diagnostics. Directory and archive names use only the
64 hexadecimal characters: a colon in a directory would break `PYTHONPATH`.
Dirty source is supported for validation, but is explicitly marked dirty with no
claim that its base commit is the candidate tree. The manifest hashes identify the
actual shipped bytes. Identical source identity, console and dependency inputs on the same
build platform produce identical manifests and archives, without timestamp-based
version identities. Existing output is never overwritten; failed output stays
recognisably `.partial-*` and must not be installed.

Bundle hashes establish integrity and identity, not publisher authentication.
Install only an explicitly trusted bundle. Before activation, verify its manifest,
all payload hashes, target ABI, dependency imports, permissions, compiled assets
and TLS configuration. Follow the installation and maintenance documentation for
the separate administrator steps, maintenance admission, consistent backup and
same-window recovery. A build command is not an installer or an update approval.
