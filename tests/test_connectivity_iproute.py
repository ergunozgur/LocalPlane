import json

import pytest

from localplane.agent.providers.connectivity_iproute import COMMANDS, MAX_BYTES, observe, parse_inventory, _collect


def test_route_and_rule_normalization_keeps_metric_unknown_and_multipath():
    routes = [{"dst": "default", "table": 100, "type": "unicast", "nexthops": [
        {"gateway": "192.168.1.1", "dev": "eth0", "weight": 2}, {"gateway": "192.168.2.1", "dev": "eth1"}]}]
    route = parse_inventory(json.dumps(routes).encode(), "ipv4", "routes")[0]
    assert route["table"] == "100" and route["metric"] is None
    assert len(route["nexthops"]) == 2 and route["nexthops"][1]["weight"] is None
    rule = parse_inventory(b'[{"priority":100,"table":"123","fwmark":"0x1","iif":"eth0"}]', "ipv4", "rules")[0]
    assert rule["fwmark"] == "0x1" and rule["incoming_interface"] == "eth0"


@pytest.mark.parametrize("payload", [b'{}', b'[{"dst":true}]', b'[{"dst":"default","metric":true}]', b'[{"dst":"default","nexthops":{}}]', b'[{"table":"main"}]', b'[' + b' ' * MAX_BYTES])
def test_malformed_inventory_is_not_empty(payload):
    with pytest.raises((ValueError, UnicodeError)):
        parse_inventory(payload, "ipv4", "routes")


def test_family_failures_are_independent_and_commands_are_fixed():
    calls = []
    def runner(argv):
        calls.append(argv)
        if argv == COMMANDS["routes6"]:
            raise TimeoutError()
        return b'[]'
    result = observe(runner)
    assert calls == list(COMMANDS.values())
    assert result["routes"]["completeness"] == "partial" and result["routes"]["value"] == []
    assert result["rules"]["completeness"] == "complete" and result["rules"]["value"] == []


def test_fixed_runner_caps_output_without_executing_a_process(monkeypatch):
    from types import SimpleNamespace
    def fake_run(argv, *, stdout, **kwargs):
        stdout.write(b"[]")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("localplane.agent.providers.connectivity_iproute.subprocess.run", fake_run)
    assert _collect(COMMANDS["routes4"]) == b"[]"
    with pytest.raises(ValueError):
        _collect(("ip", "route", "delete", "default"))


def test_unrepresented_fields_make_evidence_partial():
    def runner(argv):
        return b'[{"dst":"default","encap":{"type":"example"}}]' if argv in (COMMANDS["routes4"], COMMANDS["routes6"]) else b'[]'
    routes = observe(runner)["routes"]
    assert routes["completeness"] == "partial"
    assert len(routes["value"]) == 2 and len(routes["gaps"]) == 2
