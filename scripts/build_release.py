#!/usr/bin/env python3
"""Build a content-addressed Linux release using existing local dependencies.

No package manager, download, installation or service operation is performed.
Use a prepared Python environment matching packaging/runtime-constraints.txt.
The output includes unpacked dependency code, licenses and metadata: a directory
of wheel archives is not a Python runtime. Node is needed only to build assets.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath


class BuildError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BuildError("Only single regular files may enter a release")
    if info.st_size > 64 * 1024 * 1024:
        raise BuildError("Release file exceeds size limit")
    return path.read_bytes()


def put(root, relative, content, executable=False):
    name = PurePosixPath(str(relative))
    if name.is_absolute() or str(name) != str(relative) or any(p in {"..", "."} for p in name.parts):
        raise BuildError("Unsafe release member")
    target = root.joinpath(*name.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_bytes() != content:
        raise BuildError("Conflicting release member")
    target.write_bytes(content)
    target.chmod(0o755 if executable else 0o644)


def read_pins(path):
    pins = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+-]+", line):
            raise BuildError("Runtime constraints must be exact pins")
        name, version = line.split("==")
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        if normalized in pins:
            raise BuildError("Duplicate dependency pin")
        pins[normalized] = version
    if not pins:
        raise BuildError("No runtime dependency pins")
    return pins


def copy_dependencies(root, pins):
    """Copy only pinned distributions; verify their installed RECORD hashes."""
    result = []
    for name, version in sorted(pins.items()):
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            raise BuildError("Required prepared dependency is missing: " + name) from None
        if distribution.version != version or not distribution.files:
            raise BuildError("Prepared dependency version/metadata mismatch: " + name)
        files = {}
        for member in sorted(distribution.files, key=str):
            rel = str(member)
            pure = PurePosixPath(rel)
            # Wheel console-script RECORD entries point outside site-packages. The
            # release supplies its own launchers; never copy outside this payload.
            if pure.is_absolute() or ".." in pure.parts:
                continue
            if "__pycache__" in pure.parts or rel.endswith((".pyc", ".pyo")):
                continue
            if pure.name == "direct_url.json":
                raise BuildError("Editable/local dependency provenance is not a release input")
            if rel.endswith(".pth"):
                raise BuildError("Dependency startup path injection is unsupported")
            content = safe_file(Path(distribution.locate_file(member)))
            if member.hash is not None:
                observed = base64.urlsafe_b64encode(hashlib.new(member.hash.mode, content).digest()).decode().rstrip("=")
                if observed != member.hash.value:
                    raise BuildError("Prepared dependency RECORD mismatch: " + name)
            elif pure.name != "RECORD":
                raise BuildError("Unhashed dependency payload member: " + name)
            files[rel] = hashlib.sha256(content).hexdigest()
            put(root, "vendor/" + rel, content)
        if not files:
            raise BuildError("Dependency payload is empty: " + name)
        result.append({"name": name, "version": version, "sha256": digest(files)})
    return result


ASSET = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*-[A-Za-z0-9_-]{8,}\.(?:js|css|woff2?|ttf|otf|svg|png|jpe?g|gif|webp|avif|ico)")


class Index(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = set()
        self.module = False
        self.root = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "base":
            raise BuildError("Console must use same-origin root")
        self.root |= tag == "div" and values.get("id") == "root"
        for key in ("src", "href"):
            if key in values:
                value = values[key] or ""
                if not value.startswith("/assets/") or not ASSET.fullmatch(value[8:]):
                    raise BuildError("Console contains a non-bundle reference")
                self.references.add(value[1:])
        if tag == "script":
            if values.get("type") != "module" or not str(values.get("src", "")).endswith(".js"):
                raise BuildError("Console must contain compiled module scripts")
            self.module = True


def copy_console(root, source):
    if source.is_symlink() or (source / "assets").is_symlink():
        raise BuildError("Console directory links are refused")
    index = safe_file(source / "index.html")
    if not index or len(index) > 1024 * 1024:
        raise BuildError("Console index is empty or oversized")
    parser = Index()
    parser.feed(index.decode("utf-8"))
    hashes = {"index.html": hashlib.sha256(index).hexdigest()}
    put(root, "console/index.html", index)
    total = len(index)
    entries = sorted((source / "assets").iterdir())
    if len(entries) > 512:
        raise BuildError("Console contains too many assets")
    for entry in entries:
        if entry.is_symlink():
            raise BuildError("Console asset links are refused")
        if not ASSET.fullmatch(entry.name):
            continue  # Source maps and other non-public inputs are never shipped.
        data = safe_file(entry)
        total += len(data)
        if not data or len(data) > 16 * 1024 * 1024 or total > 64 * 1024 * 1024:
            raise BuildError("Console asset is empty or oversized")
        relative = "assets/" + entry.name
        hashes[relative] = hashlib.sha256(data).hexdigest()
        put(root, "console/" + relative, data)
    if not parser.root or not parser.module or not parser.references <= hashes.keys():
        raise BuildError("Console build is incomplete")
    return "sha256:" + digest(hashes)


def source_identity(repo):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()
    commit = git("rev-parse", "HEAD")
    # Private artifacts are not part of product source. Tracked changes and new
    # public product paths are part of this exact candidate; never claim base HEAD
    # is the new source tree when they exist.
    changed = git("status", "--porcelain", "--untracked-files=all", "--", ".", ":(exclude).openchamber")
    dirty = bool(changed)
    return {"source_commit": commit, "source_tree": None if dirty else git("rev-parse", "HEAD^{tree}"), "source_dirty": dirty}


def write_launchers(root):
    common = ('#!/bin/sh\nset -eu\n'
              'ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)\n'
              'export PYTHONPATH="$ROOT/vendor:$ROOT/src"\n'
              'export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1\n'
              'export LOCALPLANE_RELEASE_ROOT="$ROOT" LOCALPLANE_RELEASE_MANIFEST="$ROOT/release.json"\n')
    for command, module in (("agent", "localplane.agent"), ("backend", "localplane.backend"),
                            ("maintenance", "localplane.maintenance_cli")):
        put(root, "bin/localplane-" + command,
            (common + 'exec /usr/bin/python3 -s -m ' + module + ' "$@"\n').encode(), True)
    put(root, "bin/localplane-auth", (common + 'exec /usr/bin/python3 -s -c \'from localplane.backend.auth_cli import main; raise SystemExit(main())\' "$@"\n').encode(), True)


def write_tar(root, target, arcname):
    with target.open("xb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for path in [root, *sorted(root.rglob("*"))]:
                    name = arcname if path == root else arcname + "/" + path.relative_to(root).as_posix()
                    info = archive.gettarinfo(str(path), arcname=name)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    if path.is_dir():
                        info.mode = 0o755
                        archive.addfile(info)
                    else:
                        info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)


def build_release(repo, output, *, console=None, compile_console=True):
    repo, output = Path(repo).absolute(), Path(output).absolute()
    if os.geteuid() == 0:
        raise BuildError("Build as an ordinary user, not root")
    if ":" in str(output) or ":" in str(repo):
        raise BuildError("Colon paths cannot be used in Python import paths")
    if compile_console:
        subprocess.run(["npm", "run", "build"], cwd=repo / "web", check=True)
    console = Path(console) if console else repo / "web/dist"
    pins = read_pins(repo / "packaging/runtime-constraints.txt")
    identity = source_identity(repo)
    output.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".partial-", dir=output))
    try:
        for path in sorted((repo / "src/localplane").rglob("*")):
            if path.is_symlink():
                raise BuildError("Source links are refused")
            if path.is_file() and path.suffix in {".py", ".sql"} and "__pycache__" not in path.parts:
                put(stage, path.relative_to(repo).as_posix(), safe_file(path))
        for required in ("backend/app.py", "agent/__main__.py", "backend/auth_cli.py",
                         "maintenance.py", "maintenance_cli.py", "build_identity.py"):
            if not (stage / "src/localplane" / required).is_file():
                raise BuildError("Runtime source is incomplete: " + required)
        dependencies = copy_dependencies(stage, pins)
        console_digest = copy_console(stage, console)
        for name in ("localplane-agent.service", "localplane-backend.service", "runtime-constraints.txt", "README.md"):
            put(stage, "packaging/" + name, safe_file(repo / "packaging" / name))
        for name in ("installation.md", "maintenance-and-update.md"):
            put(stage, "docs/" + name, safe_file(repo / "docs" / name))
        put(stage, "LICENSE", safe_file(repo / "LICENSE"))
        write_launchers(stage)
        files = {p.relative_to(stage).as_posix(): file_hash(p) for p in sorted(stage.rglob("*")) if p.is_file()}
        migrations = {p.name: file_hash(p) for p in sorted((stage / "src/localplane/backend/db/migrations").glob("*.sql"))}
        if not migrations:
            raise BuildError("Release has no schema migration contract")
        version = tomllib.loads((repo / "pyproject.toml").read_text())["project"]["version"]
        os_release = platform.freedesktop_os_release()
        if not os_release.get("ID") or not os_release.get("VERSION_ID"):
            raise BuildError("Build OS identity is unavailable")
        manifest = {"format_version": 1, "version": version, **identity, "files": files,
                    "migrations": migrations, "console_digest": console_digest,
                    "source_digest": "sha256:" + digest({"files": files, "migrations": migrations, "console_digest": console_digest}),
                    "dependencies": dependencies, "platform": {"os": os_release["ID"] + ":" + os_release["VERSION_ID"],
                    "architecture": platform.machine(), "python": platform.python_version()}}
        manifest["build_id"] = "sha256:" + digest(manifest)
        put(stage, "release.json", canonical(manifest) + b"\n")
        slug = manifest["build_id"][7:]
        destination = output / slug
        if destination.exists() or (output / (slug + ".tar.gz")).exists():
            raise BuildError("Release output already exists; it was not overwritten")
        for directory in [stage, *(p for p in stage.rglob("*") if p.is_dir())]:
            directory.chmod(0o755)
        # Paths, modes and tar metadata are deterministic. The partial directory is
        # retained on failure so it cannot be mistaken for a verified installed build.
        write_tar(stage, output / (slug + ".tar.gz"), slug)
        stage.rename(destination)
        return destination
    except BaseException:
        # Keep recognisably partial artifacts for inspection; never delete an
        # existing candidate or unrelated output as a build side effect.
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compiled-console", type=Path, help="Use an explicitly precompiled console instead of invoking npm")
    args = parser.parse_args()
    try:
        result = build_release(args.repo, args.output, console=args.compiled_console,
                               compile_console=args.compiled_console is None)
    except (BuildError, OSError, ValueError, subprocess.SubprocessError) as error:
        print("Build refused: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps({"bundle": str(result), "build_id": "sha256:" + result.name}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
