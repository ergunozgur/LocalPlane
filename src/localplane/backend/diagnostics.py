"""Closed diagnostic orchestration; passive preparation is never execution.

An issued one-use ticket closes the threaded-IPC ordering gap: an unknown or
expired ticket cannot start later, and only that ticket's terminal status can
resolve an uncertain dispatch. Results remain view-held, not durable history.
"""
from __future__ import annotations

import ipaddress
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, StrictBool, StrictStr, ValidationError

from localplane.agent.providers import diagnostic_exclusions
from localplane.backend.agent_client import AgentError
from localplane.backend.api import connectivity_schemas as schema
from localplane.backend.api.schemas import Model
from localplane.backend import connectivity
from localplane.protocol.diagnostic_policy import TcpTarget, tcp_target_permitted


class Preflight(Model):
    host_id: StrictStr
    agent_instance_id: StrictStr
    boot_id: StrictStr
    network_namespace: StrictStr
    clock_namespace: StrictStr
    ticket: StrictStr = Field(pattern=r"^[0-9a-f]{32}$")
    policy_digest: StrictStr
    observed_at: datetime
    complete: StrictBool
    local_addresses: list[StrictStr] = Field(max_length=4096)
    broadcast_addresses: list[StrictStr] = Field(max_length=4096)


class Status(Model):
    host_id: StrictStr
    agent_instance_id: StrictStr
    ticket: StrictStr
    state: Literal["pending", "completed", "expired", "unknown"]


@dataclass(frozen=True)
class Pending:
    host: str
    agent: str
    ticket: str


class Admission:
    def __init__(self, limiter):
        self.limiter = limiter
        self.lock = threading.Lock()
        self.pending: Pending | None = None

    def recover(self, client, deadline: float) -> bool:
        if not self.lock.acquire(blocking=False):
            return False
        try:
            p = self.pending
            if p is None:
                return True
            try:
                result = Status.model_validate(client.diagnostic_status(expected_host_id=p.host,
                    expected_agent_instance_id=p.agent, ticket=p.ticket,
                    timeout_s=min(1.0, max(0.001, deadline - time.monotonic()))))
                if (result.host_id, result.agent_instance_id, result.ticket) != (p.host, p.agent, p.ticket):
                    return False
                if result.state not in ("completed", "expired"):
                    return False
            except (AgentError, ValueError, TypeError):
                return False
            self.pending = None
            self.limiter.release()
            return True
        finally:
            self.lock.release()


def _result(body, outcome, detail):
    if isinstance(body, schema.DnsRequest):
        return schema.DnsResult(name=body.name, record_type=body.record_type, outcome=outcome, detail=detail)
    return schema.TcpResult(address=body.address, port=body.port, outcome=outcome, detail=detail)


def _validate_preflight(answer, record, inventory, context, digest, kernel, clock_ns, started):
    result = Preflight.model_validate(answer)
    host = context.ingestor.hosts.get(record.host_id)
    if ((result.host_id, result.agent_instance_id, result.policy_digest) != (record.host_id, inventory.agent_instance_id, digest)
            or host is None or not host.boot_id or result.boot_id != host.boot_id
            or (result.boot_id, result.network_namespace) != kernel or result.clock_namespace != clock_ns
            or result.complete is not True or result.observed_at.tzinfo is None
            or not started <= result.observed_at <= datetime.now(timezone.utc)):
        raise ValueError("invalid preflight identity or timing")
    if len(result.local_addresses) + len(result.broadcast_addresses) > 4096:
        raise ValueError("unbounded exclusions")
    for address in (*result.local_addresses, *result.broadcast_addresses):
        if len(address) > 45 or "%" in address or str(ipaddress.ip_address(address)) != address:
            raise ValueError("invalid address")
    if any(ipaddress.ip_address(address).version != 4 for address in result.broadcast_addresses):
        raise ValueError("invalid broadcast")
    return result


def _validate_result(answer, body, p, preflight):
    if (not isinstance(answer, dict) or set(answer) != {"host_id", "agent_instance_id", "ticket", "completion_proven", "result"}
            or (answer["host_id"], answer["agent_instance_id"], answer["ticket"]) != (p.host, p.agent, p.ticket)
            or type(answer["completion_proven"]) is not bool):
        raise ValueError("invalid diagnostic envelope")
    raw = answer["result"]
    fields = schema.DnsResult.model_fields if isinstance(body, schema.DnsRequest) else schema.TcpResult.model_fields
    if not isinstance(raw, dict) or set(raw) != set(fields):
        raise ValueError("incomplete diagnostic result shape")
    if isinstance(body, schema.DnsRequest):
        result = schema.DnsResult.model_validate(raw)
        if (result.name, result.record_type) != (body.name, body.record_type):
            raise ValueError("DNS target mismatch")
        if result.answers is not None:
            if len(result.answers) > 32:
                raise ValueError("unbounded DNS answers")
            for address in result.answers:
                if len(address) > 45 or "%" in address or ipaddress.ip_address(address).version != (4 if body.record_type == "A" else 6):
                    raise ValueError("invalid DNS answer")
        if result.outcome == "resolved":
            if not result.answers or type(result.truncated) is not bool:
                raise ValueError("incomplete DNS evidence")
        elif result.answers:
            raise ValueError("answers without resolution")
        success = result.outcome == "resolved"
    else:
        result = schema.TcpResult.model_validate(raw)
        if (result.address, result.port) != (body.address, body.port):
            raise ValueError("TCP target mismatch")
        for key in ("local", "remote"):
            endpoint = getattr(result, key)
            if endpoint is not None:
                if (not isinstance(raw.get(key), dict) or type(raw[key].get("port")) is not int
                        or not 1 <= endpoint.port <= 65535 or "%" in endpoint.address
                        or ipaddress.ip_address(endpoint.address).version != ipaddress.ip_address(body.address).version):
                    raise ValueError("invalid socket tuple")
        success = result.outcome == "connected"
        if success and (result.local is None or result.remote is None or (result.remote.address, result.remote.port) != (body.address, body.port)):
            raise ValueError("incomplete connected socket evidence")
    if len(result.detail) > 512 or (result.started_at is None) != (result.ended_at is None):
        raise ValueError("invalid diagnostic evidence")
    if result.started_at is not None:
        if (result.started_at.tzinfo is None or result.ended_at.tzinfo is None
                or not preflight.observed_at <= result.started_at <= result.ended_at <= datetime.now(timezone.utc)
                or result.vantage != "agent network namespace " + preflight.network_namespace):
            raise ValueError("invalid execution context or timing")
    elif success or result.vantage is not None:
        raise ValueError("missing execution timing")
    return result, answer["completion_proven"]


def run(context, record, loaded, body, admission: Admission, *, additional_addresses=()):
    start = time.monotonic()
    deadline = start + 5.0
    wall_start = datetime.now(timezone.utc)
    policy = loaded.policy
    dns = isinstance(body, schema.DnsRequest)
    kind = "dns" if dns else "tcp"
    if loaded.status != "valid" or policy is None:
        return _result(body, "denied", "Diagnostic policy is missing, unsafe, invalid or unreadable.")
    if body.policy_digest != policy.digest:
        return _result(body, "policy_mismatch", "Backend diagnostic policy identity differs; reload the snapshot.")
    if dns:
        if body.name not in policy.dns:
            return _result(body, "denied", "DNS target is not independently authorized.")
    else:
        if TcpTarget(body.address, body.port) not in policy.tcp or body.address in policy.control_plane_addresses:
            return _result(body, "denied", "TCP target is not authorized or is control-plane denied.")
        if policy.version != 2 or policy.control_plane_complete is not True:
            return _result(body, "unavailable", "TCP requires a complete administrator-attested v2 control-plane declaration.")
    try:
        current, inventory, _ = connectivity._inventory(context, record)
        kernel = diagnostic_exclusions.kernel_context()
        clock_ns = diagnostic_exclusions.clock_namespace()
    except (ValueError, TypeError, OSError):
        return _result(body, "unavailable", "Current host, inventory or supported namespace is unavailable.")
    if not admission.recover(context.client, deadline):
        return _result(body, "busy", "An earlier request has no request-specific completion proof; no new diagnostic was started.")
    refused = admission.limiter.try_acquire()
    if refused:
        return _result(body, refused, "Diagnostic admission refused; no new diagnostic was started.")
    pending = None
    uncertain = False
    try:
        values = body.model_dump(exclude={"kind"})
        prepared = {**values, "kind": kind, "expected_host_id": record.host_id,
                    "expected_agent_instance_id": inventory.agent_instance_id,
                    "expected_namespace": kernel[1], "expected_clock_namespace": clock_ns,
                    "absolute_monotonic_deadline": deadline}
        answer = context.client.diagnostic_preflight(prepared, timeout_s=max(0.001, deadline - time.monotonic()))
        if isinstance(answer, dict) and set(answer) == {"status"} and answer["status"] in {"unavailable", "denied", "policy_mismatch", "busy"}:
            return _result(body, answer["status"], "Agent preparation refused; no target traffic was started.")
        preflight = _validate_preflight(answer, record, inventory, context, policy.digest, kernel, clock_ns, wall_start)
        pending = Pending(record.host_id, inventory.agent_instance_id, preflight.ticket)
        after, after_inventory, _ = connectivity._inventory(context, record)
        if (current.observation.observation_id != after.observation.observation_id or after_inventory.sweep_id != inventory.sweep_id
                or diagnostic_exclusions.kernel_context() != kernel or diagnostic_exclusions.clock_namespace() != clock_ns):
            raise ValueError("context changed")
        if not dns and not tcp_target_permitted(loaded, body.policy_digest, body.address, body.port,
                local_addresses=tuple(preflight.local_addresses), broadcast_addresses=tuple(preflight.broadcast_addresses),
                management_addresses=tuple(additional_addresses)):
            return _result(body, "denied", "Current local, broadcast or management evidence excludes this TCP target.")
        if time.monotonic() >= deadline:
            return _result(body, "timeout", "Response budget expired before execution; no connection was started.")
        del prepared["kind"]
        prepared["ticket"] = preflight.ticket
        uncertain = True  # A partial write is not a proof that nothing was dispatched.
        answer = context.client.diagnostic_execute(kind, prepared, timeout_s=max(0.001, deadline - time.monotonic()))
        result, completed = _validate_result(answer, body, pending, preflight)
        uncertain = not completed
        if time.monotonic() >= deadline:
            updates = {"outcome": "timeout", "detail": "The response budget expired; this does not prove provider-internal traffic stopped."}
            if dns:
                updates.update(answers=None, truncated=None)
            return result.model_copy(update=updates)
        return result
    except AgentError as exc:
        if not exc.dispatched and isinstance(exc.__cause__, FileNotFoundError):
            uncertain = False
        return _result(body, "unavailable", "Agent response unavailable; an execution already dispatched may continue. Retry is explicit only.")
    except (ValueError, TypeError, OSError, ValidationError):
        return _result(body, "error", "Diagnostic evidence is unavailable, changed or malformed; no successful outcome is inferred.")
    finally:
        if uncertain and pending is not None:
            with admission.lock:
                admission.pending = pending
        else:
            admission.limiter.release()
