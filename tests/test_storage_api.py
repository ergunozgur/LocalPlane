import json

import pytest
from fastapi.testclient import TestClient
from fastapi.openapi.models import Schema
from pydantic import ValidationError

from localplane.agent.server import AgentServer
from localplane.agent.service import AgentService
from localplane.backend.agent_client import AgentClient, AgentError
from localplane.backend.config import Settings
from localplane.backend.api.storage_schemas import StorageMount
from localplane.backend.db.database import open_database
from localplane.protocol.capabilities import CAPABILITY_STORAGE_OBSERVE
from localplane.protocol.wire import ErrorCode, Method, ProtocolError
from tests.conftest import AuthenticatedTestClient, create_authenticated_app, TEST_MASTER_SECRET
from tests.storage_fixture import FixtureStorageProvider


@pytest.fixture
def storage_service(fake_root, populated_sysfs, working_runner, absent_docker):
    provider = FixtureStorageProvider()
    service = AgentService(root=fake_root, sysfs_net=populated_sysfs, runner=working_runner,
                           docker_socket=absent_docker, storage_provider=provider)
    return service, provider


def test_closed_service_and_capability(storage_service):
    service, provider = storage_service
    assert service.handle(Method.STORAGE_OBSERVE_FILESYSTEMS, {})["status"] == "partial"
    assert provider.calls == 1
    for params in [{"path": "/arbitrary"}, {"timeout": 100}, {"namespace": "other"}]:
        with pytest.raises(ProtocolError):
            service.handle(Method.STORAGE_OBSERVE_FILESYSTEMS, params)
    assert provider.calls == 1
    capability = next(c for c in service._capabilities if c.capability == CAPABILITY_STORAGE_OBSERVE)
    assert not capability.mutating
    assert capability.reason == "filesystem_access_unverified"
    service._capabilities = tuple(c for c in service._capabilities if c.capability != CAPABILITY_STORAGE_OBSERVE)
    with pytest.raises(ProtocolError) as failure:
        service.handle(Method.STORAGE_OBSERVE_FILESYSTEMS, {})
    assert failure.value.code == ErrorCode.CAPABILITY_UNAVAILABLE


@pytest.fixture
def wired(tmp_path, storage_service):
    service, provider = storage_service
    server = AgentServer(tmp_path / "agent.sock", service)
    thread = server.serve_in_thread()
    settings = Settings(tmp_path / "storage.db", server.socket_path, 5, 60, "WARNING", False)
    database = open_database(settings.database_path)
    app = create_authenticated_app(settings, database)
    with AuthenticatedTestClient(app) as client:
        yield client, provider, app
    database.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_authenticated_api_over_real_protocol_and_no_retention(wired):
    client, provider, app = wired
    before = app.state.context.database.connection.total_changes
    response = client.post("/api/v1/storage/filesystems/observe")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["mounts"][0]["bytes"]["available"] == "0"
    assert data["mounts"][1]["inodes"]["available"] == "0"
    assert data["mounts"][2]["bytes"] is None
    assert provider.calls == 1
    assert app.state.context.database.connection.total_changes == before
    assert client.post("/api/v1/storage/filesystems/observe?path=/other").status_code == 400
    assert client.post("/api/v1/storage/filesystems/observe", json={"path": "/other"}).status_code == 422
    assert provider.calls == 1
    assert client.get("/api/v1/storage/filesystems/observe").status_code == 405


def test_auth_and_csrf_not_bypassed(wired):
    client, provider, app = wired
    with TestClient(app) as anonymous:
        assert anonymous.post("/api/v1/storage/filesystems/observe").status_code == 401
        login = anonymous.post("/api/v1/session", headers={"Authorization": f"Bearer {TEST_MASTER_SECRET}"})
        assert login.status_code == 200
        assert anonymous.post("/api/v1/storage/filesystems/observe").status_code == 403
        assert provider.calls == 0


def test_agent_failure_and_malformed_response_explicit(wired, monkeypatch):
    client, provider, app = wired
    for code, status in [(ErrorCode.AGENT_UNAVAILABLE, 503), (ErrorCode.TIMEOUT, 504),
                         (ErrorCode.UNSUPPORTED_METHOD, 503), (ErrorCode.CAPABILITY_UNAVAILABLE, 503)]:
        def fail():
            raise AgentError(code, "Synthetic unavailable")
        monkeypatch.setattr(app.state.context.client, "observe_filesystems", fail)
        assert client.post("/api/v1/storage/filesystems/observe").status_code == status
    monkeypatch.setattr(app.state.context.client, "observe_filesystems", lambda: {"status": "complete"})
    response = client.post("/api/v1/storage/filesystems/observe")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "malformed_storage_evidence"


def test_typed_client_has_no_caller_path(monkeypatch):
    client = AgentClient("unused.sock")
    calls = []
    monkeypatch.setattr(client, "call", lambda *args: calls.append(args) or {})
    client.observe_filesystems()
    assert calls == [(Method.STORAGE_OBSERVE_FILESYSTEMS,)]


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_mount_id_schema_bound_literals_are_stable(mode):
    properties = StorageMount.model_json_schema(mode=mode)["properties"]
    for field, lower in [("mount_id", "exclusiveMinimum"), ("parent_id", "minimum")]:
        schema = properties[field]
        assert schema["type"] == "integer"
        bounds = {lower: 0.0, "exclusiveMaximum": 9007199254740992.0}
        for key, value in bounds.items():
            assert type(schema[key]) is float
            assert schema[key] == value
        # Compare serialized text: Python numeric equality hides 0 versus 0.0 drift.
        converted = Schema.model_validate(schema).model_dump(exclude_none=True)
        assert json.dumps(schema, sort_keys=True) == json.dumps(converted, sort_keys=True)


@pytest.mark.parametrize("field,valid", [("mount_id", [1, 2**53 - 1]), ("parent_id", [0, 2**53 - 1])])
def test_mount_id_schema_override_preserves_strict_runtime_bounds(field, valid):
    row = FixtureStorageProvider().observe()["mounts"][0]
    for value in valid:
        assert getattr(StorageMount.model_validate(row | {field: value}), field) == value
    for value in [-1, 2**53, 2**53 + 1, 1.0, "1", True, None]:
        with pytest.raises(ValidationError):
            StorageMount.model_validate(row | {field: value})
    with pytest.raises(ValidationError):
        StorageMount.model_validate(row | {"mount_id": 0})
