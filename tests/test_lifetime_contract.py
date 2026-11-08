"""Address lifetimes end to end: provider → stored facts → API.

The list API used to expose only nullable seconds, so a caller could not tell "the source
did not say" from "the kernel says forever". These tests hold the additive state fields to
that distinction across the real observation and ingestion path, including legacy rows that
predate the state fields.
"""

from __future__ import annotations

import json

import pytest

from localplane.agent.providers.linux_network import ADDR_ARGV, LINK_ARGV, LinuxNetworkProvider
from tests.conftest import FakeRunner, json_result, write_interface
from tests.test_api import client as _client
from tests.test_api import running_agent as _running_agent


def _raw(fixture):
    return getattr(fixture, "__wrapped__", fixture)


client = pytest.fixture(_raw(_client))
running_agent = pytest.fixture(_raw(_running_agent))


def _interfaces(client) -> dict[str, dict]:
    body = client.get("/api/v1/network/interfaces").json()
    return {interface["name"]: interface for interface in body["interfaces"]}


def test_provider_ingestion_storage_api_roundtrip_keeps_zero_finite_infinite_and_unknown(
    client, tmp_path
):
    """One address each: finite zero, finite max, infinite sentinel, malformed lifetime."""
    sysfs = tmp_path / "roundtrip" / "sys" / "class" / "net"
    sysfs.mkdir(parents=True)
    write_interface(sysfs, "lifetimes0", ifindex=41, address="02:00:00:00:00:41")

    runner = FakeRunner(
        {
            LINK_ARGV: json_result(LINK_ARGV, [{"ifindex": 41, "ifname": "lifetimes0"}]),
            ADDR_ARGV: json_result(
                ADDR_ARGV,
                [
                    {
                        "ifindex": 41,
                        "ifname": "lifetimes0",
                        "addr_info": [
                            {
                                "family": "inet",
                                "local": "192.0.2.41",
                                "prefixlen": 24,
                                "scope": "global",
                                "valid_life_time": 0,
                                "preferred_life_time": 4294967295,
                            },
                            {
                                "family": "inet6",
                                "local": "2001:db8::41",
                                "prefixlen": 64,
                                "scope": "global",
                                "valid_life_time": 4294967294,
                                "preferred_life_time": 1,
                            },
                            {
                                "family": "inet",
                                "local": "192.0.2.42",
                                "prefixlen": 24,
                                "scope": "global",
                                "valid_life_time": "3600",
                                "preferred_life_time": True,
                            },
                        ],
                    }
                ],
            ),
        }
    )
    batch = LinuxNetworkProvider(sysfs_net=sysfs, runner=runner).observe_interfaces()

    context = client.app.state.context
    host = context.ingestor.hosts.most_recent()
    assert host is not None
    context.ingestor.ingest_network_sweep(
        {
            "observation": batch.as_dict(),
            "host_id": host.host_id,
            "agent_instance_id": None,
        }
    )

    addresses = _interfaces(client)["lifetimes0"]["addresses"]
    by_value = {address["address"]: address for address in addresses}
    assert by_value["192.0.2.41"]["valid_lifetime_s"] == 0
    assert by_value["192.0.2.41"]["valid_lifetime_state"] == "finite"
    assert by_value["192.0.2.41"]["preferred_lifetime_s"] is None
    assert by_value["192.0.2.41"]["preferred_lifetime_state"] == "infinite"
    assert by_value["2001:db8::41"]["valid_lifetime_s"] == 4294967294
    assert by_value["2001:db8::41"]["valid_lifetime_state"] == "finite"
    assert by_value["2001:db8::41"]["preferred_lifetime_state"] == "finite"
    assert by_value["192.0.2.42"]["valid_lifetime_s"] is None
    assert by_value["192.0.2.42"]["valid_lifetime_state"] == "unknown"
    assert by_value["192.0.2.42"]["preferred_lifetime_s"] is None
    assert by_value["192.0.2.42"]["preferred_lifetime_state"] == "unknown"


def test_legacy_stored_finite_is_finite_and_legacy_null_stays_unknown(client):
    """Rows stored before the state field have no evidence of infinity, so none is invented."""
    context = client.app.state.context
    host = context.ingestor.hosts.most_recent()
    objects = context.ingestor.objects.list_by_kind(host.host_id, "network.interface")
    eth1 = next(record for record in objects if record.display_name == "eth1")
    observation_id = eth1.observation.observation_id
    legacy_facts = dict(eth1.observation.facts)
    legacy_facts["addresses"] = [
        {
            "family": "inet",
            "address": "192.0.2.215",
            "prefix_length": 24,
            "scope": "global",
            "dynamic": True,
            "valid_lifetime_s": 41039,
            "preferred_lifetime_s": None,
        }
    ]
    context.database.execute(
        "UPDATE observations SET facts = ? WHERE observation_id = ?",
        (json.dumps(legacy_facts), observation_id),
    )

    address = _interfaces(client)["eth1"]["addresses"][0]
    assert address["valid_lifetime_s"] == 41039
    assert address["valid_lifetime_state"] == "finite"
    assert address["preferred_lifetime_s"] is None
    # The old normalization erased the forever sentinel. Null alone cannot prove infinity.
    assert address["preferred_lifetime_state"] == "unknown"


def test_legacy_stored_sentinel_and_out_of_range_are_not_presented_as_finite(client):
    """A stored sentinel is infinite; corrupted numbers are unknown rather than plausible."""
    context = client.app.state.context
    host = context.ingestor.hosts.most_recent()
    eth1 = next(
        record
        for record in context.ingestor.objects.list_by_kind(host.host_id, "network.interface")
        if record.display_name == "eth1"
    )
    facts = dict(eth1.observation.facts)
    facts["addresses"] = [
        {
            "family": "inet",
            "address": "192.0.2.215",
            "prefix_length": 24,
            "valid_lifetime_s": 4294967295,
            "preferred_lifetime_s": -1,
        }
    ]
    context.database.execute(
        "UPDATE observations SET facts = ? WHERE observation_id = ?",
        (json.dumps(facts), eth1.observation.observation_id),
    )

    address = _interfaces(client)["eth1"]["addresses"][0]
    assert address["valid_lifetime_s"] is None
    assert address["valid_lifetime_state"] == "infinite"
    assert address["preferred_lifetime_s"] is None
    assert address["preferred_lifetime_state"] == "unknown"


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ({}, (None, "unknown")),
        ({"valid_lifetime_s": None}, (None, "unknown")),
        ({"valid_lifetime_s": 0}, (0, "finite")),
        ({"valid_lifetime_s": 120, "valid_lifetime_state": "finite"}, (120, "finite")),
        (
            {"valid_lifetime_s": None, "valid_lifetime_state": "infinite"},
            (None, "infinite"),
        ),
        ({"valid_lifetime_s": 120, "valid_lifetime_state": "infinite"}, (None, "unknown")),
        ({"valid_lifetime_s": None, "valid_lifetime_state": "finite"}, (None, "unknown")),
        ({"valid_lifetime_s": 120, "valid_lifetime_state": "bogus"}, (None, "unknown")),
        (
            {"valid_lifetime_s": 120, "valid_lifetime_state": {"bad": "dict"}},
            (None, "unknown"),
        ),
        (
            {"valid_lifetime_s": 120, "valid_lifetime_state": ["list"]},
            (None, "unknown"),
        ),
        ({"valid_lifetime_s": 120, "valid_lifetime_state": 5}, (None, "unknown")),
        ({"valid_lifetime_s": True, "valid_lifetime_state": "infinite"}, (None, "unknown")),
        ({"valid_lifetime_s": "120", "valid_lifetime_state": "infinite"}, (None, "unknown")),
        ({"valid_lifetime_s": 120, "valid_lifetime_state": "unknown"}, (None, "unknown")),
        (
            {"valid_lifetime_s": 4294967295, "valid_lifetime_state": "finite"},
            (None, "infinite"),
        ),
        ({"valid_lifetime_s": 4294967295}, (None, "infinite")),
        ({"valid_lifetime_s": -1, "valid_lifetime_state": "infinite"}, (None, "unknown")),
        ({"valid_lifetime_s": 4294967296, "valid_lifetime_state": "finite"}, (None, "unknown")),
    ],
)
def test_stored_lifetime_pair_validation_table(address, expected):
    from localplane.backend.api.views import _lifetime_view

    assert (
        _lifetime_view(address, "valid_lifetime_s", "valid_lifetime_state") == expected
    )


def test_malformed_stored_state_is_unknown_over_http_not_a_server_error(client):
    context = client.app.state.context
    host = context.ingestor.hosts.most_recent()
    eth1 = next(
        record
        for record in context.ingestor.objects.list_by_kind(host.host_id, "network.interface")
        if record.display_name == "eth1"
    )
    facts = dict(eth1.observation.facts)
    facts["addresses"] = [
        {
            "family": "inet",
            "address": "192.0.2.215",
            "prefix_length": 24,
            "valid_lifetime_s": 120,
            "valid_lifetime_state": {"unhashable": "state"},
            "preferred_lifetime_s": 60,
            "preferred_lifetime_state": ["unhashable"],
        }
    ]
    context.database.execute(
        "UPDATE observations SET facts = ? WHERE observation_id = ?",
        (json.dumps(facts), eth1.observation.observation_id),
    )

    response = client.get("/api/v1/network/interfaces")
    assert response.status_code == 200
    address = _interfaces(client)["eth1"]["addresses"][0]
    assert address["valid_lifetime_s"] is None
    assert address["valid_lifetime_state"] == "unknown"
    assert address["preferred_lifetime_s"] is None
    assert address["preferred_lifetime_state"] == "unknown"


def test_address_state_defaults_are_unknown_for_older_typed_fixtures():
    from localplane.backend.api.schemas import Address

    address = Address(
        family="inet",
        address="192.0.2.1",
        prefix_length=24,
        scope=None,
        dynamic=None,
        valid_lifetime_s=None,
        preferred_lifetime_s=None,
    )
    assert address.valid_lifetime_state == "unknown"
    assert address.preferred_lifetime_state == "unknown"


def test_address_state_and_seconds_are_forbidden_to_arrive_by_extra_fields():
    """The API schema is closed: a caller cannot post an address shape with invented keys."""
    from pydantic import ValidationError

    from localplane.backend.api.schemas import Address

    with pytest.raises(ValidationError):
        Address(
            family="inet",
            address="192.0.2.1",
            prefix_length=24,
            scope=None,
            dynamic=None,
            valid_lifetime_s=None,
            preferred_lifetime_s=None,
            valid_lifetime_state="infinite",
            preferred_lifetime_state="unknown",
            lifetime_override=1,
        )
