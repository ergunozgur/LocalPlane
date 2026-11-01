"""Startup and same-origin boundary tests. TLS inputs are non-secret mock data."""

from __future__ import annotations

import os
import ssl
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from localplane.backend import __main__ as startup
from localplane.backend import console
from localplane.backend.app import create_app
from localplane.backend.auth import Authentication, SESSION_COOKIE
from localplane.backend.config import Settings
from localplane.backend.db.database import open_database
from localplane.backend.serving_files import ServingConfigurationError
from localplane.backend.tls import server_config, validate_transport
from tests.conftest import TEST_AUTHORIZATION, TEST_MASTER_SECRET


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_path=tmp_path / "db", agent_socket=tmp_path / "absent.sock",
        agent_timeout_s=0.01, freshness_ttl_s=60, log_level="WARNING",
        observe_on_startup=False,
    )


@pytest.fixture
def tls_settings(tmp_path, settings):
    cert, key = tmp_path / "certificate.pem", tmp_path / "key.pem"
    cert.write_text("NONSECRET MOCK CERTIFICATE")
    cert.chmod(0o644)
    key.write_text("NONSECRET MOCK KEY")
    key.chmod(0o600)
    return replace(settings, tls_cert_path=cert, tls_key_path=key, bind_host="192.0.2.10")


@pytest.fixture
def build(tmp_path):
    path = tmp_path / "dist"
    path.mkdir(mode=0o755)
    (path / "assets").mkdir(mode=0o755)
    (path / "index.html").write_text(
        '<!doctype html><html><head><script type="module" src="/assets/index-Abcd1234.js"></script>'
        '<link rel="stylesheet" href="/assets/index-Abcd1234.css"></head>'
        '<body><div id="root"></div></body></html>'
    )
    (path / "assets/index-Abcd1234.js").write_text('console.log("fixture");')
    (path / "assets/index-Abcd1234.css").write_text("body{color:black}")
    (path / "assets/font-Abcd1234.woff2").write_bytes(b"mock font")
    (path / "assets/index-Abcd1234.js.map").write_text("NONPUBLIC SOURCE MAP")
    (path / ".env").write_text("NONPUBLIC MOCK INPUT")
    for file in (path / "index.html", *(path / "assets").iterdir()):
        file.chmod(0o644)
    return path


def test_settings_are_optional_and_environment_contract_is_exact():
    defaults = Settings.from_env({})
    assert defaults.bind_host == "127.0.0.1"
    assert defaults.tls_cert_path is defaults.tls_key_path is defaults.console_dir is None
    configured = Settings.from_env({
        "LOCALPLANE_TLS_CERT_PATH": "cert.pem", "LOCALPLANE_TLS_KEY_PATH": "key.pem",
        "LOCALPLANE_CONSOLE_DIR": "web/dist",
    })
    assert (configured.tls_cert_path, configured.tls_key_path, configured.console_dir) == (
        Path("cert.pem"), Path("key.pem"), Path("web/dist"),
    )


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.1", "example.invalid", "localhost", ""])
def test_remote_or_ambiguous_http_refused(settings, host):
    with pytest.raises(ServingConfigurationError, match="tls_required"):
        validate_transport(replace(settings, bind_host=host))


@pytest.mark.parametrize("host", ["127.0.0.1", "127.2.3.4", "::1"])
def test_numeric_loopback_http_is_unchanged(settings, host):
    config = server_config(replace(settings, bind_host=host), FastAPI(), 8080)
    assert config.host == host and not config.is_ssl
    assert config.workers == 1 and config.proxy_headers is False and config.access_log is False


@pytest.mark.parametrize("field", ["tls_cert_path", "tls_key_path"])
def test_incomplete_pair_refused_even_on_loopback(settings, field):
    with pytest.raises(ServingConfigurationError, match="tls_pair_required"):
        server_config(replace(settings, **{field: Path("absent")}), FastAPI(), 8080)


@pytest.mark.parametrize("kind", ["missing", "symlink", "parent_symlink", "directory", "fifo", "hardlink", "mode", "unreadable", "parent_writable"])
def test_bad_key_never_reaches_ssl(tls_settings, tmp_path, monkeypatch, kind):
    key = tls_settings.tls_key_path
    if kind == "missing":
        key.unlink()
    elif kind == "symlink":
        target = key.with_name("target")
        key.rename(target)
        key.symlink_to(target)
    elif kind == "parent_symlink":
        link = tmp_path / "link"
        link.symlink_to(tmp_path, target_is_directory=True)
        tls_settings = replace(tls_settings, tls_key_path=link / key.name)
    elif kind in {"directory", "fifo"}:
        key.unlink()
        key.mkdir() if kind == "directory" else os.mkfifo(key)
    elif kind == "hardlink":
        os.link(key, tmp_path / "second")
    elif kind == "mode":
        key.chmod(0o640)
    elif kind == "unreadable":
        key.chmod(0)
    else:
        parent = tmp_path / "unsafe"
        parent.mkdir()
        moved = parent / "key"
        key.rename(moved)
        parent.chmod(0o777)
        tls_settings = replace(tls_settings, tls_key_path=moved)
    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", lambda *a, **k: pytest.fail("SSL must not see invalid key"))
    with pytest.raises(ServingConfigurationError, match="tls_key_file_invalid") as caught:
        server_config(tls_settings, FastAPI(), 8443)
    assert str(tmp_path) not in str(caught.value)


def test_wrong_key_owner_refused(tls_settings, monkeypatch):
    import localplane.backend.serving_files as files
    original = files.os.fstat
    key_inode = tls_settings.tls_key_path.stat().st_ino

    def wrong_owner(fd):
        info = original(fd)
        if info.st_ino == key_inode:
            values = list(info)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(files.os, "fstat", wrong_owner)
    with pytest.raises(ServingConfigurationError, match="tls_key_file_invalid"):
        server_config(tls_settings, FastAPI(), 8443)


def test_invalid_pem_is_sanitized_without_reading_any_real_key(tls_settings):
    with pytest.raises(ServingConfigurationError, match="tls_certificate_load_failed") as caught:
        server_config(tls_settings, FastAPI(), 8443)
    assert "MOCK" not in str(caught.value)
    assert str(tls_settings.tls_key_path) not in str(caught.value)


def test_ssl_mismatch_error_is_sanitized(tls_settings, monkeypatch):
    def fail(*a, **k):
        raise ssl.SSLError("mock sensitive path and contents")
    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", fail)
    with pytest.raises(ServingConfigurationError) as caught:
        server_config(tls_settings, FastAPI(), 8443)
    assert "sensitive" not in str(caught.value)


def test_encrypted_key_callback_refuses_without_prompt(tls_settings, monkeypatch):
    def encrypted(context, certfile, keyfile, password):
        password()
    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", encrypted)
    with pytest.raises(ServingConfigurationError, match="tls_certificate_load_failed"):
        server_config(tls_settings, FastAPI(), 8443)


@pytest.mark.parametrize("kind", ["missing", "symlink", "directory", "writable", "unreadable"])
def test_invalid_certificate_file_is_refused(tls_settings, kind):
    cert = tls_settings.tls_cert_path
    if kind == "missing":
        cert.unlink()
    elif kind == "symlink":
        target = cert.with_name("other.pem")
        cert.rename(target)
        cert.symlink_to(target)
    elif kind == "directory":
        cert.unlink()
        cert.mkdir()
    else:
        cert.chmod(0o666 if kind == "writable" else 0)
    with pytest.raises(ServingConfigurationError, match="tls_certificate_file_invalid"):
        server_config(tls_settings, FastAPI(), 8443)


def test_os_unreadable_key_refused(tls_settings, monkeypatch):
    import localplane.backend.serving_files as files
    original = files.os.open

    def denied(path, *args, **kwargs):
        if path == tls_settings.tls_key_path.name:
            raise PermissionError("mock undisclosed OS error")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(files.os, "open", denied)
    with pytest.raises(ServingConfigurationError, match="tls_key_file_invalid"):
        server_config(tls_settings, FastAPI(), 8443)


def test_valid_tls_configuration_pins_files_and_loads_once(tls_settings, monkeypatch):
    calls = []
    inodes = {tls_settings.tls_cert_path.stat().st_ino, tls_settings.tls_key_path.stat().st_ino}

    def load(context, certfile, keyfile, password):
        # Metadata only: never read even the fixture key through Python.
        assert {os.stat(certfile).st_ino, os.stat(keyfile).st_ino} == inodes
        calls.append((certfile, keyfile))

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", load)
    monkeypatch.setenv("WEB_CONCURRENCY", "8")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    config = server_config(tls_settings, FastAPI(), 8443)
    assert config.loaded and config.is_ssl
    assert config.host == "192.0.2.10" and config.port == 8443
    assert config.workers == 1 and config.proxy_headers is False
    assert config.ssl.minimum_version == ssl.TLSVersion.TLSv1_2
    assert config.ssl.options & ssl.OP_NO_COMPRESSION
    assert config.ssl.protocol == ssl.PROTOCOL_TLS_SERVER
    assert len(calls) == 1
    assert all(not Path(path).exists() for path in calls[0])
    assert not any(cipher["name"] in {"AES128-SHA", "AES256-SHA"} for cipher in config.ssl.get_ciphers())


def test_pinned_key_survives_path_replacement_without_following_link(tls_settings, monkeypatch, tmp_path):
    original_inode = tls_settings.tls_key_path.stat().st_ino
    replacement = tmp_path / "replacement"
    replacement.write_text("NONSECRET OTHER INPUT")

    def replace_path(context, certfile, keyfile, password):
        tls_settings.tls_key_path.unlink()
        tls_settings.tls_key_path.symlink_to(replacement)
        assert os.stat(keyfile).st_ino == original_inode
        assert os.stat(keyfile).st_ino != replacement.stat().st_ino

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", replace_path)
    assert server_config(tls_settings, FastAPI(), 8443).is_ssl


def test_official_entrypoint_fails_before_serving(settings, monkeypatch, capsys):
    monkeypatch.setattr(startup, "configure_logging", lambda *a: None)
    monkeypatch.setattr(startup.Settings, "from_env", lambda: replace(settings, bind_host="0.0.0.0"))
    monkeypatch.setattr(startup, "create_app", lambda *a: pytest.fail("refuse before app construction"))
    assert startup.main([]) == 1
    assert capsys.readouterr().err.startswith("tls_required:")


def test_tls_load_failure_never_starts_server_or_logs_input(tls_settings, monkeypatch, capsys, caplog):
    monkeypatch.setattr(startup, "configure_logging", lambda *a: None)
    monkeypatch.setattr(startup.Settings, "from_env", lambda: tls_settings)
    monkeypatch.setattr(startup.uvicorn.Server, "run", lambda *a: pytest.fail("must not serve"))
    assert startup.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("tls_certificate_load_failed:")
    for value in (str(tls_settings.tls_key_path), "NONSECRET MOCK KEY", "Traceback"):
        assert value not in captured.err + caplog.text


def test_official_entrypoint_uses_loaded_tls_context(tls_settings, monkeypatch):
    calls = []
    monkeypatch.setattr(startup, "configure_logging", lambda *a: None)
    monkeypatch.setattr(startup.Settings, "from_env", lambda: tls_settings)
    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", lambda *a, **k: None)
    monkeypatch.setenv("LOCALPLANE_PORT", "8443")
    monkeypatch.setattr(startup.uvicorn.Server, "run", lambda server: calls.append(server.config))
    assert startup.main([]) == 0
    assert len(calls) == 1 and calls[0].loaded and calls[0].is_ssl
    assert calls[0].ssl.minimum_version == ssl.TLSVersion.TLSv1_2
    assert calls[0].workers == 1 and calls[0].proxy_headers is False


@pytest.fixture
def client(settings, build):
    database = open_database(":memory:")
    configured = replace(settings, console_dir=build, bind_host="192.0.2.10")
    app = create_app(configured, database, authentication=Authentication(TEST_MASTER_SECRET, bind_host=configured.bind_host))
    with TestClient(app, base_url="https://192.0.2.10:8443") as client:
        yield client
    database.close()


@pytest.mark.parametrize("path", [
    "/", "/index.html", "/network", "/network/obj_a", "/system", "/system/obj_abc?tab=journal",
    "/workloads", "/workloads/runtime", "/workloads/obj_d", "/storage", "/settings",
    "/operations", "/operations/findings", "/operations/findings/fnd_abc",
    "/operations/findings/own_abc", "/operations/runs/run_abc", "/operations/changes/chg_abc",
])
def test_actual_spa_routes_are_public_same_origin_documents(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert 'id="root"' in response.text
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "/assets/index-Abcd1234.js" in response.text
    head = client.head(path)
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == response.headers["content-length"]


@pytest.mark.parametrize("name,mime", [
    ("index-Abcd1234.js", "text/javascript"), ("index-Abcd1234.css", "text/css"),
    ("font-Abcd1234.woff2", "font/woff2"),
])
def test_assets_have_truthful_types_head_and_revalidation(client, name, mime):
    url = "/assets/" + name
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(mime)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "must-revalidate" in response.headers["cache-control"]
    assert client.head(url).content == b""
    unchanged = client.get(url, headers={"If-None-Match": response.headers["etag"]})
    assert unchanged.status_code == 304 and unchanged.content == b""


@pytest.mark.parametrize("path", [
    "/unknown", "/api/no-such-route", "/api/v1/no-such-route", "/docs/private", "/redoc/private",
    "/openapi.json/private", "/src/main.tsx", "/.env", "/var/master.secret", "/assets/",
    "/assets/missing-Abcd1234.js", "/assets/index-Abcd1234.js.map", "/assets/.env",
    "/assets/%2e%2e/index.html", "/assets/%252e%252e%252findex.html",
    "/assets/%2findex-Abcd1234.js", "/%69ndex.html", "/system/%2e%2e",
    "/system/not-an-object", "/operations/runs/run_a/extra", "/assets/sub/index-Abcd1234.js",
])
def test_unknown_private_and_encoded_paths_are_never_index(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 404
    assert 'id="root"' not in response.text
    assert "NONPUBLIC" not in response.text


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@pytest.mark.parametrize("path", ["/", "/system/obj_a", "/assets/index-Abcd1234.js", "/docs", "/openapi.json"])
def test_static_routes_do_not_swallow_method_boundaries(client, method, path):
    assert client.request(method, path).status_code == 405


@pytest.mark.parametrize("path", ["/api/v1/status", "/docs", "/redoc", "/openapi.json"])
def test_api_and_documentation_authentication_unchanged(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers=TEST_AUTHORIZATION).status_code == 200
    assert client.head(path).status_code == 405


def test_console_leaves_schema_and_session_origin_logout_truth_intact(client, settings):
    assert client.app.openapi() == create_app(settings).openapi()
    login = client.post("/api/v1/session", headers=TEST_AUTHORIZATION)
    assert login.status_code == 200
    assert "Secure" in login.headers["set-cookie"] and "HttpOnly" in login.headers["set-cookie"]
    assert SESSION_COOKIE in client.cookies
    assert client.delete("/api/v1/session").status_code == 403
    assert client.get("/api/v1/status").status_code == 200
    assert client.delete("/api/v1/session", headers={"Origin": "https://192.0.2.10:8444"}).status_code == 403
    assert client.delete("/api/v1/session", headers={"Origin": "https://192.0.2.10:8443"}).status_code == 204
    assert client.get("/api/v1/status").status_code == 401


def test_replacing_build_with_links_after_startup_cannot_expose_files(client, build, tmp_path):
    before = client.get("/assets/index-Abcd1234.js").content
    outside = tmp_path / "private"
    outside.write_text("NONPUBLIC AFTER STARTUP")
    asset = build / "assets/index-Abcd1234.js"
    asset.unlink()
    asset.symlink_to(outside)
    assert client.get("/assets/index-Abcd1234.js").content == before
    (build / "assets/new-Abcd1234.js").write_text("NOT IN SNAPSHOT")
    assert client.get("/assets/new-Abcd1234.js").status_code == 404


@pytest.mark.parametrize("kind", [
    "missing_index", "missing_asset", "source_index", "empty_index", "no_root", "remote_script", "parser_failure",
    "index_symlink", "asset_symlink", "asset_directory", "assets_symlink", "root_symlink",
    "index_oversize", "asset_oversize", "total_oversize", "too_many", "writable_asset",
])
def test_invalid_build_fails_closed_with_sanitized_startup_error(build, tmp_path, monkeypatch, kind):
    index, asset = build / "index.html", build / "assets/index-Abcd1234.js"
    if kind == "missing_index":
        index.unlink()
    elif kind == "missing_asset":
        asset.unlink()
    elif kind == "parser_failure":
        def fail_parser(*args):
            raise AssertionError("NONPUBLIC parser input")
        monkeypatch.setattr(console._Index, "feed", fail_parser)
    elif kind in {"source_index", "empty_index", "no_root", "remote_script"}:
        content = index.read_text()
        if kind == "source_index":
            content = content.replace("/assets/index-Abcd1234.js", "/src/main.tsx")
        elif kind == "empty_index":
            content = ""
        elif kind == "no_root":
            content = content.replace('id="root"', 'id="other"')
        else:
            content = content.replace("/assets/index-Abcd1234.js", "https://example.invalid/script.js")
        index.write_text(content)
    elif kind in {"index_symlink", "asset_symlink"}:
        target = tmp_path / "outside"
        target.write_text("NONPUBLIC OUTSIDE")
        file = index if kind == "index_symlink" else asset
        file.unlink()
        file.symlink_to(target)
    elif kind == "asset_directory":
        asset.unlink()
        asset.mkdir()
    elif kind == "assets_symlink":
        target = tmp_path / "assets"
        (build / "assets").rename(target)
        (build / "assets").symlink_to(target, target_is_directory=True)
    elif kind == "root_symlink":
        link = tmp_path / "build-link"
        link.symlink_to(build, target_is_directory=True)
        build = link
    elif kind == "index_oversize":
        monkeypatch.setattr(console, "MAX_INDEX_BYTES", 5)
    elif kind == "asset_oversize":
        monkeypatch.setattr(console, "MAX_ASSET_BYTES", 5)
    elif kind == "total_oversize":
        monkeypatch.setattr(console, "MAX_BUILD_BYTES", 5)
    elif kind == "too_many":
        monkeypatch.setattr(console, "MAX_FILES", 1)
    else:
        asset.chmod(0o666)
    with pytest.raises(ServingConfigurationError, match="console_build_invalid") as caught:
        console.load_console(build)
    assert str(tmp_path) not in str(caught.value) and "NONPUBLIC" not in str(caught.value)


def test_console_remains_disabled_by_default(settings):
    app = create_app(settings)
    client = TestClient(app)
    assert client.get("/").status_code == 404
    assert client.get("/system/obj_abc").status_code == 404
    assert client.get("/assets/index-Abcd1234.js").status_code == 404
