"""Sweep-boundary freshness.

A sweep's currency comes from its own completed_at and the backend's configured TTL. It is
not inherited from an object row, and a fresh targeted observation cannot make an older
inventory sweep current — the empty-but-ok estate is exactly where that distinction matters.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from localplane.backend.db.database import open_database
from localplane.protocol.capabilities import CAPABILITY_NETWORK_OBSERVE
from tests.conftest import AuthenticatedTestClient, create_authenticated_app
from tests.test_api import _settings
from tests.test_api import client as _client
from tests.test_api import running_agent as _running_agent


def _raw(fixture):
    return getattr(fixture, "__wrapped__", fixture)


client = pytest.fixture(_raw(_client))
running_agent = pytest.fixture(_raw(_running_agent))


@pytest.fixture
def short_ttl_client(tmp_path, running_agent):
    settings = replace(_settings(tmp_path, running_agent.socket_path), freshness_ttl_s=5.0)
    database = open_database(settings.database_path)
    with AuthenticatedTestClient(create_authenticated_app(settings, database)) as test_client:
        yield test_client
    database.close()


def _ts(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _insert_sweep(
    client,
    *,
    sweep_id: str,
    completed_at: datetime,
    received_at: datetime,
    scope: str = "inventory",
    status: str = "ok",
    object_count: int = 0,
) -> None:
    context = client.app.state.context
    host = context.ingestor.hosts.most_recent()
    assert host is not None
    context.ingestor.sweeps.insert(
        {
            "sweep_id": sweep_id,
            "host_id": host.host_id,
            "agent_instance_id": None,
            "capability": CAPABILITY_NETWORK_OBSERVE,
            "scope": scope,
            "provider": "linux.network",
            "provider_version": "test",
            "status": status,
            "started_at": _ts(completed_at),
            "completed_at": _ts(completed_at),
            "received_at": _ts(received_at),
            "object_count": object_count,
            "missing": [],
            "issues": [],
        }
    )


def _network_body(client) -> dict:
    return client.get("/api/v1/network/interfaces").json()


def test_a_stale_empty_ok_inventory_sweep_is_stale(client):
    now = datetime.now(timezone.utc)
    captured_at = now - timedelta(hours=1)
    _insert_sweep(
        client,
        sweep_id="sweep_stale_empty",
        completed_at=captured_at,
        received_at=now + timedelta(seconds=10),
    )

    body = _network_body(client)
    sweep = body["last_sweep"]
    assert sweep["sweep_id"] == "sweep_stale_empty"
    assert sweep["status"] == "ok"
    assert sweep["object_count"] == 0
    assert sweep["freshness"] == "stale"
    assert sweep["age_seconds"] >= 3500
    # The estate is still retained, but this empty inventory is not its current membership.
    assert body["interfaces"]
    assert all(item["observed_in_latest_sweep"] is False for item in body["interfaces"])


def test_a_fresh_empty_ok_inventory_sweep_is_current_with_near_zero_age(client):
    now = datetime.now(timezone.utc)
    _insert_sweep(
        client,
        sweep_id="sweep_fresh_empty",
        completed_at=now,
        received_at=now + timedelta(seconds=10),
    )

    sweep = _network_body(client)["last_sweep"]
    assert sweep["sweep_id"] == "sweep_fresh_empty"
    assert sweep["status"] == "ok"
    assert sweep["object_count"] == 0
    assert sweep["freshness"] == "current"
    assert 0 <= sweep["age_seconds"] < 2


def test_a_fresh_targeted_sweep_does_not_make_an_old_inventory_sweep_current(client):
    now = datetime.now(timezone.utc)
    _insert_sweep(
        client,
        sweep_id="sweep_old_inventory",
        completed_at=now - timedelta(hours=1),
        received_at=now + timedelta(seconds=10),
    )
    _insert_sweep(
        client,
        sweep_id="sweep_new_targeted",
        completed_at=now,
        received_at=now + timedelta(seconds=20),
        scope="targeted",
    )

    sweep = _network_body(client)["last_sweep"]
    assert sweep["sweep_id"] == "sweep_old_inventory"
    assert sweep["freshness"] == "stale"
    assert sweep["age_seconds"] >= 3500

    # The targeted row is genuinely fresh; it simply is not the inventory boundary.
    sweeps = client.get("/api/v1/observations/sweeps").json()["sweeps"]
    targeted = next(item for item in sweeps if item["sweep_id"] == "sweep_new_targeted")
    assert targeted["freshness"] == "current"


def test_the_configured_ttl_governs_sweep_freshness(short_ttl_client):
    now = datetime.now(timezone.utc)
    _insert_sweep(
        short_ttl_client,
        sweep_id="sweep_ttl_sensitive",
        completed_at=now - timedelta(seconds=30),
        received_at=now + timedelta(seconds=10),
    )

    sweep = _network_body(short_ttl_client)["last_sweep"]
    assert sweep["sweep_id"] == "sweep_ttl_sensitive"
    assert sweep["freshness"] == "stale", "30s is stale under the configured 5s TTL"
    assert 28 <= sweep["age_seconds"] <= 35
