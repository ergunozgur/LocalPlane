"""Security-boundary proofs for the accepted single-credential design."""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from localplane.backend.app import create_app
from localplane.backend.auth import (
    SESSION_COOKIE,
    Authentication,
    AuthenticationConfigurationError,
    SessionStore,
    initialize_master_secret,
    load_master_secret,
    require_authentication,
)
from localplane.backend.auth_cli import main as auth_cli_main
from localplane.backend.config import Settings
from localplane.backend.db.database import open_database
from tests.conftest import TEST_MASTER_SECRET


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "database_path": tmp_path / "localplane.db",
        "agent_socket": tmp_path / "absent.sock",
        "agent_timeout_s": 0.01,
        "freshness_ttl_s": 60,
        "log_level": "WARNING",
        "observe_on_startup": False,
        "auth_secret_path": tmp_path / "master.secret",
        "bind_host": "127.0.0.1",
        "development_origin": None,
    }
    values.update(overrides)
    return Settings(**values)


def _client(
    tmp_path: Path,
    *,
    authentication: Authentication | None = None,
    settings: Settings | None = None,
    base_url: str = "http://127.0.0.1:8080",
):
    settings = settings or _settings(tmp_path)
    database = open_database(settings.database_path)
    authentication = authentication or Authentication(
        TEST_MASTER_SECRET,
        bind_host=settings.bind_host,
        development_origin=settings.development_origin,
    )
    client = TestClient(
        create_app(settings, database, authentication=authentication),
        base_url=base_url,
    )
    client.__enter__()
    return client, database


def _close(client: TestClient, database) -> None:
    client.__exit__(None, None, None)
    database.close()


def _bearer(secret: str = TEST_MASTER_SECRET) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def test_initializer_creates_one_256_bit_0600_secret_and_refuses_overwrite(
    tmp_path: Path,
):
    path = tmp_path / "master.secret"
    token = initialize_master_secret(path)
    assert len(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))) == 32
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert load_master_secret(path) == token
    with pytest.raises(FileExistsError):
        initialize_master_secret(path)
    assert load_master_secret(path) == token


def test_initializer_refuses_symlink_and_missing_parent(tmp_path: Path):
    target = tmp_path / "target"
    target.write_text("not a secret")
    link = tmp_path / "master.secret"
    link.symlink_to(target)
    with pytest.raises(AuthenticationConfigurationError, match="symbolic link"):
        initialize_master_secret(link)
    with pytest.raises(AuthenticationConfigurationError, match="component"):
        initialize_master_secret(tmp_path / "missing" / "master.secret")
    assert target.read_text() == "not a secret"


def test_initializer_refuses_an_unsafe_parent_directory(tmp_path: Path):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o700)
    parent.chmod(0o777)
    try:
        with pytest.raises(AuthenticationConfigurationError, match="parent has unsafe"):
            initialize_master_secret(parent / "master.secret")
        assert not (parent / "master.secret").exists()
    finally:
        parent.chmod(0o700)


def test_cli_prints_credential_once_without_logging_or_failure_disclosure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
):
    path = tmp_path / "master.secret"
    monkeypatch.setenv("LOCALPLANE_AUTH_SECRET_PATH", str(path))
    assert auth_cli_main(["init"]) == 0
    success = capsys.readouterr()
    token = success.out.strip()
    assert token == load_master_secret(path)
    assert success.err == ""
    assert token not in caplog.text

    assert auth_cli_main(["init"]) == 1
    refused = capsys.readouterr()
    assert refused.out == ""
    assert "already exists" in refused.err
    assert token not in refused.err
    assert token not in caplog.text


@pytest.mark.parametrize("mode", [0o604, 0o640, 0o660])
def test_secret_loader_refuses_group_or_other_access(tmp_path: Path, mode: int):
    path = tmp_path / "master.secret"
    path.write_text(TEST_MASTER_SECRET + "\n")
    path.chmod(mode)
    with pytest.raises(AuthenticationConfigurationError, match="permissions"):
        load_master_secret(path)


@pytest.mark.parametrize("content", ["short\n", "not base64 !\n", "  token\n", "\n"])
def test_secret_loader_refuses_malformed_content(tmp_path: Path, content: str):
    path = tmp_path / "master.secret"
    path.write_text(content)
    path.chmod(0o600)
    with pytest.raises(AuthenticationConfigurationError):
        load_master_secret(path)


def test_backend_startup_fails_closed_when_secret_is_missing(tmp_path: Path):
    settings = _settings(tmp_path)
    database = open_database(settings.database_path)
    try:
        with pytest.raises(AuthenticationConfigurationError):
            with TestClient(create_app(settings, database)):
                pass
    finally:
        database.close()


def test_master_verification_calls_compare_digest(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, str]] = []
    original = __import__("hmac").compare_digest

    def observed(left: str, right: str) -> bool:
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr("localplane.backend.auth.hmac.compare_digest", observed)
    authentication = Authentication(TEST_MASTER_SECRET)
    assert authentication.verify_master(TEST_MASTER_SECRET)
    assert calls == [(TEST_MASTER_SECRET, TEST_MASTER_SECRET)]


def test_missing_malformed_and_invalid_bearer_are_structured_refusals(tmp_path: Path):
    client, database = _client(tmp_path)
    try:
        missing = client.get("/api/v1/status")
        malformed = client.get("/api/v1/status", headers={"Authorization": "Basic x"})
        invalid = client.get("/api/v1/status", headers=_bearer("wrong"))
        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "authentication_required"
        assert malformed.status_code == 401
        assert malformed.json()["error"]["code"] == "bearer_malformed"
        assert invalid.status_code == 401
        assert invalid.json()["error"]["code"] == "bearer_invalid"
        assert invalid.headers["www-authenticate"] == "Bearer"
    finally:
        _close(client, database)


def test_every_user_facing_route_and_docs_refuse_unauthenticated_requests(tmp_path: Path):
    client, database = _client(tmp_path)
    try:
        checked: set[tuple[str, str]] = set()
        for route in client.app.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set())
            if not path.startswith("/api/v1"):
                continue
            concrete = path
            for parameter in ("object_id", "run_id", "change_id", "finding_id"):
                concrete = concrete.replace("{" + parameter + "}", "missing")
            for method in methods:
                if method in {"HEAD", "OPTIONS"}:
                    continue
                response = client.request(method, concrete)
                assert response.status_code == 401, (method, path, response.text)
                checked.add((method, path))
        assert len(checked) >= 47
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 401
    finally:
        _close(client, database)


def test_valid_bearer_reaches_representative_read_and_record_boundaries(tmp_path: Path):
    client, database = _client(tmp_path)
    try:
        assert client.get("/api/v1/status", headers=_bearer()).status_code == 200
        record = client.post(
            "/api/v1/runs",
            headers=_bearer(),
            json={"type": "docker.start", "object_id": "missing"},
        )
        assert record.status_code != 401
        host_write = client.post("/api/v1/runs/missing/apply", headers=_bearer())
        assert host_write.status_code == 404
    finally:
        _close(client, database)


def test_session_stores_only_hash_and_has_fixed_non_sliding_expiry():
    now = [datetime(2026, 9, 1, tzinfo=timezone.utc)]
    store = SessionStore(clock=lambda: now[0])
    token, expiry = store.create()
    assert expiry == now[0] + timedelta(hours=12)
    assert token not in store.stored_hashes()
    assert store.stored_hashes() == (hashlib.sha256(token.encode()).hexdigest(),)
    now[0] += timedelta(hours=6)
    assert store.lookup(token) == expiry
    now[0] += timedelta(hours=6)
    assert store.lookup(token) is None
    assert store.stored_hashes() == ()


def test_session_exchange_cookie_rotation_logout_and_restart_invalidation(tmp_path: Path):
    authentication = Authentication(TEST_MASTER_SECRET)
    client, database = _client(tmp_path, authentication=authentication)
    try:
        login = client.post("/api/v1/session", headers=_bearer())
        assert login.status_code == 200
        first = client.cookies.get(SESSION_COOKIE)
        assert first and TEST_MASTER_SECRET not in login.headers["set-cookie"]
        cookie = login.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        assert "path=/" in cookie
        assert "domain=" not in cookie
        assert "max-age=43200" in cookie
        status = client.get("/api/v1/session")
        assert status.status_code == 200
        assert status.json()["mechanism"] == "session"

        second = client.post("/api/v1/session", headers=_bearer())
        rotated = client.cookies.get(SESSION_COOKIE)
        assert second.status_code == 200 and rotated != first
        assert authentication.sessions.lookup(first) is None

        logout = client.delete(
            "/api/v1/session", headers={"Origin": "http://127.0.0.1:8080"}
        )
        assert logout.status_code == 204
        assert client.get("/api/v1/session").status_code == 401
    finally:
        _close(client, database)

    restarted = Authentication(TEST_MASTER_SECRET)
    assert restarted.sessions.lookup(rotated) is None


def test_explicit_invalid_bearer_never_falls_back_to_valid_cookie(tmp_path: Path):
    client, database = _client(tmp_path)
    try:
        assert client.post("/api/v1/session", headers=_bearer()).status_code == 200
        refused = client.get("/api/v1/status", headers=_bearer("wrong"))
        assert refused.status_code == 401
        assert refused.json()["error"]["code"] == "bearer_invalid"
    finally:
        _close(client, database)


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("origin", ["expected", "missing", "foreign"])
@pytest.mark.parametrize("mechanism", ["bearer", "session"])
def test_authentication_method_origin_matrix(
    method: str,
    origin: str,
    mechanism: str,
):
    app = FastAPI()
    authentication = Authentication(TEST_MASTER_SECRET)
    app.state.authentication = authentication

    @app.api_route("/covered", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def covered(
        _request: Request,
        _authenticated=Depends(require_authentication),
    ):
        return {"covered": True}

    client = TestClient(app, base_url="http://127.0.0.1:8080")
    headers: dict[str, str] = {}
    if mechanism == "bearer":
        headers.update(_bearer())
    else:
        token, _expiry = authentication.create_session()
        client.cookies.set(SESSION_COOKIE, token)
    if origin == "expected":
        headers["Origin"] = "http://127.0.0.1:8080"
    elif origin == "foreign":
        headers["Origin"] = "http://foreign.example"

    response = client.request(method, "/covered", headers=headers)
    if mechanism == "session" and method != "GET" and origin != "expected":
        assert response.status_code == 403
    else:
        assert response.status_code == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_invalid_explicit_bearer_never_falls_back_to_cookie_for_unsafe_methods(
    method: str,
):
    app = FastAPI()
    authentication = Authentication(TEST_MASTER_SECRET)
    app.state.authentication = authentication

    @app.api_route("/covered", methods=["POST", "PUT", "PATCH", "DELETE"])
    def covered(_authenticated=Depends(require_authentication)):
        return {"covered": True}

    token, _expiry = authentication.create_session()
    client = TestClient(app, base_url="http://127.0.0.1:8080")
    client.cookies.set(SESSION_COOKIE, token)
    response = client.request(
        method,
        "/covered",
        headers={
            "Authorization": "Bearer wrong",
            "Origin": "http://127.0.0.1:8080",
        },
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:99999",
        "http://127.0.0.1:not-a-port",
        "http://[::1",
        "http://::1",
        "http://[::1]]",
        "http://user@example.com",
    ],
)
def test_every_malformed_cookie_origin_is_a_controlled_403(origin: str):
    app = FastAPI()
    authentication = Authentication(TEST_MASTER_SECRET)
    app.state.authentication = authentication

    @app.post("/covered")
    def covered(_authenticated=Depends(require_authentication)):
        return {"covered": True}

    token, _expiry = authentication.create_session()
    client = TestClient(app, base_url="http://127.0.0.1:8080")
    client.cookies.set(SESSION_COOKIE, token)
    response = client.post("/covered", headers={"Origin": origin})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "origin_invalid"


def test_cookie_origin_policy_and_state_changing_get_exception(tmp_path: Path):
    client, database = _client(tmp_path)
    try:
        assert client.post("/api/v1/session", headers=_bearer()).status_code == 200
        assert client.get("/api/v1/status").status_code == 200
        missing = client.post("/api/v1/docker/containers/missing/stats")
        foreign = client.post(
            "/api/v1/docker/containers/missing/stats",
            headers={"Origin": "http://foreign.example"},
        )
        accepted = client.post(
            "/api/v1/docker/containers/missing/stats",
            headers={"Origin": "http://127.0.0.1:8080"},
        )
        assert missing.status_code == 403
        assert missing.json()["error"]["code"] == "origin_required"
        assert foreign.status_code == 403
        assert foreign.json()["error"]["code"] == "origin_not_allowed"
        assert accepted.status_code == 404
        bearer = client.post(
            "/api/v1/docker/containers/missing/stats", headers=_bearer()
        )
        assert bearer.status_code == 404
        # Accepted method-semantic exception: cookie-authenticated GET is not Origin-gated.
        assert client.get("/api/v1/agent/capabilities").status_code == 200
    finally:
        _close(client, database)


def test_exact_development_origin_is_the_only_additional_origin(tmp_path: Path):
    settings = _settings(tmp_path, development_origin="http://127.0.0.1:5178")
    client, database = _client(tmp_path, settings=settings)
    try:
        assert client.post("/api/v1/session", headers=_bearer()).status_code == 200
        accepted = client.post(
            "/api/v1/docker/containers/missing/stats",
            headers={"Origin": "http://127.0.0.1:5178"},
        )
        near_miss = client.post(
            "/api/v1/docker/containers/missing/stats",
            headers={"Origin": "http://127.0.0.1:5179"},
        )
        assert accepted.status_code == 404
        assert near_miss.status_code == 403
    finally:
        _close(client, database)


def test_remote_http_session_issuance_is_refused_and_https_cookie_is_secure(
    tmp_path: Path,
):
    remote = _settings(tmp_path, bind_host="0.0.0.0")
    client, database = _client(tmp_path, settings=remote, base_url="http://192.0.2.10")
    try:
        refused = client.post("/api/v1/session", headers=_bearer())
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "browser_session_requires_https"
    finally:
        _close(client, database)

    secure_settings = _settings(
        tmp_path, database_path=tmp_path / "secure.db", bind_host="192.0.2.10"
    )
    client, database = _client(
        tmp_path, settings=secure_settings, base_url="https://192.0.2.10"
    )
    try:
        login = client.post("/api/v1/session", headers=_bearer())
        assert login.status_code == 200
        assert "secure" in login.headers["set-cookie"].lower()
    finally:
        _close(client, database)


def test_session_lookup_and_logout_are_lock_safe_and_never_resurrect():
    store = SessionStore()
    token, _expiry = store.create()
    barrier = threading.Barrier(9)

    def lookup():
        barrier.wait()
        return store.lookup(token)

    def revoke():
        barrier.wait()
        return store.revoke(token)

    with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:
        futures = [pool.submit(lookup) for _ in range(8)] + [pool.submit(revoke)]
        [future.result() for future in futures]
    assert store.lookup(token) is None
    assert store.stored_hashes() == ()




def test_0015_store_upgrades_preserving_history_and_all_authority_triggers(
    tmp_path: Path,
):
    import shutil
    import sqlite3

    from localplane.backend.db.database import MIGRATIONS_DIR
    from tests.test_runs import _seed_a_published_plan

    staged = tmp_path / "migrations"
    staged.mkdir()
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(migration.name.split("_", 1)[0]) <= 15:
            shutil.copy(migration, staged / migration.name)

    old = open_database(tmp_path / "upgrade.db", staged)
    with old.transaction():
        _seed_a_published_plan(old)
        old.execute(
            "INSERT INTO run_confirmations (confirmation_id,run_id,purpose,preview_id,"
            "preview_digest,digest_version,required_method,method,policy,source,satisfied_at) "
            "VALUES ('cnf_history','run_legacy','apply','prv_legacy','sha256:legacy',1,"
            "'acknowledge','acknowledge','pol','unauthenticated_request','t')"
        )
    old.close()

    shutil.copy(MIGRATIONS_DIR / "0016_authentication.sql", staged)
    upgraded = open_database(tmp_path / "upgrade.db", staged)
    try:
        history = upgraded.query_one(
            "SELECT source, preview_digest FROM run_confirmations "
            "WHERE confirmation_id='cnf_history'"
        )
        assert tuple(history) == ("unauthenticated_request", "sha256:legacy")
        upgraded.execute(
            "INSERT INTO run_confirmations (confirmation_id,run_id,purpose,preview_id,"
            "preview_digest,digest_version,required_method,method,policy,source,satisfied_at) "
            "VALUES ('cnf_authenticated','run_legacy','recovery_retry','prv_legacy',"
            "'sha256:legacy',1,'acknowledge','acknowledge','pol',"
            "'authenticated_request','later')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            upgraded.execute(
                "INSERT INTO run_confirmations (confirmation_id,run_id,purpose,preview_id,"
                "preview_digest,digest_version,required_method,method,policy,source,satisfied_at) "
                "VALUES ('cnf_invalid','run_legacy','self_impact_override','prv_legacy',"
                "'sha256:legacy',1,'acknowledge','acknowledge','pol','invented_actor','later')"
            )
        upgraded.execute(
            "UPDATE run_confirmations SET consumed_at='spent',"
            "consumed_by_attempt_id='attempt' WHERE confirmation_id='cnf_authenticated'"
        )
        with pytest.raises(sqlite3.IntegrityError, match="single-use"):
            upgraded.execute(
                "UPDATE run_confirmations SET policy='changed' "
                "WHERE confirmation_id='cnf_authenticated'"
            )
        triggers = {
            row["name"]
            for row in upgraded.query(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert {
            "run_confirmations_match_the_runs_preview",
            "run_confirmations_are_consumed_once",
            "changes_require_a_consumed_confirmation",
            "changes_requiring_a_self_impact_override_have_one",
            "run_guards_require_the_typed_authority",
            "recovery_mutation_names_the_authority_spent_on_it_on_insert",
            "recovery_mutation_names_the_authority_spent_on_it_on_update",
        } <= triggers
        assert upgraded.query("PRAGMA foreign_key_check") == []
        assert upgraded.query_one("PRAGMA integrity_check")[0] == "ok"
    finally:
        upgraded.close()

def test_openapi_declares_session_surface_and_authentication_schemes(tmp_path: Path):
    settings = _settings(tmp_path)
    database = open_database(":memory:")
    try:
        app = create_app(
            settings,
            database,
            authentication=Authentication(TEST_MASTER_SECRET),
        )
        contract = app.openapi()
        assert {"MasterBearer", "BrowserSession"} <= set(
            contract["components"]["securitySchemes"]
        )
        assert {
            ("post", "/api/v1/session"),
            ("get", "/api/v1/session"),
            ("delete", "/api/v1/session"),
        } <= {
            (method, path)
            for path, operations in contract["paths"].items()
            for method in operations
            if method in {"get", "post", "put", "patch", "delete"}
        }
        assert contract["paths"]["/api/v1/session"]["post"]["security"] == [
            {"MasterBearer": []}
        ]
        assert contract["paths"]["/api/v1/session"]["delete"]["security"] == [
            {"BrowserSession": []}
        ]
        assert contract["paths"]["/api/v1/status"]["get"]["security"] == [
            {"MasterBearer": []},
            {"BrowserSession": []},
        ]
    finally:
        database.close()
