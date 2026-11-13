"""Reserved provider rows must not become primary interface observations."""
from datetime import datetime, timezone
from types import SimpleNamespace
import json
import pytest

from localplane.backend.agent_client import AgentError
from localplane.backend.connectivity import refresh, snapshot
from localplane.backend.db.database import open_database
from localplane.backend.db.repositories import AgentRepository, HostRepository, ObjectRepository, ProviderObservationRepository, SweepRepository
from localplane.backend.domain.identity import identify_interface
from localplane.agent.providers.connectivity_collector import collect
from localplane.protocol.diagnostic_policy import LoadedDiagnosticPolicy, parse_diagnostic_policy
from localplane.backend.config import Settings
from tests.conftest import AuthenticatedTestClient, create_authenticated_app


def fixture_context(tmp_path):
    db = open_database(tmp_path / "fixture.db")
    hosts, objects, sweeps = HostRepository(db), ObjectRepository(db), SweepRepository(db)
    agents, providers = AgentRepository(db), ProviderObservationRepository(db)
    now = datetime.now(timezone.utc).isoformat()
    boot = "11111111-1111-4111-8111-111111111111"
    hosts.upsert({"host_id": "host", "identity_basis": "machine_id", "identity_confidence": "high", "boot_id": boot}, now)
    agents.upsert("host", {"agent_instance_id": "agent", "agent_version": "1", "transport": "af_unix", "process_isolated": True,
                           "privilege": "unprivileged", "started_at": now}, "1", now)
    identity = identify_interface("host", "eth0", None, None, None)
    objects.upsert(identity.object_id, "host", "network.interface", identity.basis, identity.value, identity.confidence,
                   "eth0", "observed", "fixture", now)
    sweeps.insert({"sweep_id": "inventory", "host_id": "host", "agent_instance_id": "agent", "capability": "network.observe",
                   "provider": "fixture", "provider_version": "1", "status": "ok", "scope": "inventory",
                   "started_at": now, "completed_at": now, "received_at": now, "object_count": 1})
    sweeps.insert_observation({"observation_id": "primary", "sweep_id": "inventory", "host_id": "host", "object_id": identity.object_id,
        "capability": "network.observe", "provider": "fixture", "provider_version": "1", "method": "fixture",
        "fidelity": "complete", "observed_at": now, "received_at": now, "health_state": "unknown", "health_reason": "fixture",
        "facts": {"ifindex": 2, "name": "eth0", "kind": "ethernet", "admin_up": True, "mtu": 1500, "operstate": "up", "carrier": True}, "evidence": {"primary": True}})
    ingestor = SimpleNamespace(hosts=hosts, objects=objects, sweeps=sweeps, provider_readings=providers)
    ctx = SimpleNamespace(database=db, ingestor=ingestor, settings=SimpleNamespace(freshness_ttl_s=60), client=SimpleNamespace())
    return ctx, identity.object_id, boot


def agent_answer(boot, index=2, name="eth0", host_id="host", agent_id="agent"):
    identity = {"ifindex": index, "name": name, "mac_address": None, "mac_is_permanent": None, "device_path": None,
                "kernel_device": "1:2", "sysfs_device": "/sys/devices/virtual/net/" + name, "boot_id": boot, "network_namespace": "net:[1]"}
    unavailable_nm = {"status": "unavailable", "version": None, "profile": None, "applied": None, "gaps": ["fixture unavailable"]}
    result = collect(index, identity_reader=lambda _: identity, address_reader=lambda _: {"addresses": []},
        inventory_reader=lambda: {k: {"value": [], "completeness": "complete", "gaps": []} for k in ("routes", "rules")},
        nm_reader=lambda _: unavailable_nm,
        resolver_reader=lambda *_: {"status": "unavailable", "value": None, "version": None, "gaps": ["fixture unavailable"]},
        client_reader=lambda: {"client_nameservers": [], "client_mode": "unknown"})
    for section in result["sections"].values():
        section["evidence"]["host_id"] = host_id
        section["evidence"]["agent_instance_id"] = agent_id
    return {"host_id": host_id, "agent_instance_id": agent_id, "observation": result}


def test_reserved_store_preserves_primary_evidence_and_pure_reread(tmp_path):
    from localplane.backend.provenance import ProvenanceService
    ctx, object_id, boot = fixture_context(tmp_path)
    try:
        policy = LoadedDiagnosticPolicy("missing", parse_diagnostic_policy(b'{"version":1,"dns":[],"tcp":[]}'))
        calls = []
        ctx.client.observe_connectivity = lambda index: calls.append(index) or agent_answer(boot)
        record = ctx.ingestor.objects.get(object_id)
        provenance = ProvenanceService(ctx.database)
        owner_before = provenance.for_object(record)
        eligibility_before = provenance.eligibility(record, owner_before)
        def counts():
            return {name: ctx.database.query_one(f"SELECT COUNT(*) AS n FROM {name}")["n"] for name in ("objects", "observations", "findings", "runs", "changes")}
        counts_before = counts()
        result = refresh(ctx, record, policy)
        assert calls == [2] and result.kernel.value.addresses == []
        assert result.profile.evidence.status == "unavailable"
        assert ctx.ingestor.objects.get(object_id).observation.observation_id == "primary"
        assert ctx.ingestor.objects.evidence(object_id) == ("primary", {"primary": True})
        assert ctx.ingestor.sweeps.object_ids("inventory") == {object_id}
        assert ctx.ingestor.provider_readings.latest("host") == []
        after_record = ctx.ingestor.objects.get(object_id)
        owner_after = provenance.for_object(after_record)
        assert owner_after == owner_before
        assert provenance.eligibility(after_record, owner_after) == eligibility_before
        assert counts() == counts_before
        assert snapshot(ctx, record, policy).kernel.value.addresses == [] and calls == [2]
        assert refresh(ctx, record, policy).kernel.value.addresses == []
        assert calls == [2, 2]
        row = ctx.ingestor.provider_readings.latest_connectivity("host", object_id)
        assert row["status"] == "ok"
        # An older reader without the namespace filter can see this real reading ID in
        # metadata, even though primary observations remain unchanged.
        legacy = ctx.database.query("SELECT provider_observation_id FROM provider_observations WHERE host_id = ? AND provider = ?", ("host", "linux.connectivity"))
        assert len(legacy) == 2 and row["provider_observation_id"] in {r["provider_observation_id"] for r in legacy}
        # Corrupt the newest reserved payload: generic provenance must not deserialize it,
        # and the dedicated reader must not fall back to any earlier success.
        ctx.database.execute("UPDATE provider_observations SET records = ? WHERE provider_observation_id = ?", ("{bad", row["provider_observation_id"]))
        assert ctx.ingestor.provider_readings.latest("host") == []
        assert snapshot(ctx, record, policy).kernel.evidence.status == "unavailable"
    finally:
        ctx.database.close()


def test_latest_failure_does_not_replay_old_success_and_retry_is_explicit(tmp_path):
    ctx, object_id, boot = fixture_context(tmp_path)
    try:
        policy = LoadedDiagnosticPolicy("missing", parse_diagnostic_policy(b'{"version":1,"dns":[],"tcp":[]}'))
        record = ctx.ingestor.objects.get(object_id)
        ctx.client.observe_connectivity = lambda _: agent_answer(boot)
        assert refresh(ctx, record, policy).kernel.value.addresses == []
        def timeout(_):
            raise AgentError("timeout", "fixture", dispatched=True)
        ctx.client.observe_connectivity = timeout
        failed = refresh(ctx, record, policy)
        assert failed.kernel.value is None and failed.kernel.evidence.status == "unavailable"
        assert snapshot(ctx, record, policy).kernel.value is None
        assert ctx.database.query_one("SELECT COUNT(*) AS n FROM provider_observations WHERE provider = ?", ("linux.connectivity",))["n"] == 2
        assert ctx.ingestor.objects.get(object_id).observation.observation_id == "primary"
    finally:
        ctx.database.close()


def test_atomic_store_failure_rolls_back_sweep_and_reading(tmp_path, monkeypatch):
    ctx, object_id, boot = fixture_context(tmp_path)
    try:
        policy = LoadedDiagnosticPolicy("missing", parse_diagnostic_policy(b'{"version":1,"dns":[],"tcp":[]}'))
        ctx.client.observe_connectivity = lambda _: agent_answer(boot)
        def fail(_): raise RuntimeError("fixture insertion failure")
        monkeypatch.setattr(ctx.ingestor.provider_readings, "insert", fail)
        with pytest.raises(RuntimeError):
            refresh(ctx, ctx.ingestor.objects.get(object_id), policy)
        assert ctx.database.query_one("SELECT COUNT(*) AS n FROM observation_sweeps WHERE capability = ?", ("network.connectivity.observe",))["n"] == 0
        assert ctx.ingestor.objects.get(object_id).observation.observation_id == "primary"
    finally:
        ctx.database.close()


def test_namespace_accessor_rejects_wrong_host_and_uses_rowid_ties(tmp_path):
    ctx, object_id, boot = fixture_context(tmp_path)
    try:
        policy = LoadedDiagnosticPolicy("missing", parse_diagnostic_policy(b'{"version":1,"dns":[],"tcp":[]}'))
        ctx.client.observe_connectivity = lambda _: agent_answer(boot)
        record = ctx.ingestor.objects.get(object_id)
        refresh(ctx, record, policy)
        first = ctx.ingestor.provider_readings.latest_connectivity("host", object_id)
        assert ctx.ingestor.provider_readings.latest_connectivity("other-host", object_id) is None
        assert ctx.ingestor.provider_readings.latest_connectivity("host", "other-object") is None
        timestamp = first["observed_at"]
        ctx.ingestor.sweeps.insert({"sweep_id": "tie", "host_id": "host", "capability": "network.connectivity.observe", "scope": "targeted",
            "provider": "linux.connectivity", "provider_version": "1", "status": "failed", "started_at": timestamp,
            "completed_at": timestamp, "received_at": timestamp, "object_count": 0})
        ctx.ingestor.provider_readings.insert({"provider_observation_id": "tie", "sweep_id": "tie", "host_id": "host",
            "provider": "linux.connectivity", "source": "network.connectivity.interface:" + object_id, "status": "unavailable",
            "reason": "fixture", "method": "fixture", "observed_at": timestamp, "received_at": timestamp, "records": []})
        assert ctx.ingestor.provider_readings.latest_connectivity("host", object_id)["provider_observation_id"] == "tie"
        assert snapshot(ctx, record, policy).kernel.value is None
    finally:
        ctx.database.close()


def test_newest_spoofed_sweep_is_not_hidden_by_older_success(tmp_path):
    ctx, object_id, boot = fixture_context(tmp_path)
    try:
        policy = LoadedDiagnosticPolicy("missing", parse_diagnostic_policy(b'{"version":1,"dns":[],"tcp":[]}'))
        ctx.client.observe_connectivity = lambda _: agent_answer(boot)
        record = ctx.ingestor.objects.get(object_id)
        assert refresh(ctx, record, policy).kernel.value.addresses == []
        # Deliberately attach a newer exact-source record to the wrong capability/scope.
        timestamp = "9999-12-31T00:00:00+00:00"
        ctx.ingestor.sweeps.insert({"sweep_id": "spoof", "host_id": "host", "capability": "network.observe", "scope": "inventory",
            "provider": "fixture", "provider_version": "1", "status": "ok", "started_at": timestamp,
            "completed_at": timestamp, "received_at": timestamp, "object_count": 0})
        ctx.ingestor.provider_readings.insert({"provider_observation_id": "spoof", "sweep_id": "spoof", "host_id": "host",
            "provider": "linux.connectivity", "source": "network.connectivity.interface:" + object_id, "status": "ok",
            "method": "fixture", "observed_at": timestamp, "received_at": timestamp, "records": []})
        assert ctx.ingestor.provider_readings.latest_connectivity("host", object_id)["provider_observation_id"] == "spoof"
        assert snapshot(ctx, record, policy).kernel.evidence.status == "unavailable"
    finally:
        ctx.database.close()


def test_http_observe_then_pure_get_survives_backend_restart(tmp_path, monkeypatch):
    ctx, object_id, boot = fixture_context(tmp_path)
    policy = LoadedDiagnosticPolicy("missing", parse_diagnostic_policy(b'{"version":1,"dns":[],"tcp":[]}'))
    monkeypatch.setattr("localplane.backend.app.load_diagnostic_policy", lambda: policy)
    settings = Settings(tmp_path / "fixture.db", tmp_path / "absent.sock", 1, 60, "WARNING", False)
    calls = []
    base = f"/api/v1/network/interfaces/{object_id}/connectivity"
    original_closed = False
    try:
        app = create_authenticated_app(settings, ctx.database)
        with AuthenticatedTestClient(app) as client:
            monkeypatch.setattr(app.state.context.client, "observe_connectivity", lambda index: calls.append(index) or agent_answer(boot))
            protection_path = f"/api/v1/network/interfaces/{object_id}/protection"
            before_protection = client.get(protection_path).json()
            assert client.get(base).json()["kernel"]["evidence"]["status"] == "not_observed"
            observed = client.post(base + "/observations/refresh")
            assert observed.status_code == 200 and observed.json()["kernel"]["value"]["addresses"] == []
            after_protection = client.get(protection_path).json()
            for field in ("status", "reasons", "unresolved", "management_path", "reason", "missing_evidence", "assessed"):
                assert after_protection[field] == before_protection[field]
            assert client.get(base).json()["kernel"]["value"]["addresses"] == [] and calls == [2]
        ctx.database.close()
        original_closed = True
        reopened = open_database(settings.database_path)
        try:
            app2 = create_authenticated_app(settings, reopened)
            with AuthenticatedTestClient(app2) as client:
                assert client.get(base).json()["kernel"]["value"]["addresses"] == []
                assert calls == [2]
        finally:
            reopened.close()
    finally:
        if not original_closed:
            ctx.database.close()


def test_history_over_256_still_allows_refresh_without_deleting_evidence(tmp_path):
    ctx, object_id, boot = fixture_context(tmp_path)
    try:
        policy = LoadedDiagnosticPolicy("missing", parse_diagnostic_policy(b'{"version":1,"dns":[],"tcp":[]}'))
        now = datetime.now(timezone.utc).isoformat()
        with ctx.database.transaction():
            for i in range(257):
                sweep_id = f"history{i}"
                ctx.ingestor.sweeps.insert({"sweep_id": sweep_id, "host_id": "host", "capability": "network.connectivity.observe", "scope": "targeted",
                    "provider": "linux.connectivity", "provider_version": "1", "status": "failed", "started_at": now,
                    "completed_at": now, "received_at": now, "object_count": 0})
                ctx.ingestor.provider_readings.insert({"provider_observation_id": sweep_id, "sweep_id": sweep_id, "host_id": "host",
                    "provider": "linux.connectivity", "source": "network.connectivity.interface:" + object_id, "status": "unavailable",
                    "reason": "fixture", "method": "fixture", "observed_at": now, "received_at": now, "records": []})
        calls = []
        ctx.client.observe_connectivity = lambda index: calls.append(index) or agent_answer(boot)
        assert refresh(ctx, ctx.ingestor.objects.get(object_id), policy).kernel.value.addresses == []
        assert calls == [2]
        assert ctx.database.query_one("SELECT COUNT(*) AS n FROM provider_observations WHERE provider = ?", ("linux.connectivity",))["n"] == 258
    finally:
        ctx.database.close()


def test_two_objects_are_isolated_and_legacy_metadata_collapses_namespace(tmp_path):
    from localplane.backend.domain.provenance import ProviderEvidence
    ctx, first_id, boot = fixture_context(tmp_path)
    try:
        policy = LoadedDiagnosticPolicy("missing", parse_diagnostic_policy(b'{"version":1,"dns":[],"tcp":[]}'))
        other = identify_interface("host", "eth1", None, None, None)
        now = datetime.now(timezone.utc).isoformat()
        ctx.ingestor.objects.upsert(other.object_id, "host", "network.interface", other.basis, other.value, other.confidence,
                                    "eth1", "observed", "fixture", now)
        ctx.ingestor.sweeps.insert_observation({"observation_id": "primary2", "sweep_id": "inventory", "host_id": "host", "object_id": other.object_id,
            "capability": "network.observe", "provider": "fixture", "provider_version": "1", "method": "fixture",
            "fidelity": "complete", "observed_at": now, "received_at": now, "health_state": "unknown", "health_reason": "fixture",
            "facts": {"ifindex": 3, "name": "eth1"}})
        ctx.client.observe_connectivity = lambda index: agent_answer(boot, index, "eth0" if index == 2 else "eth1")
        first = ctx.ingestor.objects.get(first_id)
        second = ctx.ingestor.objects.get(other.object_id)
        assert refresh(ctx, first, policy).kernel.value.addresses == []
        assert refresh(ctx, second, policy).kernel.value.addresses == []
        assert ctx.ingestor.objects.get(first_id).observation.observation_id == "primary"
        assert ctx.ingestor.objects.get(other.object_id).observation.observation_id == "primary2"
        assert ctx.ingestor.provider_readings.latest("host") == []
        # Simulate the pre-namespace-filter SQL and existing provider-key reduction.
        rows = ctx.database.query("SELECT * FROM provider_observations WHERE host_id = ? ORDER BY provider", ("host",))
        legacy = ProviderEvidence.of(ctx.ingestor.provider_readings._to_view(row) for row in rows)
        assert len(rows) == 2 and len(legacy.readings) == 1
        assert legacy.readings["linux.connectivity"].provider_observation_id in {r["provider_observation_id"] for r in rows}
    finally:
        ctx.database.close()


def test_two_hosts_with_same_interface_name_remain_separate(tmp_path):
    ctx, first_id, boot = fixture_context(tmp_path)
    try:
        policy = LoadedDiagnosticPolicy("missing", parse_diagnostic_policy(b'{"version":1,"dns":[],"tcp":[]}'))
        now = datetime.now(timezone.utc).isoformat()
        ctx.ingestor.hosts.upsert({"host_id": "host2", "identity_basis": "machine_id", "identity_confidence": "high", "boot_id": boot}, now)
        AgentRepository(ctx.database).upsert("host2", {"agent_instance_id": "agent2", "agent_version": "1", "transport": "af_unix", "process_isolated": True,
            "privilege": "unprivileged", "started_at": now}, "1", now)
        other = identify_interface("host2", "eth0", None, None, None)
        ctx.ingestor.objects.upsert(other.object_id, "host2", "network.interface", other.basis, other.value, other.confidence, "eth0", "observed", "fixture", now)
        ctx.ingestor.sweeps.insert({"sweep_id": "inventory2", "host_id": "host2", "agent_instance_id": "agent2", "capability": "network.observe",
            "scope": "inventory", "provider": "fixture", "provider_version": "1", "status": "ok", "started_at": now,
            "completed_at": now, "received_at": now, "object_count": 1})
        ctx.ingestor.sweeps.insert_observation({"observation_id": "host2primary", "sweep_id": "inventory2", "host_id": "host2", "object_id": other.object_id,
            "capability": "network.observe", "provider": "fixture", "provider_version": "1", "method": "fixture", "fidelity": "complete",
            "observed_at": now, "received_at": now, "health_state": "unknown", "health_reason": "fixture", "facts": {"ifindex": 2, "name": "eth0"}})
        ctx.client.observe_connectivity = lambda _: agent_answer(boot, host_id="host", agent_id="agent")
        assert refresh(ctx, ctx.ingestor.objects.get(first_id), policy).kernel.value.addresses == []
        ctx.client.observe_connectivity = lambda _: agent_answer(boot, host_id="host2", agent_id="agent2")
        assert refresh(ctx, ctx.ingestor.objects.get(other.object_id), policy).kernel.value.addresses == []
        assert ctx.ingestor.provider_readings.latest_connectivity("host", other.object_id) is None
        assert ctx.ingestor.provider_readings.latest_connectivity("host2", first_id) is None
        assert ctx.ingestor.objects.get(first_id).observation.observation_id == "primary"
        assert ctx.ingestor.objects.get(other.object_id).observation.observation_id == "host2primary"
    finally:
        ctx.database.close()


def test_dns_http_independent_admission_and_agent_execution_fixture_only(tmp_path, monkeypatch):
    from tests.connectivity_diagnostic_fixtures import diagnostic_rig
    rig = diagnostic_rig(tmp_path, monkeypatch, document={"version": 1, "dns": ["nas.lab.test."], "tcp": [{"address": "192.168.247.2", "port": 443}]})
    try:
        with rig.client as client:
            rig.connect_client()
            dns, path = rig.body("dns"), rig.path
            assert client.post(path, json=dns | {"policy_digest": "0" * 64}).json()["outcome"] == "policy_mismatch"
            assert not rig.queries
            result = client.post(path, json=dns).json()
            assert result["outcome"] == "resolved" and result["answers"] == ["192.168.247.2"] and len(rig.queries) == 1
            for _ in range(5):
                assert client.post(path, json=dns).json()["outcome"] == "resolved"
            assert client.post(path, json=dns).json()["outcome"] == "rate_limited" and len(rig.queries) == 6
            assert client.post(path, json=rig.body()).json()["outcome"] == "unavailable" and len(rig.queries) == 6
            assert client.post("/api/v1/session").status_code == 200
            client.headers.pop("authorization")
            assert client.post(path, json=dns).status_code == 403
            assert client.post(path, json=dns, headers={"Origin": "https://untrusted.invalid"}).status_code == 403
            client.cookies.clear()
            assert client.post(path, json=dns).status_code == 401
            assert len(rig.queries) == 6 and not rig.sockets
    finally:
        rig.ctx.database.close()


def test_dns_transport_uncertainty_keeps_backend_slot_held(tmp_path, monkeypatch):
    from tests.connectivity_diagnostic_fixtures import diagnostic_rig
    rig = diagnostic_rig(tmp_path, monkeypatch)
    calls = []
    def timeout(*args, **kwargs):
        calls.append(1)
        raise AgentError("timeout", "fixture", dispatched=True)
    try:
        with rig.client as client:
            rig.connect_client()
            rig.fail_execute = timeout
            assert client.post(rig.path, json=rig.body("dns")).json()["outcome"] == "unavailable"
            assert client.post(rig.path, json=rig.body("dns")).json()["outcome"] == "busy"
            assert calls == [1]
    finally:
        rig.ctx.database.close()


def test_dns_missing_socket_before_dispatch_releases_for_explicit_retry(tmp_path, monkeypatch):
    from tests.connectivity_diagnostic_fixtures import diagnostic_rig
    rig = diagnostic_rig(tmp_path, monkeypatch)
    calls = []
    def missing(*args, **kwargs):
        calls.append(1)
        raise AgentError("agent_unavailable", "fixture", dispatched=False) from FileNotFoundError("fixture")
    try:
        with rig.client as client:
            rig.connect_client()
            monkeypatch.setattr(rig.app.state.context.client, "diagnostic_preflight", missing)
            assert client.post(rig.path, json=rig.body("dns")).json()["outcome"] == "unavailable"
            assert client.post(rig.path, json=rig.body("dns")).json()["outcome"] == "unavailable"
            assert calls == [1, 1]
            assert not rig.queries and not rig.sockets
    finally:
        rig.ctx.database.close()


def test_dns_partial_write_uncertainty_is_not_treated_as_pre_dispatch(tmp_path, monkeypatch):
    from tests.connectivity_diagnostic_fixtures import diagnostic_rig
    rig = diagnostic_rig(tmp_path, monkeypatch)
    calls = []
    def ambiguous(*args, **kwargs):
        calls.append(1)
        raise AgentError("agent_unavailable", "fixture", dispatched=False) from BrokenPipeError("fixture")
    try:
        with rig.client as client:
            rig.connect_client()
            rig.fail_execute = ambiguous
            assert client.post(rig.path, json=rig.body("dns")).json()["outcome"] == "unavailable"
            assert client.post(rig.path, json=rig.body("dns")).json()["outcome"] == "busy"
            assert calls == [1]
    finally:
        rig.ctx.database.close()


def test_populated_prior_readers_preserve_actual_plan_and_published_rows(tmp_path):
    """Exercise real adoption, intent and planning; all provider evidence is synthetic."""
    from localplane.backend.domain.protection import ManagementPathVerdict
    from localplane.backend.domain.runs import OperationType, canonical_plan, plan_digest
    from localplane.backend.management import ManagementService
    from localplane.backend.operations import OPERATIONS
    from localplane.backend.provenance import ProvenanceService
    from localplane.backend.runs import RunService
    from localplane.backend.domain.provenance import ProviderEvidence
    ctx, object_id, boot = fixture_context(tmp_path)
    try:
        now = datetime.now(timezone.utc).isoformat()
        for provider, source in (("docker", "docker.networks"), ("networkmanager", "networkmanager.devices"), ("tailscale", "tailscale.status")):
            ctx.ingestor.provider_readings.insert({"provider_observation_id": "old_" + provider, "sweep_id": "inventory", "host_id": "host",
                "provider": provider, "source": source, "status": "absent", "reason": "fixture", "method": "fixture",
                "observed_at": now, "received_at": now, "records": []})
        provenance = ProvenanceService(ctx.database)
        management = ManagementService(ctx.database, freshness_ttl_s=60, provenance=provenance)
        management.adopt(ctx.ingestor.objects.get(object_id))
        later = datetime.now(timezone.utc).isoformat()
        ctx.ingestor.sweeps.insert({"sweep_id": "drift", "host_id": "host", "agent_instance_id": "agent", "capability": "network.observe",
            "provider": "fixture", "provider_version": "1", "status": "ok", "scope": "inventory", "started_at": later,
            "completed_at": later, "received_at": later, "object_count": 1})
        ctx.ingestor.sweeps.insert_observation({"observation_id": "drift-observation", "sweep_id": "drift", "host_id": "host", "object_id": object_id,
            "capability": "network.observe", "provider": "fixture", "provider_version": "1", "method": "fixture", "fidelity": "complete",
            "observed_at": later, "received_at": later, "health_state": "unknown", "health_reason": "fixture",
            "facts": {"ifindex": 2, "name": "eth0", "kind": "ethernet", "admin_up": True, "mtu": 1400, "operstate": "up", "carrier": True}})
        other = identify_interface("host", "eth1", None, None, None)
        ctx.ingestor.objects.upsert(other.object_id, "host", "network.interface", other.basis, other.value, other.confidence,
                                   "eth1", "observed", "fixture", later)
        ctx.ingestor.sweeps.insert_observation({"observation_id": "other-observation", "sweep_id": "drift", "host_id": "host", "object_id": other.object_id,
            "capability": "network.observe", "provider": "fixture", "provider_version": "1", "method": "fixture", "fidelity": "complete",
            "observed_at": later, "received_at": later, "health_state": "unknown", "health_reason": "fixture",
            "facts": {"ifindex": 3, "name": "eth1", "kind": "ethernet", "admin_up": True, "mtu": 1500, "operstate": "up", "carrier": True}})
        class PriorReader(ProvenanceService):
            def evidence(self, host_id):
                # Exact baseline repository latest() SQL from HEAD repositories.py:1244;
                # baseline file SHA256 3017b2f033a4c67edc82df60dbb73fbf4e0044c696d5cb78b583752abe59a6f6.
                rows = self.readings._db.query("""
                    SELECT o.* FROM provider_observations o
                    WHERE o.host_id = ?
                      AND o.provider_observation_id = (
                          SELECT provider_observation_id FROM provider_observations
                          WHERE host_id = o.host_id AND provider = o.provider AND source = o.source
                          ORDER BY observed_at DESC, rowid DESC LIMIT 1
                      ) ORDER BY o.provider
                """, (host_id,))
                return ProviderEvidence.of(self.readings._to_view(row) for row in rows)
        runs = RunService(ctx.database, 60, OPERATIONS, provenance)
        old_runs = RunService(ctx.database, 60, OPERATIONS, PriorReader(ctx.database))
        path = ManagementPathVerdict(resource_id=None, reason="management_path_unobserved", missing_evidence=("route.observe", "session.peer"))
        before = runs.create(OperationType.NETWORK_INTERFACE_RECONCILE_MTU, ctx.ingestor.objects.get(object_id), path)
        old_before = old_runs.create(OperationType.NETWORK_INTERFACE_RECONCILE_MTU, ctx.ingestor.objects.get(object_id), path)
        before_plan, before_digest = canonical_plan(before.plan), plan_digest(before.plan)
        prior_runs = [tuple(row) for row in ctx.database.query("SELECT * FROM runs ORDER BY run_id")]
        prior_previews = [tuple(row) for row in ctx.database.query("SELECT * FROM run_previews ORDER BY preview_id")]
        policy = LoadedDiagnosticPolicy("missing", parse_diagnostic_policy(b'{"version":1,"dns":[],"tcp":[]}'))
        ctx.client.observe_connectivity = lambda index: agent_answer(boot, index, "eth0" if index == 2 else "eth1")
        assert refresh(ctx, ctx.ingestor.objects.get(object_id), policy).kernel.value.addresses == []
        assert refresh(ctx, ctx.ingestor.objects.get(other.object_id), policy).kernel.value.addresses == []
        assert [tuple(row) for row in ctx.database.query("SELECT * FROM runs ORDER BY run_id")] == prior_runs
        assert [tuple(row) for row in ctx.database.query("SELECT * FROM run_previews ORDER BY preview_id")] == prior_previews
        after = runs.create(OperationType.NETWORK_INTERFACE_RECONCILE_MTU, ctx.ingestor.objects.get(object_id), path)
        assert canonical_plan(after.plan) == before_plan and plan_digest(after.plan) == before_digest
        assert after.plan.protection == before.plan.protection
        old_after = old_runs.create(OperationType.NETWORK_INTERFACE_RECONCILE_MTU, ctx.ingestor.objects.get(object_id), path)
        assert canonical_plan(old_after.plan) == canonical_plan(old_before.plan)
        assert plan_digest(old_after.plan) == plan_digest(old_before.plan)
        assert old_after.plan.protection == old_before.plan.protection
        assert set(old_after.plan.ownership.readings) - set(old_before.plan.ownership.readings) == {"linux.connectivity"}
        added = old_after.plan.ownership.readings["linux.connectivity"]
        reserved_ids = {row["provider_observation_id"] for row in ctx.database.query("SELECT provider_observation_id FROM provider_observations WHERE provider = ?", ("linux.connectivity",))}
        assert len(reserved_ids) == 2 and added in reserved_ids
    finally:
        ctx.database.close()


def test_maintenance_fence_refuses_dns_before_agent_dispatch(tmp_path, monkeypatch):
    from localplane.maintenance import MaintenanceController
    ctx, object_id, _boot = fixture_context(tmp_path)
    policy = parse_diagnostic_policy(b'{"version":1,"dns":["nas.lab.test."],"tcp":[]}')
    monkeypatch.setattr("localplane.backend.app.load_diagnostic_policy", lambda: LoadedDiagnosticPolicy("valid", policy))
    root = tmp_path / "maintenance"
    controller = MaintenanceController(root, service="fixture")
    controller.initialize()
    controller.open_window()
    settings = Settings(tmp_path / "fixture.db", tmp_path / "absent.sock", 1, 60, "WARNING", False, maintenance_dir=root)
    app = create_authenticated_app(settings, ctx.database)
    calls = []
    try:
        with AuthenticatedTestClient(app) as client:
            monkeypatch.setattr(app.state.context.client, "diagnostic_preflight", lambda *a, **kw: calls.append("prepare"))
            monkeypatch.setattr(app.state.context.client, "diagnostic_execute", lambda *a, **kw: calls.append("execute"))
            body = {"kind": "dns", "name": "nas.lab.test.", "record_type": "A", "policy_digest": policy.digest}
            response = client.post(f"/api/v1/network/interfaces/{object_id}/connectivity/diagnostics", json=body)
            assert response.status_code == 503 and calls == []
    finally:
        ctx.database.close()


def test_agent_identity_spoof_is_failed_without_touching_primary(tmp_path):
    ctx, object_id, boot = fixture_context(tmp_path)
    try:
        policy = LoadedDiagnosticPolicy("missing", parse_diagnostic_policy(b'{"version":1,"dns":[],"tcp":[]}'))
        ctx.client.observe_connectivity = lambda index: agent_answer(boot) | {"host_id": "other"}
        result = refresh(ctx, ctx.ingestor.objects.get(object_id), policy)
        assert result.kernel.evidence.status == "unavailable"
        assert ctx.ingestor.provider_readings.latest_connectivity("host", object_id)["status"] == "unavailable"
        assert ctx.ingestor.objects.get(object_id).observation.observation_id == "primary"
    finally:
        ctx.database.close()
