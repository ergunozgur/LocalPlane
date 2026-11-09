"""Offline build contracts. No npm, pip, service or live provider call."""
import base64
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("release_builder", Path(__file__).parents[1] / "scripts/build_release.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def console(tmp_path):
    root = tmp_path / "compiled"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text('<html><script type="module" src="/assets/main-Abcd1234.js"></script><div id="root"></div></html>')
    (root / "assets/main-Abcd1234.js").write_text('console.log("fixture");')
    (root / "assets/main-Abcd1234.js.map").write_text("not public")
    return root


def source(tmp_path):
    root = tmp_path / "repo"
    (root / "src/localplane/backend/db/migrations").mkdir(parents=True)
    (root / "src/localplane/backend/app.py").write_text("# fixture\n")
    (root / "src/localplane/backend/auth_cli.py").write_text("# fixture\n")
    (root / "src/localplane/agent").mkdir()
    (root / "src/localplane/agent/__main__.py").write_text("# fixture\n")
    for name in ("maintenance.py", "maintenance_cli.py", "build_identity.py"):
        (root / "src/localplane" / name).write_text("# fixture\n")
    (root / "src/localplane/backend/db/migrations/0001_initial.sql").write_text("CREATE TABLE fixture(value TEXT);\n")
    (root / "packaging").mkdir()
    (root / "packaging/runtime-constraints.txt").write_text("fixture==1.0\n")
    (root / "packaging/README.md").write_text("# Build fixture\n")
    for name in ("localplane-agent.service", "localplane-backend.service"):
        (root / "packaging" / name).write_text("[Service]\nExecStart=@RELEASE_ROOT@/bin/localplane-agent\n")
    (root / "LICENSE").write_text("fixture license\n")
    (root / "docs").mkdir()
    for name in ("installation.md", "maintenance-and-update.md"):
        (root / "docs" / name).write_text("# Fixture instructions\n")
    (root / "pyproject.toml").write_text('[project]\nversion="1.0"\n')
    return root


def fake_dependencies(root, pins):
    assert pins == {"fixture": "1.0"}
    builder.put(root, "vendor/fixture.py", b"VALUE=1\n")
    return [{"name": "fixture", "version": "1.0", "sha256": "1" * 64}]


def test_content_addressed_tar_is_reproducible_and_dirty_identity_honest(tmp_path, monkeypatch):
    repo, compiled = source(tmp_path), console(tmp_path)
    monkeypatch.setattr(builder, "copy_dependencies", fake_dependencies)
    monkeypatch.setattr(builder, "source_identity", lambda _: {
        "source_commit": "a" * 40, "source_tree": None, "source_dirty": True})
    a = builder.build_release(repo, tmp_path / "out-a", console=compiled, compile_console=False)
    b = builder.build_release(repo, tmp_path / "out-b", console=compiled, compile_console=False)
    assert a.name == b.name
    assert a.with_suffix(".tar.gz").read_bytes() == b.with_suffix(".tar.gz").read_bytes()
    manifest = json.loads((a / "release.json").read_text())
    assert manifest["source_dirty"] and manifest["source_tree"] is None
    assert manifest["build_id"] == "sha256:" + a.name
    payload = {key: value for key, value in manifest.items() if key != "build_id"}
    assert builder.digest(payload) == a.name
    assert "assets/main-Abcd1234.js.map" not in manifest["files"]
    assert not (a / "console/assets/main-Abcd1234.js.map").exists()
    assert manifest["migrations"] == {"0001_initial.sql": builder.file_hash(a / "src/localplane/backend/db/migrations/0001_initial.sql")}
    assert (a.stat().st_mode & 0o777) == 0o755
    with pytest.raises(builder.BuildError, match="already exists"):
        builder.build_release(repo, tmp_path / "out-a", console=compiled, compile_console=False)
    assert a.is_dir()  # Existing release is retained, never cleaned to retry.


def test_missing_or_invalid_assets_fail_closed_and_partial_is_retained(tmp_path, monkeypatch):
    repo, compiled = source(tmp_path), console(tmp_path)
    monkeypatch.setattr(builder, "copy_dependencies", fake_dependencies)
    monkeypatch.setattr(builder, "source_identity", lambda _: {
        "source_commit": "a" * 40, "source_tree": None, "source_dirty": True})
    (compiled / "assets/main-Abcd1234.js").unlink()
    output = tmp_path / "output"
    with pytest.raises(builder.BuildError, match="incomplete"):
        builder.build_release(repo, output, console=compiled, compile_console=False)
    assert list(output.glob(".partial-*"))
    assert not list(output.glob("*.tar.gz"))


def test_console_links_and_external_references_refused(tmp_path):
    compiled = console(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    (compiled / "index.html").write_text('<script type="module" src="https://unexpected.invalid/a.js"></script>')
    with pytest.raises(builder.BuildError, match="non-bundle"):
        builder.copy_console(stage, compiled)
    (compiled / "index.html").unlink()
    (compiled / "index.html").symlink_to(compiled / "assets/main-Abcd1234.js")
    with pytest.raises(builder.BuildError, match="regular"):
        builder.copy_console(stage, compiled)


def test_constraints_require_nonempty_exact_unique_pins(tmp_path):
    path = tmp_path / "pins"
    for text in ("", "fastapi>=0.1\n", "fixture==1\nfixture==1\n"):
        path.write_text(text)
        with pytest.raises(builder.BuildError):
            builder.read_pins(path)
    path.write_text("typing_extensions==4.16.0\n")
    assert builder.read_pins(path) == {"typing-extensions": "4.16.0"}


def test_installed_payload_record_integrity_and_version_checked(tmp_path, monkeypatch):
    package = tmp_path / "installed"
    package.mkdir()
    (package / "fixture.py").write_bytes(b"VALUE=1\n")
    member = SimpleNamespace(hash=SimpleNamespace(mode="sha256", value=base64.urlsafe_b64encode(hashlib.sha256(b"VALUE=1\n").digest()).decode().rstrip("=")))
    class Member(PurePosixPath):
        @property
        def hash(self):
            return member.hash
    distribution = SimpleNamespace(version="1.0", files=[Member("fixture.py")], locate_file=lambda p: package / str(p))
    monkeypatch.setattr(builder.metadata, "distribution", lambda _: distribution)
    stage = tmp_path / "stage"
    stage.mkdir()
    result = builder.copy_dependencies(stage, {"fixture": "1.0"})
    assert result[0]["sha256"]
    assert (stage / "vendor/fixture.py").read_bytes() == b"VALUE=1\n"
    (package / "fixture.py").write_bytes(b"TAMPERED\n")
    with pytest.raises(builder.BuildError, match="RECORD mismatch"):
        builder.copy_dependencies(stage, {"fixture": "1.0"})
    with pytest.raises(builder.BuildError, match="version"):
        builder.copy_dependencies(stage, {"fixture": "2.0"})


def test_launcher_uses_its_own_relocated_source_and_unpacked_vendor(tmp_path):
    root = tmp_path / "relocated-release"
    (root / "src/localplane").mkdir(parents=True)
    (root / "vendor").mkdir()
    (root / "src/localplane/__init__.py").write_text("")
    (root / "vendor/bundle_fixture.py").write_text("VALUE=42\n")
    (root / "src/localplane/maintenance_cli.py").write_text(
        "import bundle_fixture,os\nprint(bundle_fixture.VALUE)\nprint(os.environ['LOCALPLANE_RELEASE_ROOT'])\n")
    builder.write_launchers(root)
    result = subprocess.run([str(root / "bin/localplane-maintenance")], capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ["42", str(root)]


def test_service_templates_have_no_privilege_or_hidden_install_hooks():
    repo = Path(__file__).parents[1]
    for filename in ("localplane-agent.service", "localplane-backend.service"):
        text = (repo / "packaging" / filename).read_text()
        assert "User=@SERVICE_USER@" in text
        assert "Group=@SERVICE_GROUP@" in text
        assert "ExecStart=@RELEASE_ROOT@/bin/localplane-" in text
        assert "NoNewPrivileges=yes" in text and "ProtectSystem=strict" in text
        assert "RuntimeDirectoryMode=0700" in text
        assert "Restart=no" in text
        assert all(term not in text for term in ("ExecStartPre", "sudo", "SupplementaryGroups", "docker.sock", "polkit"))
