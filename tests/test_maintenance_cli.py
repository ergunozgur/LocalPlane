"""Disposable SQLite/filesystem tests against the shared maintenance controller.

Focused sequencing doubles remain explicitly labelled. No root/service operations,
external source-tree imports, real database or real authentication reads occur here.
"""
from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from localplane import maintenance_cli as cli
from localplane.maintenance import MaintenanceController, MaintenanceRefused


BUILD = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
CHECKSUM = hashlib.sha256(b"CREATE TABLE fixture(id INTEGER);\n").hexdigest()
MIGRATIONS = {"0001_init.sql": CHECKSUM}
VERIFY_RELEASE = cli.verify_release
CONTROLLER = cli.controller


def make_db(path):
    cli.atomic_bytes(path, b"")
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT, checksum TEXT);
        CREATE TABLE records(id INTEGER PRIMARY KEY, outcome TEXT, held INTEGER);
        INSERT INTO records VALUES(1, 'unknown', 1);
    """)
    connection.execute("INSERT INTO schema_migrations VALUES(1,'init',?)", (CHECKSUM,))
    connection.commit()
    connection.close()


class ControllerDouble:
    """Models API sequencing only, without claiming real service/executor exclusion."""
    def __init__(self):
        self.window = None
        self.generation = 0
        self.leased = False
        self.busy = False
        self.pending = None
        self.in_restore = False

    def state(self):
        return SimpleNamespace(window=self.window,
            status="unknown" if self.pending else ("active" if self.window and self.window.active else "closed"),
            refusal={"code": "restore_pending"} if self.pending else None,
            as_dict=lambda: {
            "active": bool(self.window and self.window.active),
            "window": self.window.as_dict() if self.window else None,
        })

    def open_window(self, **_):
        if self.window and self.window.active:
            cli.refuse("active", "Already active")
        self.generation += 1
        self.window = SimpleNamespace(window_id=f"w_{self.generation}", generation=self.generation,
                                      active=True, snapshot_id=None)
        self.window.as_dict = lambda: {k: v for k, v in vars(self.window).items() if k != "as_dict"}
        return self.window

    def require(self, window):
        if not self.window or not self.window.active or self.window.window_id != window:
            cli.refuse("window_mismatch", "Not the active window")

    @contextmanager
    def snapshot_lease(self, window):
        self.require(window)
        if self.pending and not self.in_restore:
            cli.refuse("restore_pending", "Restore is pending")
        if self.busy:
            cli.refuse("runtime_busy", "Runtime lease held")
        assert not self.leased
        self.leased = True
        try:
            yield self.window
        finally:
            self.leased = False

    @contextmanager
    def restore_lease(self, snapshot):
        self.require(snapshot["window_id"])
        if snapshot["generation"] != self.window.generation:
            cli.refuse("window_mismatch", "Wrong generation")
        with self.snapshot_lease(snapshot["window_id"]) as window:
            yield window

    def record_snapshot(self, window, snapshot):
        assert not self.leased, "Controller transition locks are non-reentrant"
        self.require(window)
        self.window.snapshot_id = snapshot
        return self.window

    def close_window(self, window, **_):
        self.require(window)
        if self.pending:
            cli.refuse("restore_pending", "Restore is pending")
        self.window.active = False
        return self.window

    @contextmanager
    def restore_transaction(self, snapshot, *, snapshot_id):
        self.require(snapshot["window_id"])
        binding = (snapshot["window_id"], snapshot["generation"], snapshot_id)
        if self.pending is not None and self.pending != binding:
            cli.refuse("restore_pending_mismatch", "Pending restore binding differs or is unknown")
        self.in_restore = True
        try:
            with self.restore_lease(snapshot) as window:
                self.pending = binding
                def complete():
                    self.pending = None
                yield SimpleNamespace(window=window, transaction_id="synthetic-restore-stable-1", complete=complete)
        finally:
            self.in_restore = False


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    paths = {key: tmp_path / key for key in ("root", "etc", "var", "run")}
    for key, path in paths.items():
        path.mkdir(mode=0o700)
        monkeypatch.setenv("LOCALPLANE_" + key.upper(), str(path))
    maintenance = paths["var"] / "maintenance"
    maintenance.mkdir(mode=0o700)
    monkeypatch.setenv("LOCALPLANE_MAINTENANCE_DIR", str(maintenance))
    monkeypatch.setenv("LOCALPLANE_SERVICE_UID", str(os.geteuid()))
    monkeypatch.setenv("LOCALPLANE_SERVICE_GID", str(os.getegid()))
    # Initialize the actual shared controller BEFORE creating store/config/auth.
    call(paths, "initialize")
    releases = {}
    (paths["root"] / "releases").mkdir(mode=0o755)
    for build in (BUILD, OTHER):
        release = cli.release_path(paths, build)
        release.mkdir(mode=0o755)
        releases[str(release)] = {"build_id": build, "migrations": dict(MIGRATIONS),
                                  "source_commit": "c" * 40, "source_dirty": True,
                                  "source_digest": "sha256:" + "d" * 64}
    # Explicit unit-test seam: no root ownership or real identity validation claimed.
    monkeypatch.setattr(cli, "verify_release", lambda root, **_: releases[str(root)])
    ctl = ControllerDouble()
    monkeypatch.setattr(cli, "controller", lambda _: ctl)
    env = {
        "LOCALPLANE_DB_PATH": paths["var"] / "localplane.db",
        "LOCALPLANE_AUTH_SECRET_PATH": paths["var"] / "localplane-master.secret",
        "LOCALPLANE_MAINTENANCE_DIR": maintenance,
        "LOCALPLANE_AGENT_SOCKET": paths["run"] / "agent.sock",
        "LOCALPLANE_RELEASE_MANIFEST": cli.release_path(paths, BUILD) / "release.json",
        "LOCALPLANE_CONSOLE_DIR": cli.release_path(paths, BUILD) / "console",
        "LOCALPLANE_TLS_CERT_PATH": paths["etc"] / "tls.crt",
        "LOCALPLANE_TLS_KEY_PATH": paths["etc"] / "tls.key",
    }
    cli.atomic_bytes(paths["etc"] / "environment", "".join(f"{k}={v}\n" for k, v in env.items()).encode())
    for name in ("tls.crt", "tls.key"):
        cli.atomic_bytes(paths["etc"] / name, b"SYNTHETIC TLS FIXTURE")
    cli.atomic_bytes(paths["var"] / "localplane-master.secret", base64.urlsafe_b64encode(b"S" * 32).rstrip(b"=") + b"\n")
    make_db(paths["var"] / "localplane.db")
    return paths, ctl, releases


def call(paths, *argv):
    return cli.execute(cli.build_parser().parse_args(list(argv)), paths)


def backup(paths, ctl):
    window = call(paths, "open")["window_id"]
    name = call(paths, "backup", "--window", window)["snapshot"]
    return window, name


def test_plan_dry_run_does_not_touch_state(fixture, monkeypatch):
    paths, _, _ = fixture
    before = cli.inventory(paths["var"])
    monkeypatch.setattr(cli, "controller", lambda _: pytest.fail("No controller for plan"))
    assert "SQLite backup API" in call(paths, "plan", "restore")["plan"]
    call(paths, "--dry-run", "backup", "--window", "unknown")
    assert cli.inventory(paths["var"]) == before


@pytest.mark.parametrize("command", ["check", "status"])
def test_inspection_never_opens_database_or_secret(fixture, monkeypatch, command):
    paths, _, _ = fixture
    before = cli.inventory(paths["var"])
    original = Path.read_bytes
    def screened(path):
        if path.name in {"localplane.db", "localplane-master.secret", "tls.key"}:
            pytest.fail("Inspection read protected data")
        return original(path)
    with monkeypatch.context() as inspection:
        inspection.setattr(Path, "read_bytes", screened)
        inspection.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("Inspection opened SQLite"))
        result = call(paths, command)
        assert result["source_dirty"] is True
        assert "unknown" in result["runtime"]
    assert cli.inventory(paths["var"]) == before


def test_backup_and_actual_same_window_restore_preserve_holds_config_secret(fixture):
    paths, ctl, _ = fixture
    original_config = cli.inventory(paths["etc"])
    secret = (paths["var"] / "localplane-master.secret").read_bytes()
    window, name = backup(paths, ctl)
    source = paths["var"] / "localplane.db"
    with sqlite3.connect(source) as db:
        db.execute("UPDATE records SET outcome='failed', held=0")
    cli.atomic_bytes(paths["etc"] / "tls.key", b"REPLACED SYNTHETIC KEY")
    result = call(paths, "restore", "--window", window, "--snapshot", name)
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT outcome, held FROM records").fetchall() == [("unknown", 1)]
    assert cli.inventory(paths["etc"]) == original_config
    assert (paths["var"] / "localplane-master.secret").read_bytes() == secret
    retained = Path(result["displaced"])
    assert (retained / "original/displaced.json").is_file()
    with sqlite3.connect(retained / "original/raw-localplane.db") as db:
        assert db.execute("SELECT outcome FROM records").fetchone() == ("failed",)
    assert ctl.window.active
    assert not any(p.name == "window.json" for p in cli.snapshot_path(paths, name).rglob("*"))


def test_wal_backup_includes_committed_rows_without_raw_sidecars(fixture, tmp_path):
    paths, _, _ = fixture
    source = paths["var"] / "localplane.db"
    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO records VALUES(2,'held',1)")
        writer.commit()
        assert Path(str(source) + "-wal").stat().st_size > 0
        target = tmp_path / "coherent.db"
        cli.sqlite_backup(source, target)
        with sqlite3.connect(target) as db:
            assert db.execute("SELECT COUNT(*) FROM records").fetchone() == (2,)
        assert not Path(str(target) + "-wal").exists()
    finally:
        writer.close()


@pytest.mark.parametrize("reopen", [False, True])
def test_accept_rejects_post_resume_and_closed_reopened_restore(fixture, reopen):
    paths, ctl, _ = fixture
    window, name = backup(paths, ctl)
    call(paths, "accept", "--window", window, "--verified")
    if reopen:
        call(paths, "open")
    before = cli.inventory(paths["etc"])
    with pytest.raises(cli.MaintenanceError):
        call(paths, "restore", "--window", window, "--snapshot", name)
    assert cli.inventory(paths["etc"]) == before


def test_accept_requires_explicit_operator_verification(fixture):
    paths, ctl, _ = fixture
    window, _ = backup(paths, ctl)
    with pytest.raises(cli.MaintenanceError, match="verified"):
        call(paths, "accept", "--window", window)
    assert ctl.window.active


def test_revert_checks_current_not_snapshot_schema_before_activation(fixture):
    paths, ctl, releases = fixture
    window, name = backup(paths, ctl)
    with sqlite3.connect(paths["var"] / "localplane.db") as db:
        db.execute("INSERT INTO schema_migrations VALUES(2,'new',?)", ("e" * 64,))
    before = (paths["etc"] / "environment").read_bytes()
    with pytest.raises(cli.MaintenanceError, match="current recorded schema"):
        call(paths, "revert", OTHER, "--window", window, "--snapshot", name)
    assert (paths["etc"] / "environment").read_bytes() == before
    # Recovery validates the saved schema, NOT the failed current store against old code.
    call(paths, "restore", "--window", window, "--snapshot", name)
    assert cli.schema(paths["var"] / "localplane.db") == MIGRATIONS
    assert ctl.window.active


def test_compatible_revert_really_selects_exact_code_and_console(fixture):
    paths, ctl, _ = fixture
    window, name = backup(paths, ctl)
    call(paths, "revert", OTHER, "--window", window, "--snapshot", name)
    env, _ = cli.configuration(paths)
    assert env["LOCALPLANE_RELEASE_MANIFEST"] == str(cli.release_path(paths, OTHER) / "release.json")
    assert env["LOCALPLANE_CONSOLE_DIR"] == str(cli.release_path(paths, OTHER) / "console")
    assert ctl.window.active


def test_restore_corrupt_failed_store_retains_raw_evidence(fixture):
    paths, ctl, _ = fixture
    window, name = backup(paths, ctl)
    cli.atomic_bytes(paths["var"] / "localplane.db", b"CORRUPT SYNTHETIC SQLITE")
    result = call(paths, "restore", "--window", window, "--snapshot", name)
    assert (Path(result["displaced"]) / "original/raw-localplane.db").read_bytes() == b"CORRUPT SYNTHETIC SQLITE"
    assert cli.schema(paths["var"] / "localplane.db") == MIGRATIONS


def test_tampered_snapshot_refused_before_mutation(fixture):
    paths, ctl, _ = fixture
    window, name = backup(paths, ctl)
    cli.atomic_bytes(cli.snapshot_path(paths, name) / "config/tls.key", b"TAMPERED")
    before = cli.inventory(paths["etc"])
    with pytest.raises(cli.MaintenanceError, match="inventory"):
        call(paths, "restore", "--window", window, "--snapshot", name)
    assert cli.inventory(paths["etc"]) == before
    assert not (paths["var"] / "displaced").exists()


def test_runtime_busy_refuses_backup_without_reading_store(fixture, monkeypatch):
    paths, ctl, _ = fixture
    window = call(paths, "open")["window_id"]
    ctl.busy = True
    monkeypatch.setattr(cli, "capture", lambda *a, **k: pytest.fail("Lease not acquired"))
    with pytest.raises(cli.MaintenanceError, match="Runtime lease"):
        call(paths, "backup", "--window", window)


def test_cli_lock_is_stable_and_exclusive(fixture):
    paths, _, _ = fixture
    with cli.command_lock(paths):
        inode = (paths["var"] / "maintenance/cli.lock").stat().st_ino
        with pytest.raises(cli.MaintenanceError, match="Another maintenance"):
            with cli.command_lock(paths):
                pytest.fail("Concurrent command admitted")
    with cli.command_lock(paths):
        assert (paths["var"] / "maintenance/cli.lock").stat().st_ino == inode


@pytest.mark.parametrize("name", ["../escape", "/absolute", "snapshot_bad"])
def test_snapshot_path_traversal_refused(fixture, name):
    with pytest.raises(cli.MaintenanceError):
        cli.snapshot_path(fixture[0], name)


def test_symlink_and_hardlink_state_refused(fixture):
    paths, _, _ = fixture
    secret = paths["var"] / "localplane-master.secret"
    link = paths["var"] / "alias"
    link.symlink_to(secret)
    with pytest.raises(cli.MaintenanceError):
        cli.regular(link)
    link.unlink()
    os.link(secret, link)
    with pytest.raises(cli.MaintenanceError):
        cli.regular(secret)


def test_repeat_install_does_not_rewrite_state_or_configuration(fixture, monkeypatch):
    paths, _, _ = fixture
    target = cli.release_path(paths, BUILD)
    monkeypatch.setattr(cli, "stage", lambda *a: target)
    before = cli.inventory(paths["var"]), cli.inventory(paths["etc"])
    call(paths, "install", str(target))
    call(paths, "install", str(target))
    # Exclude the newly created stable CLI lock, never auth/database state.
    after = cli.inventory(paths["var"])
    after.pop("maintenance/cli.lock", None)
    assert (after, cli.inventory(paths["etc"])) == before


def test_update_really_selects_staged_target_and_retains_configuration(fixture, monkeypatch):
    paths, ctl, _ = fixture
    window, name = backup(paths, ctl)
    target = cli.release_path(paths, OTHER)
    monkeypatch.setattr(cli, "stage", lambda *a: target)
    call(paths, "update", str(target), "--window", window, "--snapshot", name)
    assert cli.configuration(paths)[0]["LOCALPLANE_RELEASE_MANIFEST"] == str(target / "release.json")
    assert list((paths["var"] / "displaced").glob("activation_*/environment"))
    assert ctl.window.active


def test_interrupted_activation_retains_fence_and_snapshot(fixture, monkeypatch):
    paths, ctl, _ = fixture
    window, name = backup(paths, ctl)
    before = (paths["etc"] / "environment").read_bytes()
    monkeypatch.setattr(cli, "activate", lambda *a: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError):
        call(paths, "revert", OTHER, "--window", window, "--snapshot", name)
    assert ctl.window.active
    assert cli.verify_snapshot(paths, name)
    assert (paths["etc"] / "environment").read_bytes() == before


def test_config_drift_and_invalid_secret_refuse_backup(fixture):
    paths, ctl, _ = fixture
    window = call(paths, "open")["window_id"]
    cli.atomic_bytes(paths["var"] / "localplane-master.secret", b"not-a-valid-master-secret")
    with pytest.raises(cli.MaintenanceError, match="credential format"):
        call(paths, "backup", "--window", window)
    assert ctl.window.active
    assert not list((paths["var"] / "backups").glob("*/snapshot.json"))


def test_missing_database_not_created_or_migrated(fixture):
    paths, ctl, _ = fixture
    window = call(paths, "open")["window_id"]
    (paths["var"] / "localplane.db").unlink()
    with pytest.raises(FileNotFoundError):
        call(paths, "backup", "--window", window)
    assert not (paths["var"] / "localplane.db").exists()
    assert ctl.window.active


def test_foreign_key_violation_refuses_backup(fixture):
    paths, ctl, _ = fixture
    with sqlite3.connect(paths["var"] / "localplane.db") as db:
        db.executescript("CREATE TABLE child(parent INTEGER REFERENCES records(id)); INSERT INTO child VALUES(999);")
    window = call(paths, "open")["window_id"]
    with pytest.raises(cli.MaintenanceError, match="foreign-key"):
        call(paths, "backup", "--window", window)


@pytest.mark.parametrize("actual_identity", [False, True])
def test_bundle_exact_inventory_platform_and_dependency_metadata(tmp_path, monkeypatch, actual_identity):
    from localplane import build_identity
    root = tmp_path / "bundle"
    root.mkdir(mode=0o755)
    names = ["src/localplane/__init__.py", "console/index.html", "bin/localplane-backend",
             "bin/localplane-agent", "bin/localplane-maintenance", "packaging/localplane-agent.service",
             "packaging/localplane-backend.service", "docs/installation.md", "README.md",
             "src/localplane/backend/db/migrations/0001_init.sql"]
    for name in names:
        path = root / name
        cli.mkdir_parents(path.parent, 0o755)
        cli.atomic_bytes(path, b"synthetic bundle content", mode=0o644)
    pins = []
    for name in ("fastapi", "uvicorn", "jeepney"):
        metadata = root / "vendor" / (name + "-1.0.dist-info") / "METADATA"
        cli.mkdir_parents(metadata.parent, 0o755)
        cli.atomic_bytes(metadata, f"Name: {name}\nVersion: 1.0\n".encode(), mode=0o644)
        pins.append({"name": name, "version": "1.0", "sha256": "d" * 64})
    manifest = {"files": cli.inventory(root), "dependencies": pins,
                "platform": {"os": cli.platform_os(), "architecture": cli.platform.machine(),
                             "python": cli.platform.python_version()}}
    if actual_identity:
        console_digest = build_identity.console_files_digest((root / "console/index.html").read_bytes(), {})
        migrations = {"0001_init.sql": cli.sha(root / "src/localplane/backend/db/migrations/0001_init.sql")}
        source_digest = build_identity.canonical_digest({"files": manifest["files"], "migrations": migrations,
                                                        "console_digest": console_digest})
        manifest.update(format_version=1, version="0.1.0", source_commit="a" * 40,
                        source_tree=None, source_dirty=True, migrations=migrations,
                        console_digest=console_digest, source_digest=source_digest)
        manifest["build_id"] = build_identity.canonical_digest(manifest)
    cli.write_json(root / "release.json", manifest)
    # Identity verification is another explicit seam; this test covers the CLI's
    # independent full-inventory/platform/dependency refusal, not canonical identity.
    def inspect(*a, **kwargs):
        assert kwargs == {"process": "inspection", "inspect_only": True}
        return SimpleNamespace(status="inspected")
    identity = SimpleNamespace(INSPECTED="inspected", load_build_identity=inspect)
    if not actual_identity:
        monkeypatch.setattr(cli, "shared_modules", lambda: (None, identity))
    assert VERIFY_RELEASE(root) == manifest
    if actual_identity:
        inspected = build_identity.load_build_identity(root / "release.json", process="inspection", inspect_only=True)
        assert inspected.status == build_identity.INSPECTED
        assert not inspected.packaged  # Offline verification NEVER claims running identity.
    cli.atomic_bytes(root / "unmanifested.py", b"unexpected")
    with pytest.raises(cli.MaintenanceError, match="exactly its manifested"):
        VERIFY_RELEASE(root)
    (root / "unmanifested.py").unlink()
    manifest["platform"]["architecture"] = "wrong-architecture"
    if actual_identity:
        manifest["build_id"] = build_identity.canonical_digest({k: v for k, v in manifest.items() if k != "build_id"})
    cli.write_json(root / "release.json", manifest)
    with pytest.raises(cli.MaintenanceError, match="tested architecture"):
        VERIFY_RELEASE(root)
    manifest["platform"]["architecture"] = cli.platform.machine()
    manifest["dependencies"][0]["version"] = "99.0"
    if actual_identity:
        manifest["build_id"] = build_identity.canonical_digest({k: v for k, v in manifest.items() if k != "build_id"})
    cli.write_json(root / "release.json", manifest)
    with pytest.raises(cli.MaintenanceError, match="pinned, hashed"):
        VERIFY_RELEASE(root)


def test_errors_do_not_dump_sensitive_os_or_sql_values(fixture, monkeypatch, capsys):
    monkeypatch.setattr(cli, "execute", lambda *a: (_ for _ in ()).throw(OSError("PRIVATE TEST SECRET")))
    assert cli.main(["status"]) == 1
    output = capsys.readouterr()
    assert "PRIVATE TEST SECRET" not in output.err
    assert "maintenance_failed" in output.err


def test_shared_controller_closes_and_refuses_old_restore(tmp_path):
    from localplane.maintenance import MaintenanceController, MaintenanceRefused
    root = tmp_path / "maintenance"
    ctl = MaintenanceController(root, service="fixture", lock_timeout_s=0.01)
    ctl.initialize()
    window = ctl.open_window(reason="synthetic")
    with ctl.snapshot_lease(window.window_id):
        pass
    ctl.close_window(window.window_id, accepted=True)
    with pytest.raises(MaintenanceRefused):
        with ctl.restore_lease({"window_id": window.window_id, "generation": window.generation}):
            pytest.fail("Closed snapshot admitted")


@pytest.fixture
def real_fixture(fixture, monkeypatch):
    paths, _, releases = fixture
    monkeypatch.setattr(cli, "controller", CONTROLLER)
    return paths, CONTROLLER(paths), releases


def test_real_controller_backup_restore_accept_and_reopened_refusal(real_fixture):
    from localplane.maintenance import MaintenanceRefused
    paths, ctl, _ = real_fixture
    before = cli.inventory(paths["etc"])
    secret = (paths["var"] / "localplane-master.secret").read_bytes()
    window, name = backup(paths, ctl)
    assert ctl.state().window.snapshot_id == name
    witness = paths["var"] / "maintenance/window.json"
    active_witness = witness.read_bytes()
    with sqlite3.connect(paths["var"] / "localplane.db") as db:
        db.execute("UPDATE records SET outcome='failed-update', held=0")
    cli.atomic_bytes(paths["etc"] / "tls.key", b"CHANGED SYNTHETIC KEY")
    result = call(paths, "restore", "--window", window, "--snapshot", name)
    assert witness.read_bytes() == active_witness  # Witness was NEVER restored.
    assert cli.inventory(paths["etc"]) == before
    assert (paths["var"] / "localplane-master.secret").read_bytes() == secret
    with sqlite3.connect(paths["var"] / "localplane.db") as db:
        assert db.execute("SELECT outcome,held FROM records").fetchall() == [("unknown", 1)]
    assert (Path(result["displaced"]) / "original/raw-localplane.db").is_file()
    # Runtime restart is modeled by a new controller instance on the same files.
    restarted = CONTROLLER(paths)
    assert restarted.state().active
    with pytest.raises(MaintenanceRefused):
        with restarted.admit("synthetic control write"):
            pytest.fail("Active fence admitted a write")
    call(paths, "accept", "--window", window, "--verified")
    assert restarted.state().status == "closed"
    with restarted.admit("synthetic control write"):
        pass
    for reopen in (False, True):
        if reopen:
            call(paths, "open")
        with pytest.raises(MaintenanceRefused):
            call(paths, "restore", "--window", window, "--snapshot", name)


def test_real_runtime_lifetime_blocks_backup_and_recording_is_not_nested(real_fixture):
    from localplane.maintenance import MaintenanceController, MaintenanceRefused
    paths, _, _ = real_fixture
    window = call(paths, "open")["window_id"]
    short = MaintenanceController(paths["var"] / "maintenance", service="fixture", lock_timeout_s=0.01)
    with short.service_lifetime():
        with pytest.raises(MaintenanceRefused):
            with short.snapshot_lease(window):
                pytest.fail("Lifetime owner was not excluded")
    # The actual backup includes record_snapshot AFTER releasing snapshot_lease.
    name = call(paths, "backup", "--window", window)["snapshot"]
    assert short.state().window.snapshot_id == name


def test_real_missing_witness_is_unknown_and_cannot_be_reinitialized(real_fixture):
    paths, ctl, _ = real_fixture
    (paths["var"] / "maintenance/window.json").unlink()  # disposable fault fixture only
    assert ctl.state().status == "unknown"
    with pytest.raises(cli.MaintenanceError, match="unknown or pending"):
        call(paths, "open")
    with pytest.raises(cli.MaintenanceError, match="before application"):
        call(paths, "initialize")


@pytest.mark.parametrize("existing", ["localplane.db", "localplane-master.secret", "maintenance/control.lock"])
def test_initialize_refuses_any_prior_store_credential_or_lock(tmp_path, monkeypatch, existing):
    paths = {key: tmp_path / key for key in ("root", "etc", "var", "run")}
    for path in paths.values():
        path.mkdir(mode=0o700)
    (paths["var"] / "maintenance").mkdir(mode=0o700)
    monkeypatch.setenv("LOCALPLANE_MAINTENANCE_DIR", str(paths["var"] / "maintenance"))
    monkeypatch.setenv("LOCALPLANE_SERVICE_UID", str(os.geteuid()))
    monkeypatch.setenv("LOCALPLANE_SERVICE_GID", str(os.getegid()))
    path = paths["var"] / existing
    cli.atomic_bytes(path, b"preexisting synthetic state")
    with pytest.raises(cli.MaintenanceError):
        call(paths, "initialize")
    assert not (paths["var"] / "maintenance/window.json").exists()


def test_hex_slug_retains_prefixed_manifest_id(fixture):
    paths, _, _ = fixture
    assert cli.release_path(paths, BUILD).name == "a" * 64
    assert ":" not in str(cli.release_path(paths, BUILD))
    assert cli.current_release(paths)[1]["build_id"] == BUILD


@pytest.mark.parametrize("phase", ["prepared", "database_replaced", "credential_replaced",
                                    "config_displaced", "config_replaced", "validated"])
@pytest.mark.parametrize("fixture_name", ["fixture", "real_fixture"])
def test_interrupted_restore_exact_manual_resume_never_overwrites_originals(request, monkeypatch, phase, fixture_name):
    paths, ctl, _ = request.getfixturevalue(fixture_name)
    window, name = backup(paths, ctl)
    with sqlite3.connect(paths["var"] / "localplane.db") as db:
        db.execute("UPDATE records SET outcome='failed-current'")
    original = cli.write_restore_journal
    def fault(path, journal, at):
        original(path, journal, at)
        if at == phase:
            raise OSError("synthetic interrupted restore")
    monkeypatch.setattr(cli, "write_restore_journal", fault)
    with pytest.raises(OSError, match="interrupted restore"):
        call(paths, "restore", "--window", window, "--snapshot", name)
    if fixture_name == "real_fixture":
        pending = cli.read_json(paths["var"] / "maintenance/restore.json")
        transaction_id = pending["transaction_id"]
        assert ctl.state().refusal["code"] == "maintenance_restore_incomplete"
        with pytest.raises(MaintenanceRefused) as startup:
            with ctl.startup_lease():
                pytest.fail("Pending restore allowed startup")
        assert getattr(startup.value, "code", None) == "maintenance_restore_incomplete"
    else:
        assert ctl.pending is not None
        transaction_id = "synthetic-restore-stable-1"
    for command in (["accept", "--window", window, "--verified"], ["backup", "--window", window],
                    ["revert", OTHER, "--window", window, "--snapshot", name],
                    ["update", "/synthetic/unread-bundle", "--window", window, "--snapshot", name]):
        with pytest.raises(cli.MaintenanceError, match="pending"):
            call(paths, *command)
    retained = next((paths["var"] / "displaced").glob("restore_*/original"))
    evidence = cli.inventory(retained)
    if phase == "config_displaced":
        assert not paths["etc"].exists()
        (paths["var"] / "localplane.db").unlink()  # matching journal must not require current DB
    monkeypatch.setattr(cli, "write_restore_journal", original)
    result = call(paths, "restore", "--window", window, "--snapshot", name)
    assert cli.inventory(retained) == evidence
    if fixture_name == "real_fixture":
        assert not (paths["var"] / "maintenance/restore.json").exists()
        assert ctl.state().refusal is None
    else:
        assert ctl.pending is None
    assert result["transaction_id"] == transaction_id
    with sqlite3.connect(paths["var"] / "localplane.db") as db:
        assert db.execute("SELECT outcome,held FROM records").fetchall() == [("unknown", 1)]
    with sqlite3.connect(retained / "raw-localplane.db") as db:
        assert db.execute("SELECT outcome FROM records").fetchone() == ("failed-current",)


@pytest.mark.parametrize("fixture_name", ["fixture", "real_fixture"])
def test_restore_exdev_preflight_leaves_active_set_unchanged(request, monkeypatch, fixture_name):
    import errno
    paths, ctl, _ = request.getfixturevalue(fixture_name)
    window, name = backup(paths, ctl)
    before = cli.sha(paths["var"] / "localplane.db"), cli.inventory(paths["etc"])
    def exdev(*_):
        raise OSError(errno.EXDEV, "synthetic separate mount")
    monkeypatch.setattr(cli, "same_filesystem", exdev)
    with pytest.raises(OSError) as error:
        call(paths, "restore", "--window", window, "--snapshot", name)
    assert error.value.errno == errno.EXDEV
    assert before == (cli.sha(paths["var"] / "localplane.db"), cli.inventory(paths["etc"]))
    if fixture_name == "real_fixture":
        assert ctl.state().refusal["code"] == "maintenance_restore_incomplete"
    else:
        assert ctl.pending is not None


def test_restore_requires_pending_guard_before_any_active_write(fixture, monkeypatch):
    paths, ctl, _ = fixture
    window, name = backup(paths, ctl)
    before = cli.sha(paths["var"] / "localplane.db"), cli.inventory(paths["etc"])
    monkeypatch.setattr(ctl, "restore_transaction", None)
    with pytest.raises(cli.MaintenanceError, match="interrupted-restore guard"):
        call(paths, "restore", "--window", window, "--snapshot", name)
    assert before == (cli.sha(paths["var"] / "localplane.db"), cli.inventory(paths["etc"]))


def test_pending_restore_refuses_other_snapshot_and_unknown_binding(fixture, monkeypatch):
    paths, ctl, _ = fixture
    window, name = backup(paths, ctl)
    second = call(paths, "backup", "--window", window)["snapshot"]
    original = cli.write_restore_journal
    def interrupt(path, journal, phase):
        original(path, journal, phase)
        if phase == "prepared":
            raise OSError("synthetic interruption")
    monkeypatch.setattr(cli, "write_restore_journal", interrupt)
    with pytest.raises(OSError):
        call(paths, "restore", "--window", window, "--snapshot", name)
    with pytest.raises(cli.MaintenanceError, match="binding differs"):
        call(paths, "restore", "--window", window, "--snapshot", second)
    ctl.pending = ("unknown/corrupt",)
    with pytest.raises(cli.MaintenanceError, match="binding differs"):
        call(paths, "restore", "--window", window, "--snapshot", name)


def test_restore_prepares_complete_config_before_db_replacement(fixture, monkeypatch):
    paths, ctl, _ = fixture
    window, name = backup(paths, ctl)
    before = cli.sha(paths["var"] / "localplane.db")
    original = cli.apply_config_metadata
    def fail(root, metadata):
        original(root, metadata)
        raise OSError("synthetic config metadata failure")
    monkeypatch.setattr(cli, "apply_config_metadata", fail)
    with pytest.raises(OSError, match="metadata failure"):
        call(paths, "restore", "--window", window, "--snapshot", name)
    assert cli.sha(paths["var"] / "localplane.db") == before
    assert ctl.pending is not None


def test_new_install_stages_code_without_poisoning_genesis_with_a_lock(tmp_path, monkeypatch):
    paths = {key: tmp_path / key for key in ("root", "etc", "var", "run")}
    for path in paths.values():
        path.mkdir(mode=0o700)
    maintenance = paths["var"] / "maintenance"
    maintenance.mkdir(mode=0o700)
    monkeypatch.setenv("LOCALPLANE_MAINTENANCE_DIR", str(maintenance))
    monkeypatch.setenv("LOCALPLANE_SERVICE_UID", str(os.geteuid()))
    monkeypatch.setenv("LOCALPLANE_SERVICE_GID", str(os.getegid()))
    # Explicit root-owned installation seam: this test executes no root actions.
    monkeypatch.setattr(cli, "stage", lambda *_: cli.release_path(paths, BUILD))
    result = call(paths, "install", str(tmp_path / "bundle"))
    assert result["maintenance"].startswith("not initialized")
    assert not any(maintenance.iterdir())
    call(paths, "initialize")
    assert CONTROLLER(paths).state().status == "closed"


def test_backup_retains_empty_config_directories_and_metadata(fixture):
    paths, ctl, _ = fixture
    (paths["etc"] / "empty").mkdir(mode=0o700)
    window, name = backup(paths, ctl)
    backup_root, snapshot = cli.verify_snapshot(paths, name)
    assert (backup_root / "config/empty").is_dir()
    assert snapshot["database_metadata"] == cli.file_metadata(paths["var"] / "localplane.db")
    assert snapshot["secret_metadata"] == cli.file_metadata(paths["var"] / "localplane-master.secret")
    call(paths, "restore", "--window", window, "--snapshot", name)
    assert (paths["etc"] / "empty").is_dir()


@pytest.mark.parametrize("command", ["check", "status"])
def test_pending_restore_missing_config_and_db_inspection_is_read_only(real_fixture, monkeypatch, command):
    paths, ctl, _ = real_fixture
    window, name = backup(paths, ctl)
    original = cli.write_restore_journal
    def interrupt(path, journal, phase):
        original(path, journal, phase)
        if phase == "config_displaced":
            raise OSError("synthetic interruption with missing config")
    monkeypatch.setattr(cli, "write_restore_journal", interrupt)
    with pytest.raises(OSError):
        call(paths, "restore", "--window", window, "--snapshot", name)
    assert not paths["etc"].exists()
    (paths["var"] / "localplane.db").unlink()
    before = cli.inventory(paths["var"])
    with monkeypatch.context() as readonly:
        readonly.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("Inspection opened SQLite"))
        read_bytes = Path.read_bytes
        def screened(path):
            if path.name in {"localplane.db", "localplane-master.secret", "tls.key"}:
                pytest.fail("Inspection read database, secret or TLS key")
            return read_bytes(path)
        readonly.setattr(Path, "read_bytes", screened)
        os_open = os.open
        def read_only_open(path, flags, *a, **k):
            if flags & (os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_RDWR):
                pytest.fail("Inspection created a lock or opened a writable file")
            return os_open(path, flags, *a, **k)
        readonly.setattr(os, "open", read_only_open)
        result = call(paths, command)
    assert result["configuration"]["status"] == "unavailable"
    assert result["identity"]["status"] == "unknown"
    assert result["release_build_id"] is None
    assert result["maintenance"]["refusal"]["code"] == "maintenance_restore_incomplete"
    assert result["maintenance"]["window"]["window_id"] == window
    assert cli.inventory(paths["var"]) == before


def test_real_pending_guard_refuses_wrong_snapshot_and_corrupt_marker(real_fixture):
    paths, ctl, _ = real_fixture
    window, name = backup(paths, ctl)
    second = call(paths, "backup", "--window", window)["snapshot"]
    _, binding = cli.verify_snapshot(paths, name)
    with pytest.raises(OSError):
        with ctl.restore_transaction(binding, snapshot_id=name):
            raise OSError("synthetic crash before files")
    marker = paths["var"] / "maintenance/restore.json"
    original = marker.read_bytes()
    with pytest.raises(MaintenanceRefused, match="different restore"):
        call(paths, "restore", "--window", window, "--snapshot", second)
    assert marker.read_bytes() == original
    with pytest.raises(MaintenanceRefused):
        ctl.close_window(window, accepted=True)
    cli.atomic_bytes(marker, b"{malformed synthetic restore marker")
    assert ctl.state().status == "unknown"
    for method in (lambda: ctl.startup_lease(), lambda: ctl.snapshot_lease(window), lambda: ctl.restore_lease(binding)):
        with pytest.raises(MaintenanceRefused):
            with method():
                pytest.fail("Unknown pending marker was allowed")
    with pytest.raises(cli.MaintenanceError, match="unknown or pending"):
        call(paths, "accept", "--window", window, "--verified")
    with pytest.raises(MaintenanceRefused):
        call(paths, "restore", "--window", window, "--snapshot", name)


def test_snapshot_format_2_and_explicit_old_format_refusal_before_ownership_access(fixture, monkeypatch):
    paths, ctl, _ = fixture
    _, name = backup(paths, ctl)
    root, snapshot = cli.verify_snapshot(paths, name)
    assert snapshot["format_version"] == 2
    snapshot["format_version"] = 1
    snapshot.pop("database_metadata")
    snapshot.pop("secret_metadata")
    cli.write_json(root / "snapshot.json", snapshot)
    # Refusal must happen before release/schema/ownership validation, not KeyError.
    monkeypatch.setattr(cli, "verify_release", lambda *a, **k: pytest.fail("Old snapshot reached release validation"))
    with pytest.raises(cli.MaintenanceError) as refused:
        cli.verify_snapshot(paths, name)
    assert refused.value.code == "snapshot_format_unsupported"
    assert "older snapshots are refused" in str(refused.value)


@pytest.mark.parametrize("mask", [0o007, 0o077])
def test_new_public_code_ancestors_get_exact_mode_under_restrictive_umask(tmp_path, mask):
    import stat
    path = tmp_path / "public-code" / "releases"
    previous = os.umask(mask)
    try:
        cli.mkdir_parents(path, 0o755)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(path.stat().st_mode) == 0o755


@pytest.mark.parametrize("mask", [0o000, 0o007, 0o077])
def test_new_private_directories_remain_exactly_private(tmp_path, mask):
    import stat
    path = tmp_path / "private-state" / "snapshot"
    previous = os.umask(mask)
    try:
        cli.mkdir_parents(path, 0o700)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_existing_directory_is_never_chmodded_or_implicitly_repaired(tmp_path, monkeypatch):
    import stat
    existing = tmp_path / "existing-releases"
    existing.mkdir(mode=0o750)
    existing.chmod(0o750)  # synthetic preexisting state, not a production repair
    inode = existing.stat().st_ino
    fchmod = os.fchmod
    def screened(fd, mode):
        assert os.fstat(fd).st_ino != inode, "Existing directory must never be chmodded"
        return fchmod(fd, mode)
    monkeypatch.setattr(os, "fchmod", screened)
    cli.mkdir_parents(existing, 0o755)
    cli.mkdir_parents(existing / "new-child", 0o755)
    assert existing.stat().st_ino == inode
    assert stat.S_IMODE(existing.stat().st_mode) == 0o750
    assert stat.S_IMODE((existing / "new-child").stat().st_mode) == 0o755


@pytest.mark.parametrize("dangling", [False, True])
def test_mkdir_parents_refuses_symlinks_without_permission_repair(tmp_path, monkeypatch, dangling):
    target = tmp_path / "target"
    if not dangling:
        target.mkdir(mode=0o700)
    link = tmp_path / "symlink"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(os, "fchmod", lambda *_: pytest.fail("Symlink refusal must not change permissions"))
    with pytest.raises(cli.MaintenanceError, match="Symlinks"):
        cli.mkdir_parents(link / "new-child", 0o755)
    assert link.is_symlink()
    assert not (target / "new-child").exists()
