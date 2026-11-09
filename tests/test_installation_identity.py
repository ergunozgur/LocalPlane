import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from localplane.build_identity import canonical_digest, load_build_identity


def test_staged_inspection_is_not_running_identity_and_migrations_are_bound(tmp_path):
    name = "0001_initial.sql"
    relative = "src/localplane/backend/db/migrations/" + name
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"select 1;\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    files = {relative: digest}
    migrations = {name: digest}
    console = canonical_digest({})
    source = canonical_digest({"files": files, "migrations": migrations,
                               "console_digest": console})
    manifest = {"format_version": 1, "version": "1", "source_commit": "a" * 40,
                "source_tree": None, "source_dirty": True, "source_digest": source,
                "files": files, "migrations": migrations, "dependencies": [],
                "platform": {"os": "Linux", "architecture": "test", "python": "3"},
                "console_digest": console}
    manifest["build_id"] = canonical_digest(manifest)
    manifest_path = tmp_path / "release.json"
    manifest_path.write_text(json.dumps(manifest))
    manifest_path.chmod(0o600)
    staged = load_build_identity(manifest_path, process="inspection", inspect_only=True)
    assert staged.status == "inspected"
    assert not staged.packaged
    runtime = load_build_identity(manifest_path, process="backend")
    assert runtime.status == "invalid"
    assert "release_runtime_module_root_mismatch" in runtime.reasons
    path.write_text("altered")
    assert load_build_identity(manifest_path, process="inspection", inspect_only=True).status == "invalid"


def test_runtime_identity_requires_the_actually_imported_release(tmp_path):
    import localplane
    import localplane.build_identity as identity_module

    root = tmp_path / "release"
    package = root / "src" / "localplane"
    package.mkdir(parents=True)
    files = {}
    for name, source in (("__init__.py", localplane.__file__),
                         ("build_identity.py", identity_module.__file__)):
        data = Path(source).read_bytes()
        (package / name).write_bytes(data)
        files["src/localplane/" + name] = hashlib.sha256(data).hexdigest()
    console = canonical_digest({})
    manifest = {"format_version": 1, "version": "1", "source_commit": "a" * 40,
                "source_tree": None, "source_dirty": True, "files": files,
                "migrations": {}, "dependencies": [],
                "platform": {"os": "Linux", "architecture": "test", "python": "3"},
                "console_digest": console,
                "source_digest": canonical_digest({"files": files, "migrations": {},
                                                   "console_digest": console})}
    manifest["build_id"] = canonical_digest(manifest)
    path = root / "release.json"
    path.write_text(json.dumps(manifest))
    path.chmod(0o600)
    script = ("from localplane.build_identity import load_build_identity; "
              "import sys; x=load_build_identity(sys.argv[1],process='backend'); "
              "print(x.status, *x.reasons)")
    env = {**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-c", script, str(path)], cwd=tmp_path,
                            env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "packaged"
    (package / "__init__.py").write_text("# altered\n")
    result = subprocess.run([sys.executable, "-c", script, str(path)], cwd=tmp_path,
                            env=env, capture_output=True, text=True, check=True)
    assert result.stdout.startswith("invalid ")
