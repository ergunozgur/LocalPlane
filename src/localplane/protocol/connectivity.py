"""Closed stdlib-only agent request vocabulary; no generic provider or resolver."""
from __future__ import annotations

import ipaddress
import re
import math
import hashlib
import json

from localplane.protocol.diagnostic_policy import _name

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _fields(value: object, keys: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid request fields")
    return value


def _address(value: object) -> str:
    if not isinstance(value, str) or "%" in value:
        raise ValueError("numeric unscoped address required")
    return str(ipaddress.ip_address(value))


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError("invalid policy digest")
    return value


def observe_request(value: object) -> dict:
    value = _fields(value, {"ifindex"})
    if type(value["ifindex"]) is not int or value["ifindex"] < 1:
        raise ValueError("invalid interface index")
    return {"ifindex": value["ifindex"]}


def route_request(value: object) -> dict:
    value = _fields(value, {"destination"})
    return {"destination": _address(value["destination"])}


def dns_request(value: object) -> dict:
    value = _fields(value, {"name", "record_type", "policy_digest", "expected_host_id", "expected_agent_instance_id", "expected_namespace", "expected_clock_namespace", "absolute_monotonic_deadline", "ticket"})
    if value["record_type"] not in ("A", "AAAA"):
        raise ValueError("invalid record type")
    _identities(value)
    _execution_context(value)
    return {**value, "name": _name(value["name"]), "policy_digest": _digest(value["policy_digest"])}


def tcp_request(value: object) -> dict:
    value = _fields(value, {"address", "port", "policy_digest", "expected_host_id", "expected_agent_instance_id", "expected_namespace", "expected_clock_namespace", "absolute_monotonic_deadline", "ticket"})
    if type(value["port"]) is not int or not 1 <= value["port"] <= 65535:
        raise ValueError("invalid port")
    _identities(value)
    _execution_context(value)
    return {**value, "address": _address(value["address"]), "policy_digest": _digest(value["policy_digest"])}


def _identities(value: dict) -> None:
    for key in ("expected_host_id", "expected_agent_instance_id"):
        item = value[key]
        if not isinstance(item, str) or not item or len(item) > 128:
            raise ValueError("invalid expected identity")


def admission_status_request(value: object) -> dict:
    value = _fields(value, {"expected_host_id", "expected_agent_instance_id", "ticket"})
    _identities(value)
    _ticket(value["ticket"])
    return dict(value)


def preflight_request(value: object) -> dict:
    if not isinstance(value, dict) or value.get("kind") not in ("dns", "tcp") or "ticket" in value:
        raise ValueError("invalid diagnostic kind")
    kind = value["kind"]
    parsed = dict(value)
    del parsed["kind"]
    # Preparation grants no execution: a real ticket is issued only after checks.
    parsed["ticket"] = "0" * 32
    result = dns_request(parsed) if kind == "dns" else tcp_request(parsed)
    del result["ticket"]
    return {**result, "kind": kind}


def _ticket(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("invalid ticket")


def _execution_context(value):
    _ticket(value["ticket"])
    for key, prefix in (("expected_namespace", "net"), ("expected_clock_namespace", "time")):
        if type(value[key]) is not str or not re.fullmatch(prefix + r":\[[1-9][0-9]*\]", value[key]):
            raise ValueError("invalid namespace")
    deadline = value["absolute_monotonic_deadline"]
    if type(deadline) not in (float, int):
        raise ValueError("invalid diagnostic deadline")
    try:
        finite = math.isfinite(deadline)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError("invalid diagnostic deadline")


def diagnostic_binding(value: dict) -> str:
    """Bind the issued ticket to the exact closed request, not a caller override."""
    return hashlib.sha256(json.dumps({k: v for k, v in value.items() if k != "ticket"}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def unavailable_response() -> dict:
    return {"status": "unavailable", "detail": "Connectivity operation is not available."}
