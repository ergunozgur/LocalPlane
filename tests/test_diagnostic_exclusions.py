"""Synthetic inventories only; never execute ip or read the live namespace."""
import json
from types import SimpleNamespace

import pytest

from localplane.agent.providers import diagnostic_exclusions as exclusions


CONTEXT = ("12345678-1234-4234-8234-123456789abc", "net:[42]")


def inventory():
    return [{"ifindex": 2, "ifname": "eth0", "flags": ["UP", "BROADCAST"], "addr_info": [
        {"family": "inet", "local": "192.168.10.2", "prefixlen": 24, "broadcast": "192.168.10.255"},
        {"family": "inet6", "local": "fd00::2", "prefixlen": 64},
    ]}, {"ifindex": 3, "ifname": "tun0", "flags": ["POINTOPOINT"], "addr_info": [
        {"family": "inet", "local": "10.0.0.2", "prefixlen": 32},
    ]}]


def data(rows):
    return json.dumps(rows).encode()


def test_complete_multiple_addresses_and_conservative_broadcast():
    rows = inventory()
    rows[0]["addr_info"][0]["broadcast"] = "192.168.10.254"
    answer = exclusions.collect(runner=lambda _, **kw: data(rows), context_reader=lambda: CONTEXT)
    assert set(answer.local_addresses) == {"192.168.10.2", "fd00::2", "10.0.0.2"}
    assert set(answer.broadcast_addresses) == {"192.168.10.254", "192.168.10.255"}
    assert answer.network_namespace == CONTEXT[1]


@pytest.mark.parametrize("mutation", [
    lambda rows: rows.append(None),
    lambda rows: rows[1].update(ifindex=2),
    lambda rows: rows[0].pop("flags"),
    lambda rows: rows[0]["addr_info"][0].pop("broadcast"),
    lambda rows: rows[0]["addr_info"][0].update(prefixlen=True),
    lambda rows: rows[0]["addr_info"][0].update(family="inet6"),
    lambda rows: rows[0]["addr_info"][1].update(broadcast="fd00::1"),
    lambda rows: rows[0]["addr_info"].append({"family": "other", "local": "x", "prefixlen": 0}),
])
def test_any_bad_record_rejects_inventory(mutation):
    rows = inventory()
    mutation(rows)
    with pytest.raises(ValueError):
        exclusions.parse_addresses(data(rows))


def test_truncated_duplicate_and_oversize_rejected():
    for raw in (b'[{', b'[{"ifindex":1,"ifindex":2}]', b' ' * (1024 * 1024 + 1)):
        with pytest.raises(ValueError):
            exclusions.parse_addresses(raw)


def test_context_and_inventory_race_rejected():
    contexts = iter([CONTEXT, CONTEXT, (CONTEXT[0], "net:[43]")])
    with pytest.raises(ValueError):
        exclusions.collect(runner=lambda _, **kw: data(inventory()), context_reader=lambda: next(contexts))
    changed = inventory()
    changed[1]["ifname"] = "tun1"
    snapshots = iter([data(inventory()), data(changed)])
    with pytest.raises(ValueError):
        exclusions.collect(runner=lambda _, **kw: next(snapshots), context_reader=lambda: CONTEXT)


def test_order_and_lifetime_changes_are_not_security_changes():
    first = inventory()
    first[0]["addr_info"][0]["valid_life_time"] = 100
    second = inventory()
    second.reverse()
    second[1]["addr_info"].reverse()
    second[1]["addr_info"][1]["valid_life_time"] = 99
    snapshots = iter([data(first), data(second)])
    assert exclusions.collect(runner=lambda _, **kw: next(snapshots), context_reader=lambda: CONTEXT).boot_id == CONTEXT[0]


def test_shared_deadline_cannot_reset_between_calls():
    now = [10.0]
    seen = []
    def runner(_, *, deadline):
        seen.append(deadline)
        now[0] += 2.6
        return data(inventory())
    with pytest.raises(TimeoutError):
        exclusions.collect(runner=runner, context_reader=lambda: CONTEXT, clock=lambda: now[0], deadline=100.0)
    assert seen == [15.0, 15.0]


@pytest.mark.parametrize("mutation", [
    lambda rows: rows[0].update(ifindex=4),
    lambda rows: rows[0].update(ifname="eth9"),
    lambda rows: rows[0]["flags"].append("LOWER_UP"),
    lambda rows: rows[0]["addr_info"][0].update(local="192.168.10.3"),
    lambda rows: rows[0]["addr_info"][0].update(prefixlen=25),
    lambda rows: rows[0]["addr_info"][0].update(broadcast="192.168.10.254"),
])
def test_security_relevant_changes_reject_snapshot(mutation):
    changed = inventory()
    mutation(changed)
    snapshots = iter([data(inventory()), data(changed)])
    with pytest.raises(ValueError):
        exclusions.collect(runner=lambda _, **kw: next(snapshots), context_reader=lambda: CONTEXT)


def test_closed_preflight_and_status_use_expected_identity(tmp_path, monkeypatch):
    from tests.connectivity_diagnostic_fixtures import diagnostic_rig
    from localplane.protocol.wire import ProtocolError
    rig = diagnostic_rig(tmp_path, monkeypatch)
    try:
        values = rig.values()
        preflight = rig.agent._diagnostic_preflight(values)
        assert preflight["complete"] is True and preflight["local_addresses"] == list(rig.local)
        expected = {"expected_host_id": "host", "expected_agent_instance_id": "agent", "ticket": preflight["ticket"]}
        assert rig.agent._diagnostic_status(expected)["state"] == "pending"
        assert rig.agent._diagnostic_preflight(values | {"expected_namespace": "net:[99]"}) == {"status": "unavailable"}
        with pytest.raises(ProtocolError):
            rig.agent._diagnostic_status(expected | {"expected_agent_instance_id": "changed"})
        with pytest.raises(ProtocolError):
            rig.agent._diagnostic_preflight(values | {"extra": True})
    finally:
        rig.ctx.database.close()


def test_backend_policy_metadata_describes_only_its_own_loaded_policy():
    from localplane.backend.connectivity import _policy
    target = SimpleNamespace(address="192.168.1.2", port=443)
    def metadata(policy):
        return _policy(SimpleNamespace(status="valid", policy=policy))
    legacy = metadata(SimpleNamespace(digest="a" * 64, dns=(), tcp=(target,)))
    assert legacy.version == 1 and legacy.tcp_status == "missing_declaration"
    v2 = SimpleNamespace(digest="b" * 64, dns=(), tcp=(target,), version=2,
                         control_plane_complete=True, control_plane_addresses=("192.168.1.1",))
    result = metadata(v2)
    assert result.tcp_status == "ready" and result.address_count == 1 and result.control_plane_complete is True
    v2.control_plane_complete = False
    assert metadata(v2).tcp_status == "incomplete_declaration"
    v2.tcp = ()
    assert metadata(v2).tcp_status == "disabled"
