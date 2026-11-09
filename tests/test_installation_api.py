from fastapi.routing import APIRoute
import pytest

from localplane.backend.app import classify_request
from localplane.backend.config import Settings
from localplane.backend.db.database import open_database
from localplane.maintenance import MaintenanceController
from tests.conftest import AuthenticatedTestClient, create_authenticated_app


def test_fenced_startup_and_route_inventory(tmp_path, monkeypatch):
    root = tmp_path / "maintenance"
    ctl = MaintenanceController(root, service="test")
    ctl.initialize()
    window = ctl.open_window()
    settings = Settings(database_path=tmp_path / "store.db", agent_socket=tmp_path / "absent.sock",
                        agent_timeout_s=0.05, freshness_ttl_s=60, log_level="WARNING",
                        observe_on_startup=True, maintenance_dir=root)
    database = open_database(settings.database_path)
    calls = []
    monkeypatch.setattr("localplane.backend.app._settle_interrupted_changes", lambda context: calls.append("settle"))
    monkeypatch.setattr("localplane.backend.app._initial_observation", lambda *args: calls.append("observe"))
    app = create_authenticated_app(settings, database)
    with AuthenticatedTestClient(app) as client:
        assert not calls
        for route in app.routes:
            if not isinstance(route, APIRoute) or not route.path.startswith("/api/v1/"):
                continue
            if route.path.startswith("/api/v1/session"):
                continue
            path = route.path
            import re
            path = re.sub(r"\{[^}]+\}", "example", path)
            for method in route.methods:
                if method not in {"GET", "HEAD"}:
                    assert classify_request(method, path) != "read" or path in {
                        "/api/v1/storage/filesystems/observe",
                        "/api/v1/systemd/units/example/journal",
                        "/api/v1/docker/containers/example/logs",
                        "/api/v1/docker/containers/example/stats",
                    }, (method, path)
        for path in ("/api/v1/runs/example/apply", "/api/v1/runs/example/%61pply",
                     "/api/v1/runs/example/apply/"):
            response = client.post(path)
            assert response.status_code == 503, (path, response.status_code)
        for path in ("/api/v1/agent", "/api/v1/agent/", "/api/v1/%61gent/capabilities"):
            response = client.get(path)
            assert response.status_code == 503, (path, response.status_code)
        diagnostic = client.get("/api/v1/installation")
        assert diagnostic.status_code == 200, diagnostic.text
        body = diagnostic.json()
        assert body["maintenance"]["status"] == "active"
        assert "maintenance_active" in body["gaps"]
        assert body["backend"]["status"] == "unbundled"
        assert body["agent"]["source"] == "unavailable"
        assert body["schema_identity"]["applied"]
        assert "secret" not in diagnostic.text.lower()
        from localplane.build_identity import load_build_identity
        identity = load_build_identity(None, process="agent").as_dict()
        app.state.context.client.hello = lambda: {
            "agent": {"agent_instance_id": "synthetic-agent", "agent_version": "test",
                      "pid": 123, "build": identity, "maintenance": ctl.state().as_dict()},
            "capabilities": [],
        }
        live = client.get("/api/v1/installation")
        assert live.status_code == 200, live.text
        assert live.json()["agent"]["source"] == "live"
        assert live.json()["agent"]["build"]["process"] == "agent"
        assert live.json()["providers"]["source"] == "agent_cached"
        assert live.json()["providers"]["as_of"] is None
        ctl.close_window(window.window_id, accepted=True)
        closed = client.get("/api/v1/installation").json()
        assert closed["maintenance"]["status"] == "closed"
        assert "maintenance_closed" not in closed["gaps"]
        (root / "window.json").write_text("invalid")
        unknown = client.get("/api/v1/installation").json()
        assert unknown["maintenance"]["status"] == "unknown"
        assert "maintenance_unknown" in unknown["gaps"]
    database.close()


@pytest.mark.parametrize("interrupt", ["cancel", "disconnect"])
def test_asgi_cancellation_does_not_release_sync_endpoint_lease(tmp_path, monkeypatch, interrupt):
    import asyncio
    import threading
    from fastapi import APIRouter, Request
    from localplane.backend.api.routes import MaintenanceRoute
    from localplane.maintenance import MaintenanceRefused
    from tests.conftest import TEST_MASTER_SECRET
    import pytest

    root = tmp_path / "maintenance"
    ctl = MaintenanceController(root, service="test", lock_timeout_s=0)
    ctl.initialize()
    settings = Settings(database_path=tmp_path / "store.db", agent_socket=tmp_path / "absent.sock",
                        agent_timeout_s=0.05, freshness_ttl_s=60, log_level="WARNING",
                        observe_on_startup=False, maintenance_dir=root)
    database = open_database(settings.database_path)
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    router = APIRouter(prefix="/api/v1", route_class=MaintenanceRoute)

    @router.post("/runs/{run_id}/apply")
    def slow(run_id: str, request: Request):
        entered.set()
        try:
            release.wait(5)
            return {"ok": True}
        finally:
            exited.set()

    monkeypatch.setattr("localplane.backend.app.router", router)
    app = create_authenticated_app(settings, database)

    async def exercise():
        async with app.router.lifespan_context(app):
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": "POST", "scheme": "http", "path": "/api/v1/runs/x/apply",
                     "raw_path": b"/api/v1/runs/x/apply", "query_string": b"",
                     "headers": [(b"authorization", f"Bearer {TEST_MASTER_SECRET}".encode())],
                     "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 80),
                     "root_path": ""}
            first = True
            disconnected = asyncio.Event()
            async def receive():
                nonlocal first
                if first:
                    first = False
                    return {"type": "http.request", "body": b"", "more_body": False}
                await disconnected.wait()
                return {"type": "http.disconnect"}
            async def send(message):
                pass
            task = asyncio.create_task(app(scope, receive, send))
            assert await asyncio.to_thread(entered.wait, 2)
            if interrupt == "cancel":
                task.cancel()
            else:
                disconnected.set()
            await asyncio.sleep(0.05)
            assert not exited.is_set()
            with pytest.raises(MaintenanceRefused):
                ctl.open_window()
            release.set()
            assert await asyncio.to_thread(exited.wait, 2)
            try:
                await task
            except asyncio.CancelledError:
                pass
            # Thread may have signalled just before its context manager released.
            for _ in range(100):
                try:
                    ctl.open_window()
                    break
                except MaintenanceRefused:
                    await asyncio.sleep(0.01)
            else:
                pytest.fail("endpoint admission remained held after worker exit")
    try:
        asyncio.run(exercise())
    finally:
        release.set()
        database.close()


def test_startup_migration_requires_known_witness_and_preserves_authority(tmp_path, monkeypatch):
    import hashlib
    import pytest
    from localplane.backend.app import create_app
    from localplane.backend.auth import Authentication
    from localplane.maintenance import MaintenanceRefused
    from tests.conftest import TEST_MASTER_SECRET

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_base.sql").write_text(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, outcome TEXT);\n"
        "CREATE TABLE object_write_locks (id TEXT PRIMARY KEY, run_id TEXT);\n"
    )
    db_path = tmp_path / "store.db"
    db = open_database(db_path, migrations_dir=migrations)
    db.connection.execute("INSERT INTO runs VALUES ('run1', 'recovery_required')")
    db.connection.execute("INSERT INTO object_write_locks VALUES ('hold1', 'run1')")
    db.close()
    (migrations / "0002_update.sql").write_text("CREATE TABLE added_by_update (value TEXT);\n")
    monkeypatch.setattr("localplane.backend.db.database.MIGRATIONS_DIR", migrations)
    monkeypatch.setattr("localplane.backend.app._settle_interrupted_changes", lambda _: pytest.fail("settlement ran"))
    monkeypatch.setattr("localplane.backend.app._initial_observation", lambda *_: pytest.fail("observation ran"))
    root = tmp_path / "maintenance"
    ctl = MaintenanceController(root, service="test")
    ctl.initialize()
    settings = Settings(database_path=db_path, agent_socket=tmp_path / "absent.sock",
                        agent_timeout_s=0.05, freshness_ttl_s=60, log_level="WARNING",
                        observe_on_startup=True, maintenance_dir=root)
    auth = Authentication(TEST_MASTER_SECRET, bind_host=settings.bind_host)

    witness = root / "window.json"
    original = witness.read_bytes()
    for mode in ("missing", "corrupt"):
        if mode == "missing":
            witness.unlink()
        else:
            witness.write_text("not-json")
        before = hashlib.sha256(db_path.read_bytes()).digest()
        with pytest.raises(MaintenanceRefused):
            with AuthenticatedTestClient(create_app(settings, authentication=auth)):
                pytest.fail("unknown witness allowed startup")
        assert hashlib.sha256(db_path.read_bytes()).digest() == before
        witness.write_bytes(original)
    ctl.open_window()
    with AuthenticatedTestClient(create_app(settings, authentication=auth)) as client:
        rows = client.app.state.context.database.query("SELECT version FROM schema_migrations ORDER BY version")
        assert [r["version"] for r in rows] == [1, 2]
        assert tuple(client.app.state.context.database.query("SELECT * FROM runs")[0]) == ("run1", "recovery_required")
        assert tuple(client.app.state.context.database.query("SELECT * FROM object_write_locks")[0]) == ("hold1", "run1")


def test_console_diagnostic_is_bound_to_startup_bytes(tmp_path):
    from localplane.build_identity import console_files_digest
    build = tmp_path / "console"
    assets = build / "assets"
    assets.mkdir(parents=True, mode=0o755)
    build.chmod(0o755)
    assets.chmod(0o755)
    index = b'<html><head><script type="module" src="/assets/app-Abcd1234.js"></script></head><body><div id="root"></div></body></html>'
    script = b"console.log('first')"
    (build / "index.html").write_bytes(index)
    js = assets / "app-Abcd1234.js"
    js.write_bytes(script)
    (build / "index.html").chmod(0o644)
    js.chmod(0o644)
    settings = Settings(database_path=tmp_path / "store.db", agent_socket=tmp_path / "absent.sock",
                        agent_timeout_s=0.05, freshness_ttl_s=60, log_level="WARNING",
                        observe_on_startup=False, console_dir=build)
    db = open_database(settings.database_path)
    app = create_authenticated_app(settings, db)
    with AuthenticatedTestClient(app) as client:
        before = client.get("/api/v1/installation").json()["console"]
        assert "maintenance_disabled" not in client.get("/api/v1/installation").json()["gaps"]
        assert before["actual_digest"] == console_files_digest(index, {"/assets/app-Abcd1234.js": script})
        js.write_bytes(b"different on disk")
        after = client.get("/api/v1/installation").json()["console"]
        assert after["actual_digest"] == before["actual_digest"]
        assert client.get("/assets/app-Abcd1234.js").content == script
    db.close()


def test_active_startup_upgrades_real_schema_without_settling_run_or_lock(tmp_path, monkeypatch):
    import shutil
    from localplane.backend import app as app_module
    from localplane.backend.auth import Authentication
    from localplane.backend.db.database import MIGRATIONS_DIR
    from tests.conftest import TEST_MASTER_SECRET
    from tests.test_runs import _seed_a_published_plan

    staged = tmp_path / "migrations"
    staged.mkdir()
    for source in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(source.name[:4]) <= 15:
            shutil.copyfile(source, staged / source.name)
    db_path = tmp_path / "store.db"
    db = open_database(db_path, migrations_dir=staged)
    with db.transaction():
        _seed_a_published_plan(db)
        db.connection.execute("INSERT INTO object_write_locks VALUES ('lock1','h','o','mtu','run_legacy','t')")
    before_run = dict(db.query("SELECT * FROM runs")[0])
    before_lock = dict(db.query("SELECT * FROM object_write_locks")[0])
    db.close()
    shutil.copyfile(MIGRATIONS_DIR / "0016_authentication.sql", staged / "0016_authentication.sql")
    monkeypatch.setattr("localplane.backend.db.database.MIGRATIONS_DIR", staged)
    monkeypatch.setattr(app_module, "_settle_interrupted_changes", lambda _: pytest.fail("settlement ran"))
    monkeypatch.setattr(app_module, "_initial_observation", lambda *_: pytest.fail("observation ran"))
    root = tmp_path / "maintenance"
    ctl = MaintenanceController(root, service="test")
    ctl.initialize()
    ctl.open_window()
    settings = Settings(database_path=db_path, agent_socket=tmp_path / "absent.sock",
                        agent_timeout_s=0.05, freshness_ttl_s=60, log_level="WARNING",
                        observe_on_startup=True, maintenance_dir=root)
    auth = Authentication(TEST_MASTER_SECRET, bind_host=settings.bind_host)
    with AuthenticatedTestClient(app_module.create_app(settings, authentication=auth)) as client:
        db = client.app.state.context.database
        assert db.query("SELECT MAX(version) FROM schema_migrations")[0][0] == 16
        assert dict(db.query("SELECT * FROM runs")[0]) == before_run
        assert dict(db.query("SELECT * FROM object_write_locks")[0]) == before_lock
        assert not db.query("PRAGMA foreign_key_check")


def test_interrupted_restore_refuses_backend_before_store_open(tmp_path, monkeypatch):
    from localplane.backend.app import create_app
    from localplane.backend.auth import Authentication
    from localplane.maintenance import MaintenanceRefused
    from tests.conftest import TEST_MASTER_SECRET
    root = tmp_path / "maintenance"
    ctl = MaintenanceController(root, service="test")
    ctl.initialize()
    window = ctl.open_window()
    binding = {"window_id": window.window_id, "generation": window.generation}
    with ctl.restore_transaction(binding, snapshot_id="snap"):
        pass  # interrupted before completion
    settings = Settings(database_path=tmp_path / "never-opened.db", agent_socket=tmp_path / "absent.sock",
                        agent_timeout_s=0.05, freshness_ttl_s=60, log_level="WARNING",
                        observe_on_startup=False, maintenance_dir=root)
    monkeypatch.setattr("localplane.backend.app.open_database", lambda *_: pytest.fail("store opened"))
    auth = Authentication(TEST_MASTER_SECRET, bind_host=settings.bind_host)
    with pytest.raises(MaintenanceRefused) as exc:
        with AuthenticatedTestClient(create_app(settings, authentication=auth)):
            pytest.fail("startup succeeded")
    assert exc.value.code == "maintenance_restore_incomplete"
    assert not settings.database_path.exists()
