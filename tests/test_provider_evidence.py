"""Reading the systems that configure this host, and every way that can go wrong.

The Docker provider runs against a real AF_UNIX HTTP server so the transport is exercised
rather than stubbed. The other two run through the command seam, which is how their failure
paths — no binary, a daemon that will not answer, output this build cannot read — are
reachable on every machine instead of only on one that happens to be broken.

What is asserted throughout is the *difference between kinds of silence*. A provider that
is not installed and a provider that refused are different answers, and everything
downstream depends on being able to tell them apart.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from localplane.agent.capabilities import discover_capabilities
from localplane.agent.providers.base import CommandFailure, CommandResult
from localplane.agent.providers.collector import NetworkProviderEvidenceCollector
from localplane.agent.providers.docker import (
    NETWORKS_PATH,
    VERSION_PATH,
    DockerProvider,
)
from localplane.agent.providers.evidence import (
    ProviderEvidenceBatch,
    ProviderReading,
    SweepStatus,
)
from localplane.agent.providers.network_manager import (
    DEVICE_ARGV,
    GENERAL_ARGV,
    NetworkManagerEvidenceProvider,
    split_terse,
)
from localplane.agent.providers.tailscale import STATUS_ARGV, TailscaleEvidenceProvider
from localplane.agent.service import AgentService
from localplane.protocol.capabilities import (
    CAPABILITY_NETWORK_PROVIDERS_OBSERVE,
    CapabilityStatus,
)
from localplane.protocol.providers import ProviderStatus
from localplane.protocol.wire import METHODS, ErrorCode, ProtocolError
from tests.conftest import FakeRunner, docker_network, nmcli_devices, tailscale_status


# --------------------------------------------------------------------------------- docker


def test_docker_networks_are_read_over_the_socket(docker_daemon):
    daemon = docker_daemon(
        [docker_network("net1" + "0" * 60, "monitoring_default", gateway="172.19.0.1",
                        subnet="172.19.0.0/16", compose_project="monitoring")],
        version="27.1.1",
    )
    reading = DockerProvider(daemon.path).read()

    assert reading.status is ProviderStatus.OK
    assert reading.version == "27.1.1"
    assert reading.method == "unix_socket_http"
    record = reading.records[0]
    assert record["network_id"] == "net1" + "0" * 60
    assert record["name"] == "monitoring_default"
    assert record["ipam"] == [{"subnet": "172.19.0.0/16", "gateway": "172.19.0.1"}]
    assert record["compose_project"] == "monitoring"


def test_the_container_map_and_unrelated_labels_are_dropped(docker_daemon):
    """A network's container list is a dump, not evidence of who owns a bridge."""
    daemon = docker_daemon(
        [docker_network("n" * 64, "monitoring_default", gateway="172.19.0.1",
                        compose_project="monitoring")]
    )
    record = DockerProvider(daemon.path).read().records[0]

    assert "Containers" not in record and "Peers" not in record
    assert set(record) == {
        "network_id", "name", "driver", "scope", "created_at",
        "bridge_name", "default_bridge", "ipam", "compose_project", "compose_network",
    }
    assert "omitted" in DockerProvider(daemon.path).read().detail


def test_only_get_is_ever_sent_to_a_docker_socket(docker_daemon):
    daemon = docker_daemon([docker_network("n" * 64, "bridge", bridge_name="docker0")])
    DockerProvider(daemon.path).read()
    assert daemon.requests == [("GET", VERSION_PATH), ("GET", NETWORKS_PATH)]


def test_no_docker_socket_is_absence_not_a_gap(tmp_path: Path):
    reading = DockerProvider(tmp_path / "nothing.sock").read()
    assert reading.status is ProviderStatus.ABSENT
    assert reading.reason == "docker_socket_absent"
    assert reading.records == ()


def test_a_socket_nobody_is_listening_on_is_unavailable(tmp_path: Path):
    dead = tmp_path / "dead.sock"
    dead.write_text("")  # a file where a socket should be: present, not connectable
    reading = DockerProvider(dead).read()
    assert reading.status is ProviderStatus.UNAVAILABLE
    assert reading.records == ()


def test_a_daemon_that_answers_with_an_error_is_an_error(docker_daemon):
    daemon = docker_daemon(responses={
        VERSION_PATH: (200, json.dumps({"Version": "27.1.1"})),
        NETWORKS_PATH: (500, '{"message":"server error"}'),
    })
    reading = DockerProvider(daemon.path).read()
    assert reading.status is ProviderStatus.ERROR
    assert reading.reason == "unexpected_http_status"
    assert reading.detail["http_status"] == 500


def test_a_socket_this_process_may_not_open_is_unavailable(docker_daemon):
    """The commonest real failure: a daemon running as root, and no docker group.

    Unavailable and not absent, because the daemon is plainly there — LocalPlane simply
    cannot see what it holds, and every conclusion that would have rested on it is a gap.
    """
    daemon = docker_daemon([docker_network("n" * 64, "bridge", bridge_name="docker0")])
    os.chmod(daemon.path, 0o000)
    reading = DockerProvider(daemon.path).read()
    assert reading.status is ProviderStatus.UNAVAILABLE
    assert reading.reason == "permission_denied"
    assert reading.records == ()


def test_a_daemon_that_refuses_the_request_yields_no_records(docker_daemon):
    daemon = docker_daemon(responses={
        VERSION_PATH: (200, json.dumps({"Version": "27.1.1"})),
        NETWORKS_PATH: (403, '{"message":"permission denied"}'),
    })
    reading = DockerProvider(daemon.path).read()
    # The daemon answered, so this is not a transport failure — but nothing usable came
    # back, and the reading says so rather than looking like an empty Docker.
    assert reading.status is ProviderStatus.ERROR
    assert reading.records == ()


def test_output_that_is_not_json_is_an_error(docker_daemon):
    daemon = docker_daemon(responses={
        VERSION_PATH: (200, json.dumps({"Version": "27.1.1"})),
        NETWORKS_PATH: (200, "<html>not json</html>"),
    })
    reading = DockerProvider(daemon.path).read()
    assert reading.status is ProviderStatus.ERROR
    assert reading.reason == "unparseable_response"


def test_a_network_without_an_id_is_not_a_record(docker_daemon):
    daemon = docker_daemon(responses={
        VERSION_PATH: (200, json.dumps({"Version": "27.1.1"})),
        NETWORKS_PATH: (200, json.dumps([{"Name": "nameless"}, "not-an-object"])),
    })
    assert DockerProvider(daemon.path).read().records == ()


def test_a_daemon_that_will_not_say_its_version_still_reports_networks(docker_daemon):
    daemon = docker_daemon(responses={
        VERSION_PATH: (200, "{}"),
        NETWORKS_PATH: (200, json.dumps([docker_network("n" * 64, "bridge",
                                                        bridge_name="docker0")])),
    })
    reading = DockerProvider(daemon.path).read()
    assert reading.status is ProviderStatus.OK
    assert reading.version is None  # unknown, never invented
    assert reading.records[0]["bridge_name"] == "docker0"


# -------------------------------------------------------------------------- networkmanager


def test_networkmanager_devices_are_parsed_with_their_posture():
    runner = FakeRunner(nmcli_devices([
        ("enx020000000012", "ethernet", "connected", "service-lan", "uuid-lan"),
        ("docker0", "bridge", "connected (externally)", "docker0", "uuid-docker"),
        ("eth0", "ethernet", "unavailable", "", ""),
        ("tailscale0", "tun", "unmanaged", "", ""),
    ]))
    reading = NetworkManagerEvidenceProvider(runner).read()

    assert reading.status is ProviderStatus.OK
    assert reading.version == "1.46.0"
    by_device = {r["device"]: r for r in reading.records}
    assert by_device["enx020000000012"]["external"] is False
    assert by_device["enx020000000012"]["connection_uuid"] == "uuid-lan"
    # The distinction the whole NetworkManager reading exists for.
    assert by_device["docker0"]["state"] == "connected"
    assert by_device["docker0"]["external"] is True
    assert by_device["docker0"]["state_raw"] == "connected (externally)"
    assert by_device["eth0"]["connection"] is None
    assert by_device["tailscale0"]["state"] == "unmanaged"


def test_a_connection_name_containing_a_colon_survives_parsing():
    runner = FakeRunner(nmcli_devices([("eth0", "ethernet", "connected", "lan:2", "uuid-2")]))
    record = NetworkManagerEvidenceProvider(runner).read().records[0]
    assert record["connection"] == "lan:2"
    assert record["connection_uuid"] == "uuid-2"


def test_terse_splitting_handles_escapes():
    assert split_terse(r"a:b\:c:d") == ["a", "b:c", "d"]
    assert split_terse(r"a\\:b") == ["a\\", "b"]


def test_no_nmcli_is_absence(tmp_path: Path):
    reading = NetworkManagerEvidenceProvider(FakeRunner({})).read()
    assert reading.status is ProviderStatus.ABSENT
    assert reading.reason == "nmcli_absent"
    assert reading.records == ()


def test_nmcli_present_but_the_daemon_is_not_running_is_unavailable():
    runner = FakeRunner({
        GENERAL_ARGV: CommandResult(GENERAL_ARGV, 0, "not running::unknown\n", ""),
    })
    reading = NetworkManagerEvidenceProvider(runner).read()
    assert reading.status is ProviderStatus.UNAVAILABLE
    assert reading.reason == "daemon_not_running"
    # The device table is not even attempted: there is nothing to ask.
    assert DEVICE_ARGV not in runner.calls


def test_an_nmcli_that_fails_is_unavailable_with_the_transcript():
    runner = FakeRunner({
        GENERAL_ARGV: CommandResult(GENERAL_ARGV, 8, "", "error: could not connect"),
    })
    reading = NetworkManagerEvidenceProvider(runner).read()
    assert reading.status is ProviderStatus.UNAVAILABLE
    assert reading.reason == "nmcli_failed"
    assert reading.detail["command"]["returncode"] == 8


def test_an_nmcli_that_times_out_says_so():
    runner = FakeRunner({
        GENERAL_ARGV: CommandResult(
            GENERAL_ARGV, None, "", "", "timed out after 5.0s", CommandFailure.TIMEOUT
        ),
    })
    assert NetworkManagerEvidenceProvider(runner).read().reason == "timeout"


# ------------------------------------------------------------------------------ tailscale


def test_tailscale_status_is_reduced_to_what_could_attribute_an_interface():
    reading = TailscaleEvidenceProvider(FakeRunner(tailscale_status())).read()
    assert reading.status is ProviderStatus.OK
    assert reading.version == "1.102.3"
    daemon = reading.records[0]
    assert daemon["backend_state"] == "Running"
    assert daemon["tun"] is True
    assert daemon["tailscale_ips"] == ["100.64.0.10", "2001:db8::64:10"]
    assert "Peer" not in daemon and "peers" not in daemon


def test_a_stopped_daemon_is_a_successful_read_of_a_stopped_daemon():
    """Reporting is the agent's job; concluding from it is not."""
    reading = TailscaleEvidenceProvider(
        FakeRunner(tailscale_status(backend_state="Stopped"))
    ).read()
    assert reading.status is ProviderStatus.OK
    assert reading.records[0]["backend_state"] == "Stopped"


def test_no_tailscale_binary_is_absence():
    reading = TailscaleEvidenceProvider(FakeRunner({})).read()
    assert reading.status is ProviderStatus.ABSENT
    assert reading.reason == "tailscale_absent"


def test_a_tailscaled_that_cannot_be_reached_is_unavailable():
    runner = FakeRunner({
        STATUS_ARGV: CommandResult(STATUS_ARGV, 1, "", "failed to connect to local tailscaled"),
    })
    reading = TailscaleEvidenceProvider(runner).read()
    assert reading.status is ProviderStatus.UNAVAILABLE
    assert reading.reason == "daemon_not_answering"


def test_tailscale_output_that_is_not_json_is_an_error():
    runner = FakeRunner({STATUS_ARGV: CommandResult(STATUS_ARGV, 0, "not json", "")})
    reading = TailscaleEvidenceProvider(runner).read()
    assert reading.status is ProviderStatus.ERROR
    assert reading.reason == "unparseable_output"


# ------------------------------------------------------------------------------ readings


def test_a_reading_that_failed_cannot_carry_records():
    with pytest.raises(ValueError, match="must not carry records"):
        ProviderReading(
            provider="docker", source="docker.networks", status=ProviderStatus.UNAVAILABLE,
            method="unix_socket_http", observed_at="2026-01-01T00:00:00+00:00",
            reason="permission_denied", records=({"network_id": "x"},),
        )


def test_a_reading_that_failed_must_say_why():
    with pytest.raises(ValueError, match="must carry a reason"):
        ProviderReading(
            provider="docker", source="docker.networks", status=ProviderStatus.ERROR,
            method="unix_socket_http", observed_at="2026-01-01T00:00:00+00:00",
        )


def reading(provider: str, status: ProviderStatus) -> ProviderReading:
    return ProviderReading(
        provider=provider, source=f"{provider}.x", status=status, method="m",
        observed_at="2026-01-01T00:00:00+00:00",
        reason=None if status is ProviderStatus.OK else "because",
    )


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((ProviderStatus.OK, ProviderStatus.OK), SweepStatus.OK),
        # A host with none of these systems installed is a complete answer, not a failure.
        ((ProviderStatus.ABSENT, ProviderStatus.ABSENT), SweepStatus.OK),
        ((ProviderStatus.OK, ProviderStatus.UNAVAILABLE), SweepStatus.PARTIAL),
        ((ProviderStatus.ABSENT, ProviderStatus.ERROR), SweepStatus.PARTIAL),
        ((ProviderStatus.UNAVAILABLE, ProviderStatus.ERROR), SweepStatus.FAILED),
    ],
)
def test_batch_status_is_derived_from_the_readings(statuses, expected):
    batch = ProviderEvidenceBatch(
        capability="c", started_at="s", completed_at="c",
        readings=tuple(reading(f"p{i}", s) for i, s in enumerate(statuses)),
    )
    assert batch.derive_status() is expected


# ----------------------------------------------------------------------------- collector


class ExplodingProvider:
    provider = "exploding"
    source = "exploding.source"

    def read(self):
        raise RuntimeError("the provider is broken")


def test_one_broken_provider_does_not_take_the_others_with_it(docker_daemon):
    daemon = docker_daemon([docker_network("n" * 64, "bridge", bridge_name="docker0")])
    collector = NetworkProviderEvidenceCollector(
        runner=FakeRunner(tailscale_status()),
        docker_socket=daemon.path,
    )
    collector = NetworkProviderEvidenceCollector(
        providers=(*collector.providers, ExplodingProvider()),
    )
    batch = collector.collect()

    by_provider = {r.provider: r for r in batch.readings}
    assert by_provider["exploding"].status is ProviderStatus.ERROR
    assert by_provider["exploding"].reason == "provider_raised"
    assert by_provider["docker"].status is ProviderStatus.OK
    assert [i.code for i in batch.issues] == ["provider_raised"]


# --------------------------------------------------------------------------- capability


def test_the_capability_is_available_when_every_present_provider_answered(
    fake_root: Path, populated_sysfs: Path, docker_daemon
):
    daemon = docker_daemon([])
    runner = FakeRunner({**_ip(), **nmcli_devices([]), **tailscale_status()})
    capability = _providers_capability(fake_root, populated_sysfs, runner, daemon.path)
    assert capability.status is CapabilityStatus.AVAILABLE
    assert capability.detail["providers"] == {
        "docker": "ok", "networkmanager": "ok", "tailscale": "ok"
    }


def test_a_host_with_none_of_these_systems_is_still_available(
    fake_root: Path, populated_sysfs: Path, absent_docker: Path
):
    """"Nothing here owns anything" is an answer LocalPlane can act on."""
    capability = _providers_capability(
        fake_root, populated_sysfs, FakeRunner(_ip()), absent_docker
    )
    assert capability.status is CapabilityStatus.AVAILABLE
    assert set(capability.detail["providers"].values()) == {"absent"}


def test_a_provider_that_is_present_and_will_not_answer_is_degraded(
    fake_root: Path, populated_sysfs: Path, absent_docker: Path
):
    runner = FakeRunner({
        **_ip(),
        GENERAL_ARGV: CommandResult(GENERAL_ARGV, 8, "", "could not connect"),
    })
    capability = _providers_capability(fake_root, populated_sysfs, runner, absent_docker)
    assert capability.status is CapabilityStatus.DEGRADED
    assert capability.reason == "provider_sources_unreadable"
    assert capability.detail["unreadable"] == {"networkmanager": "nmcli_failed"}


def test_when_nothing_can_be_read_the_capability_is_unavailable(
    fake_root: Path, populated_sysfs: Path, tmp_path: Path
):
    dead = tmp_path / "dead.sock"
    dead.write_text("")
    runner = FakeRunner({
        **_ip(),
        GENERAL_ARGV: CommandResult(GENERAL_ARGV, 8, "", "could not connect"),
        STATUS_ARGV: CommandResult(STATUS_ARGV, 1, "", "could not connect"),
    })
    capability = _providers_capability(fake_root, populated_sysfs, runner, dead)
    assert capability.status is CapabilityStatus.UNAVAILABLE
    assert capability.reason == "no_provider_source_readable"


# ------------------------------------------------------------------------ agent operation


def test_the_agent_serves_provider_evidence_and_takes_no_parameters(
    fake_root: Path, populated_sysfs: Path, docker_daemon
):
    daemon = docker_daemon([docker_network("n" * 64, "bridge", bridge_name="docker0")])
    service = AgentService(
        root=fake_root,
        sysfs_net=populated_sysfs,
        runner=FakeRunner({**_ip(), **nmcli_devices([]), **tailscale_status()}),
        docker_socket=daemon.path,
    )
    result = service.handle("network.observe_providers", {})
    assert result["providers"]["status"] == "ok"
    assert {r["provider"] for r in result["providers"]["readings"]} == {
        "docker", "networkmanager", "tailscale"
    }

    with pytest.raises(ProtocolError) as raised:
        service.handle("network.observe_providers", {"names": ["docker0"]})
    assert raised.value.code is ErrorCode.UNKNOWN_FIELD


def test_the_operation_is_refused_when_its_capability_is_unavailable(
    fake_root: Path, populated_sysfs: Path, tmp_path: Path
):
    dead = tmp_path / "dead.sock"
    dead.write_text("")
    service = AgentService(
        root=fake_root,
        sysfs_net=populated_sysfs,
        runner=FakeRunner({
            **_ip(),
            GENERAL_ARGV: CommandResult(GENERAL_ARGV, 8, "", "no"),
            STATUS_ARGV: CommandResult(STATUS_ARGV, 1, "", "no"),
        }),
        docker_socket=dead,
    )
    with pytest.raises(ProtocolError) as raised:
        service.handle("network.observe_providers", {})
    assert raised.value.code is ErrorCode.CAPABILITY_UNAVAILABLE


def test_reading_the_providers_is_not_a_mutating_operation(
    fake_root: Path, populated_sysfs: Path, absent_docker: Path
):
    service = AgentService(
        root=fake_root,
        sysfs_net=populated_sysfs,
        runner=FakeRunner(_ip()),
        docker_socket=absent_docker,
    )
    hello = service.handle("agent.hello", {})
    assert "network.observe_providers" in METHODS
    # Reading the providers is not one of the methods that can change a host, and the set
    # of those is named rather than counted.
    assert hello["agent"]["mutating_methods"] == [
        "docker.container_lifecycle",
        "network.arm_mtu_guard",
        "network.set_interface_mtu",
        "systemd.service_lifecycle",
    ]
    assert "network.observe_providers" not in hello["agent"]["mutating_methods"]
    mutating = {c["capability"] for c in hello["capabilities"] if c["mutating"]}
    assert mutating == {
        "network.interface.set_mtu",
        "network.interface.mtu_guard",
        "docker.container.lifecycle",
        # Passively detected in Part A; it has no mutating protocol method yet.
        "systemd.service.lifecycle",
    }


# ------------------------------------------------------------------------------- helpers


def _ip() -> dict:
    from localplane.agent.providers.linux_network import ADDR_ARGV, LINK_ARGV
    from tests.conftest import json_result

    return {
        LINK_ARGV: json_result(LINK_ARGV, []),
        ADDR_ARGV: json_result(ADDR_ARGV, []),
    }


def _providers_capability(root, sysfs, runner, docker_socket):
    discovered = {
        c.capability: c
        for c in discover_capabilities(root, sysfs, runner, docker_socket)
    }
    return discovered[CAPABILITY_NETWORK_PROVIDERS_OBSERVE]
