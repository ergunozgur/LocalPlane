"""Targeted connectivity evidence in the existing isolated provider namespace."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone

from pydantic import ValidationError
from fastapi import HTTPException

from localplane.backend.agent_client import AgentError
from localplane.backend.api import connectivity_schemas as schema
from localplane.backend.domain.identity import IdentityBasis, OBJECT_KIND_NETWORK_INTERFACE, derive_object_id, identify_interface
from localplane.backend.domain.states import Freshness, derive_freshness
from localplane.protocol.capabilities import CAPABILITY_NETWORK_OBSERVE

PROVIDER = "linux.connectivity"
CAPABILITY = "network.connectivity.observe"
MAX_BYTES = 1024 * 1024
_COLLECTION_SLOT = threading.Lock()
UNCERTAIN = "Agent observation did not complete; it may still be running. Read the stored result before an explicit retry."
BEFORE_DISPATCH = "Agent observation was unavailable before dispatch."
SOURCES = {"profile": "NetworkManager", "applied": "NetworkManager", "kernel": "iproute2.address",
           "routes": "iproute2.routes", "rules": "iproute2.rules", "resolver": "systemd-resolved", "client_resolver": "resolv.conf"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _empty(status: str, gap: str) -> dict:
    evidence = schema.ConnectivityEvidence(status=status, gaps=[gap])
    return {
        "profile": schema.ProfileSection(evidence=evidence), "applied": schema.AppliedSection(evidence=evidence),
        "kernel": schema.KernelSection(evidence=evidence), "routes": schema.RoutesSection(evidence=evidence),
        "rules": schema.RulesSection(evidence=evidence), "resolver": schema.ResolverSection(evidence=evidence),
        "client_resolver": schema.ClientResolverSection(evidence=evidence),
    }


def _policy(loaded) -> schema.ConnectivityPolicy:
    policy = loaded.policy
    version = getattr(policy, "version", 1) if policy else None
    complete = getattr(policy, "control_plane_complete", False) if policy and version == 2 else None
    addresses = getattr(policy, "control_plane_addresses", ()) if policy and version == 2 else None
    tcp_status = ("policy_unavailable" if policy is None else "disabled" if not policy.tcp else
                  "missing_declaration" if version != 2 else "incomplete_declaration" if complete is not True else "ready")
    return schema.ConnectivityPolicy(status=loaded.status, digest=policy.digest if policy else None,
        dns=list(policy.dns) if policy else None,
        tcp=[schema.TcpPolicyTarget(address=t.address, port=t.port) for t in policy.tcp] if policy else None,
        version=version, control_plane_complete=complete, address_count=len(addresses) if addresses is not None else None,
        tcp_status=tcp_status)


def _validate_associations(sections: schema.ConnectivitySections, identity: schema.AgentConnectivityIdentity) -> None:
    profile = sections.profile.value
    if profile is not None and profile.device_ifindex != identity.ifindex:
        raise ValueError("Profile device identity does not match the kernel interface.")
    groups = [sections.routes.value]
    if profile is not None:
        groups.append(profile.routes)
    if sections.applied.value is not None:
        groups.append(sections.applied.value.routes)
    for group in groups:
        for route in group or []:
            for hop in route.nexthops or []:
                if hop.object_id is not None:
                    raise ValueError("Unverified route object association.")


def snapshot(context, record, loaded) -> schema.ConnectivitySnapshot:
    row = context.ingestor.provider_readings.latest_connectivity(record.host_id, record.object_id)
    sections = _empty("not_observed", "Connectivity observations have not been collected.")
    if row is not None:
        gap = row["reason"] if row["reason"] in {UNCERTAIN, BEFORE_DISPATCH} else "The latest connectivity observation failed or was malformed."
        sections = _empty("unavailable", gap)
        try:
            latest_time = datetime.fromisoformat(row["observed_at"])
            if latest_time.tzinfo is not None:
                for name, section in sections.items():
                    sections[name] = section.model_copy(update={"evidence": section.evidence.model_copy(update={"observed_at": latest_time, "source": PROVIDER})})
        except (ValueError, TypeError):
            pass
        failure_sections = sections
        try:
            if (row["sweep_host"] != record.host_id or row["sweep_capability"] != CAPABILITY
                or row["sweep_scope"] != "targeted" or row["sweep_object_count"] != 0
                or (row["agent_instance_id"] is not None and row["agent_host"] != record.host_id)):
                raise ValueError("invalid targeted sweep identity")
            if row["status"] != "ok" or row["bounded_records"] is None:
                raise ValueError("latest observation unavailable")
            records = json.loads(row["bounded_records"])
            if not isinstance(records, list) or len(records) != 1:
                raise ValueError("invalid stored observation")
            item = records[0]
            if not isinstance(item, dict) or set(item) != {"object_id", "identity_basis", "identity_value", "identity", "sections"}:
                raise ValueError("invalid stored identity")
            if (item["object_id"], item["identity_basis"], item["identity_value"]) != (record.object_id, record.identity_basis, record.identity_value):
                raise ValueError("stored identity mismatch")
            identity = schema.AgentConnectivityIdentity.model_validate(item["identity"])
            host = context.ingestor.hosts.get(record.host_id)
            derived = identify_interface(record.host_id, identity.name, identity.mac_address, identity.mac_is_permanent, identity.device_path)
            if host is None or not host.boot_id or identity.boot_id != host.boot_id or derived.object_id != record.object_id:
                raise ValueError("stored host or device identity mismatch")
            parsed = schema.ConnectivitySections.model_validate(item["sections"])
            _validate_associations(parsed, identity)
            sections = {}
            for name in schema.ConnectivitySections.model_fields:
                section = getattr(parsed, name)
                e = section.evidence
                if e.host_id != record.host_id or not row["agent_instance_id"] or e.agent_instance_id != row["agent_instance_id"]:
                    raise ValueError("stored agent or host mismatch")
                if e.source != SOURCES[name] or e.vantage != "agent network namespace " + identity.network_namespace:
                    raise ValueError("stored evidence source mismatch")
                if e.observed_at is None or e.observed_at.tzinfo is None:
                    raise ValueError("invalid timestamp")
                if e.status == "observed" and section.value is None:
                    raise ValueError("missing observed value")
                if e.status != "observed" and section.value is not None:
                    raise ValueError("value without observed status")
                fresh, _ = derive_freshness(e.observed_at, context.settings.freshness_ttl_s)
                sections[name] = section.model_copy(update={"evidence": e.model_copy(update={"freshness": "current" if fresh is Freshness.CURRENT else "stale"})})
        except (ValueError, TypeError, ValidationError, json.JSONDecodeError, OverflowError):
            sections = failure_sections
    return schema.ConnectivitySnapshot(object_id=record.object_id, policy=_policy(loaded), **sections)


def _inventory(context, record):
    current = context.ingestor.objects.get(record.object_id)
    if current is None or current.host_id != record.host_id or current.kind != OBJECT_KIND_NETWORK_INTERFACE or current.observation is None:
        raise ValueError("Interface identity is not currently observed.")
    if (current.identity_basis, current.identity_value) != (record.identity_basis, record.identity_value):
        raise ValueError("Interface identity changed.")
    basis = IdentityBasis(current.identity_basis)
    if derive_object_id(current.host_id, current.kind, basis, current.identity_value) != current.object_id:
        raise ValueError("Interface identity is inconsistent.")
    latest = context.ingestor.sweeps.latest(current.host_id, CAPABILITY_NETWORK_OBSERVE, scope="inventory")
    if latest is None or latest.status != "ok" or current.object_id not in context.ingestor.sweeps.object_ids(latest.sweep_id) or current.observation.sweep_id != latest.sweep_id:
        raise ValueError("Current complete interface inventory is required.")
    freshness, _ = derive_freshness(current.observation.observed_at_dt, context.settings.freshness_ttl_s)
    if freshness is not Freshness.CURRENT:
        raise ValueError("Interface inventory is stale.")
    index = current.observation.facts.get("ifindex")
    if type(index) is not int or index <= 0 or not latest.agent_instance_id:
        raise ValueError("Interface index or agent identity is unknown.")
    return current, latest, index


def route_egress(context, answer: schema.AgentRouteAnswer) -> tuple[str | None, str | None]:
    """Current guarded kernel label; object links additionally require durable identity.

    A display name never creates an object relationship. Stale/mismatched inventory,
    namespace, boot, agent or ifindex leaves the association unknown.
    """
    identity, facts = answer.egress_identity, answer.route.route
    if identity is None or facts is None or identity.ifindex != facts.oif_index:
        return None, None
    try:
        from localplane.agent.providers.diagnostic_exclusions import kernel_context
        host = context.ingestor.hosts.get(answer.host_id)
        inventory = context.ingestor.sweeps.latest(answer.host_id, CAPABILITY_NETWORK_OBSERVE, scope="inventory")
        if (host is None or identity.boot_id != host.boot_id or inventory is None or inventory.status != "ok"
                or inventory.agent_instance_id != answer.agent_instance_id
                or kernel_context() != (identity.boot_id, identity.network_namespace)):
            return None, None
        derived = identify_interface(answer.host_id, identity.name, identity.mac_address, identity.mac_is_permanent, identity.device_path)
        record = context.ingestor.objects.get(derived.object_id)
        if record is None:
            return identity.name, None
        current, _, index = _inventory(context, record)
        if index != identity.ifindex:
            return None, None
        if derived.basis is IdentityBasis.KERNEL_NAME:
            return identity.name, None
        if (current.identity_basis, current.identity_value) != (derived.basis, derived.value):
            return None, None
        return identity.name, current.object_id
    except (OSError, ValueError, TypeError):
        return None, None


def refresh(context, record, loaded) -> schema.ConnectivitySnapshot:
    """Do not queue duplicate observations; preserve retained evidence."""
    if not _COLLECTION_SLOT.acquire(blocking=False):
        raise HTTPException(status_code=409, detail={"code": "connectivity_busy", "message": "A connectivity observation is already in progress.", "detail": {}})
    try:
        return _refresh_one(context, record, loaded)
    finally:
        _COLLECTION_SLOT.release()


def _refresh_one(context, record, loaded) -> schema.ConnectivitySnapshot:
    """Perform exactly one passive request; transactionally retain its outcome."""
    started = _now()
    agent_id = None
    payload = None
    failure = "Connectivity observation unavailable."
    try:
        current, inventory, index = _inventory(context, record)
        answer = context.client.observe_connectivity(index)
        encoded = json.dumps(answer, ensure_ascii=True, separators=(",", ":")).encode()
        if len(encoded) > MAX_BYTES:
            raise ValueError("Agent response exceeds the observation bound.")
        parsed = schema.AgentConnectivityResponse.model_validate(answer)
        agent_id = parsed.agent_instance_id
        obs = parsed.observation
        if parsed.host_id != record.host_id or agent_id != inventory.agent_instance_id or obs.status != "observed" or obs.identity is None or obs.sections is None:
            raise ValueError("Agent or observation identity is unavailable or changed.")
        identity = obs.identity
        host = context.ingestor.hosts.get(record.host_id)
        if host is None or not host.boot_id or host.boot_id != identity.boot_id or identity.ifindex != index:
            raise ValueError("Host boot or interface index changed.")
        derived = identify_interface(record.host_id, identity.name, identity.mac_address, identity.mac_is_permanent, identity.device_path)
        if derived.object_id != record.object_id or derived.basis != record.identity_basis or derived.value != record.identity_value:
            raise ValueError("Current kernel identity does not match the selected object.")
        after, after_inventory, after_index = _inventory(context, record)
        if after.observation.observation_id != current.observation.observation_id or after_inventory.sweep_id != inventory.sweep_id or after_index != index:
            raise ValueError("Interface inventory changed during observation.")
        if obs.started_at.tzinfo is None or obs.completed_at.tzinfo is None or obs.completed_at < obs.started_at:
            raise ValueError("Invalid observation timing.")
        _validate_associations(obs.sections, identity)
        for name in schema.ConnectivitySections.model_fields:
            section = getattr(obs.sections, name)
            evidence = section.evidence
            if evidence.host_id != record.host_id or evidence.agent_instance_id != agent_id or evidence.observed_at is None or evidence.observed_at.tzinfo is None or not obs.started_at <= evidence.observed_at <= obs.completed_at:
                raise ValueError("Section provenance or timing is invalid.")
            if evidence.source != SOURCES[name] or evidence.vantage != "agent network namespace " + identity.network_namespace:
                raise ValueError("Section source is invalid.")
            if (evidence.status == "observed") != (section.value is not None):
                raise ValueError("Section status and value disagree.")
        payload = [{"object_id": record.object_id, "identity_basis": record.identity_basis,
                    "identity_value": record.identity_value, "identity": identity.model_dump(mode="json"),
                    "sections": obs.sections.model_dump(mode="json")}]
    except AgentError as exc:
        failure = UNCERTAIN if exc.dispatched else BEFORE_DISPATCH
        agent_id = None
    except (ValueError, TypeError, ValidationError, OverflowError):
        agent_id = None
    completed = _now()
    sweep_id = "sweep_" + uuid.uuid4().hex
    reading_id = "provider_" + uuid.uuid4().hex
    with context.database.transaction():
        if payload is not None:
            try:
                final_record, final_inventory, final_index = _inventory(context, record)
                final_host = context.ingestor.hosts.get(record.host_id)
                if (final_record.observation.observation_id != current.observation.observation_id
                    or final_inventory.sweep_id != inventory.sweep_id or final_index != index
                    or final_host is None or final_host.boot_id != identity.boot_id):
                    raise ValueError("Inventory changed before evidence was stored.")
            except ValueError:
                payload, agent_id = None, None
        context.ingestor.sweeps.insert({"sweep_id": sweep_id, "host_id": record.host_id, "agent_instance_id": agent_id,
            "capability": CAPABILITY, "scope": "targeted", "provider": PROVIDER, "provider_version": "1",
            "status": "ok" if payload is not None else "failed", "started_at": started.isoformat(),
            "completed_at": completed.isoformat(), "received_at": completed.isoformat(), "object_count": 0})
        context.ingestor.provider_readings.insert({"provider_observation_id": reading_id, "sweep_id": sweep_id,
            "host_id": record.host_id, "provider": PROVIDER, "source": "network.connectivity.interface:" + record.object_id,
            "status": "ok" if payload is not None else "unavailable", "reason": None if payload is not None else failure,
            "method": "passive_connectivity", "provider_version": "1", "observed_at": completed.isoformat(),
            "received_at": completed.isoformat(), "records": payload or [], "detail": {}})
    return snapshot(context, record, loaded)
