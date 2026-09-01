"""The first host write: confirmation, arming, the Change, verification and recovery.

Everything runs through the real agent service over a real AF_UNIX socket, and the mutating
path through a real privileged-helper process object over a second real socket — so a write
goes the whole way: backend → agent protocol → agent → helper protocol → helper → a netlink
frame this code built. What is simulated is the kernel behind that frame, and only the
datagram exchange (:class:`tests.conftest.FakeKernelLinks`): every byte of the frame
construction, the attribute walk, the sequence correlation and the outcome decision is real,
and a successful mutation moves the sysfs tree the provider reads, so the verification that
follows is a genuine independent read rather than a rehearsal.

The machine this runs on is never touched. ``test_live_write.py`` does the real thing, in a
disposable network namespace, and proves this host was untouched around it.

Four claims are asserted here, and the file is organised around them:

* **a Change is the record that LocalPlane entered a write path** — not that a write
  happened, not that a plan exists, not that somebody confirmed one, not that a checkpoint
  was armed;
* **``write_unknown`` is not a failure and is never resolved by reading the value back** — it
  obliges restoration, and a restoration nobody has read back is a claim;
* **``failed`` is permitted only where nothing was written and that is provable**;
* **nothing is claimed that was not established** — a kernel acknowledgement is not success,
  and where a safe end state cannot be proven the answer is ``recovery_required``.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from localplane.agent.server import AgentServer
from localplane.agent.service import AgentService
from localplane.backend.agent_client import AgentClient
from localplane.backend.changes import ChangeService
from localplane.backend.db.database import MIGRATIONS_DIR, open_database
from localplane.backend.db.repositories import FindingRepository, ObjectRecord
from localplane.backend.domain.changes import (
    ChangeResult,
    HostEffect,
    MutationOutcome,
    RecoveryReason,
    RunEvent,
    VerificationOutcome,
    host_effect_for,
)
from localplane.backend.domain.identity import OBJECT_KIND_NETWORK_INTERFACE
from localplane.backend.domain.protection import ManagementPathVerdict
from localplane.backend.domain.runs import OperationType, RunState
from localplane.backend.ingest import Ingestor, ObservationCoordinator
from localplane.backend.management import ManagementService
from localplane.backend.operations import MTU, build_executors
from localplane.backend.provenance import ProvenanceService
from localplane.backend.runs import RunRefused, RunService
from localplane.helper.client import HelperClient
from localplane.helper.mtu import InterfaceMtuSetter, NetlinkFailure
from localplane.helper.protocol import MutationOutcome as HelperOutcome
from localplane.helper.server import HelperServer
from localplane.helper.service import HelperService
from tests.conftest import (
    FakeKernelLinks,
    FakeRunner,
    json_result,
    netlink_ack,
    nmcli_devices,
    tailscale_status,
    write_interface,
)

RECONCILE = OperationType.NETWORK_INTERFACE_RECONCILE_MTU
_NOW = "2026-08-23T12:00:00.000000+00:00"


# ------------------------------------------------------------------------------- fixture


@dataclass
class Estate:
    """A fixture host with a real agent, a real helper and a simulated kernel behind it."""

    database: Any
    sysfs: Path
    runner: FakeRunner
    kernel: FakeKernelLinks
    service: AgentService
    agent_server: AgentServer
    helper_server: HelperServer
    client: AgentClient
    ingestor: Ingestor
    coordinator: ObservationCoordinator
    management: ManagementService
    runs: RunService
    changes: ChangeService
    host_id: str = ""
    management_path: ManagementPathVerdict = None  # type: ignore[assignment]

    def observe(self) -> Any:
        result = self.coordinator.refresh_network()
        self.host_id = result.host_id
        return result

    def object_named(self, name: str) -> ObjectRecord:
        for record in self.ingestor.objects.list_by_kind(
            self.host_id, OBJECT_KIND_NETWORK_INTERFACE
        ):
            if record.display_name == name:
                return record
        raise AssertionError(f"no object named {name}")

    def write(self, interface: str, field_name: str, value: str) -> None:
        """Change a value on the fixture host. Never on the machine."""
        (self.sysfs / interface / field_name).write_text(value + "\n")

    def mtu_of(self, interface: str = "eth0") -> int:
        return int((self.sysfs / interface / "mtu").read_text().strip())

    def ifindex_of(self, name: str) -> int:
        return int((self.sysfs / name / "ifindex").read_text().strip())

    def link_snapshot(self) -> dict[str, dict[str, str]]:
        return {
            entry.name: {f.name: f.read_text() for f in sorted(entry.iterdir()) if f.is_file()}
            for entry in sorted(self.sysfs.iterdir())
            if (entry / "ifindex").exists()
        }

    # -------------------------------------------------------------------------- shortcuts

    def adopt(self, name: str = "eth0") -> Any:
        return self.management.adopt(self.object_named(name))

    def drift(self, name: str = "eth0", mtu: str = "1400") -> None:
        """Make the runtime disagree with the retained intent, the way drift happens."""
        self.write(name, "mtu", mtu)
        self.kernel.links[self.ifindex_of(name)] = (name, int(mtu))
        self.observe()

    def plan(self, name: str = "eth0") -> Any:
        return self.runs.create(RECONCILE, self.object_named(name), self.management_path)

    def confirm(self, run: Any, **overrides: Any) -> Any:
        body = {"preview_id": run.preview.preview_id, "acknowledge": True,
                "expected_preview_digest": None, "management_path": self.management_path}
        body.update(overrides)
        return self.changes.confirm(run, **body)

    def apply(self, run: Any, management_path: ManagementPathVerdict | None = None) -> Any:
        return self.changes.apply(
            run, self.management_path if management_path is None else management_path)

    def prove_path_on(self, name: str = "eth1") -> Any:
        """Prove the operator's path terminates on this object.

        Called with ``eth1`` — some object other than the write target — for every ordinary
        apply, and with ``eth0`` for the guarded ones, where the target *is* the path.

        Real evidence is written rather than a fabricated identifier: a preview's protection
        evidence is a foreign key into the observations table, so a verdict naming a row that
        does not exist is one the store would refuse — which is the point.
        """
        from localplane.backend.db.repositories import ManagementPathRepository
        import uuid as _uuid

        observation_id = f"mpo_{_uuid.uuid4().hex}"
        with self.database.transaction():
            ManagementPathRepository(self.database).insert({
                "observation_id": observation_id, "host_id": self.host_id, "observed_at": _NOW,
                "agent_instance_id": None,
                "transport_peer_address": "192.0.2.130", "transport_peer_family": "inet",
                "local_endpoint_address": "192.0.2.215", "local_endpoint_family": "inet",
                "capability": "network.route.observe", "provider": "linux.route",
                "provider_version": "1", "method": "netlink_rtm_getroute",
                "route_status": "resolved", "route_reason": None, "route_family": "inet",
                "route_destination": "192.0.2.130", "route_destination_prefix_length": 32,
                "route_preferred_source": "192.0.2.215", "route_gateway": None,
                "route_oif_index": 3, "route_table": 254, "route_type": "unicast",
                "route_scope": "universe", "route_protocol": "unspec", "route_priority": None,
                "route_error": None,
            })
        self.management_path = ManagementPathVerdict(
            resource_id=self.object_named(name).object_id, reason="management_path_confirmed",
            evidence_id=observation_id, observed_at=_NOW)
        return self.management_path

    def networkmanager_takes_over_eth0(self) -> None:
        self.runner.responses.update(nmcli_devices([
            ("eth0", "ethernet", "connected", "Wired eth0", "uuid-eth0"),
            ("eth1", "ethernet", "unavailable", "", ""),
        ]))

    def events(self, run_id: str) -> list[str]:
        return [e.event for e in self.changes.transcript(run_id)]

    def rows(self, table: str) -> list[Any]:
        return self.database.query(f"SELECT * FROM {table}")


def _runner() -> FakeRunner:
    from localplane.agent.providers.linux_network import ADDR_ARGV, LINK_ARGV

    return FakeRunner({
        LINK_ARGV: json_result(LINK_ARGV, [{"ifindex": 2, "ifname": "eth0"},
                                           {"ifindex": 3, "ifname": "eth1"}]),
        ADDR_ARGV: json_result(ADDR_ARGV, [{"ifindex": 1, "ifname": "lo", "addr_info": []},
                                           {"ifindex": 2, "ifname": "eth0", "addr_info": []},
                                           {"ifindex": 3, "ifname": "eth1", "addr_info": []}]),
        **nmcli_devices([("lo", "loopback", "connected (externally)", "lo", "uuid-lo"),
                         ("eth0", "ethernet", "unavailable", "", ""),
                         ("eth1", "ethernet", "unavailable", "", "")]),
        **tailscale_status(),
    })


@pytest.fixture
def estate(database, fake_root: Path, sysfs_net: Path, tmp_path: Path) -> Any:
    """Three links, a real agent, a real privileged helper and a simulated kernel.

    ``eth0`` is the write target and ``eth1`` is what the operator's connection is proven to
    arrive on, so every apply in this file happens against an object *proven not* to be the
    management path — the only kind this build will write to at all.
    """
    write_interface(sysfs_net, "lo", ifindex=1, address="00:00:00:00:00:00", arphrd="772",
                    flags="0x9", operstate="unknown", carrier="1", mtu="65536",
                    speed=None, duplex=None)
    write_interface(sysfs_net, "eth0", ifindex=2, address="02:00:00:00:00:10", flags="0x1003",
                    operstate="up", carrier="1", mtu="1500", device="fd580000.ethernet",
                    subsystem="platform")
    write_interface(sysfs_net, "eth1", ifindex=3, address="02:00:00:00:00:12", flags="0x1003",
                    operstate="up", carrier="1", mtu="1500", speed="1000", duplex="full",
                    device="1-1.4:1.0", subsystem="usb")

    kernel = FakeKernelLinks(links={1: ("lo", 65536), 2: ("eth0", 1500), 3: ("eth1", 1500)},
                             sysfs_net=sysfs_net)
    helper_server = HelperServer(tmp_path / "helper.sock", HelperService(transport=kernel))
    helper_server.serve_in_thread()

    runner = _runner()
    service = AgentService(root=fake_root, sysfs_net=sysfs_net, runner=runner,
                           docker_socket=tmp_path / "docker-absent.sock",
                           helper_client=HelperClient(tmp_path / "helper.sock"),
                           helper_socket=tmp_path / "helper.sock")
    agent_server = AgentServer(tmp_path / "agent.sock", service)
    agent_server.serve_in_thread()

    client = AgentClient(agent_server.socket_path, timeout_s=10.0)
    provenance = ProvenanceService(database)
    management = ManagementService(database, freshness_ttl_s=60.0, provenance=provenance)
    ingestor = Ingestor(database, management)
    coordinator = ObservationCoordinator(client, ingestor)
    from localplane.backend.operations import OPERATIONS

    runs = RunService(database, 60.0, OPERATIONS, provenance)
    estate = Estate(
        database=database, sysfs=sysfs_net, runner=runner, kernel=kernel, service=service,
        agent_server=agent_server, helper_server=helper_server, client=client,
        ingestor=ingestor, coordinator=coordinator, management=management, runs=runs,
        changes=ChangeService(database, runs,
                              build_executors(client, coordinator, ingestor.objects)),
    )
    estate.observe()
    estate.prove_path_on("eth1")
    try:
        yield estate
    finally:
        for server in (agent_server, helper_server):
            server.shutdown()
            server.server_close()


@pytest.fixture
def ready(estate: Estate) -> Estate:
    """``eth0`` adopted at 1500, drifted to 1400, with the path proven onto ``eth1``.

    The state a write starts from: a managed object whose runtime disagrees with the retained
    intent, and a request that can prove it is not talking to LocalPlane through the object it
    is about to change.
    """
    estate.adopt("eth0")
    estate.drift("eth0", "1400")
    estate.prove_path_on("eth1")
    return estate


def applied(estate: Estate, name: str = "eth0") -> Any:
    """Plan, confirm and apply in the ordinary way. The happy path, as a helper."""
    run = estate.plan(name).run
    estate.confirm(run)
    return estate.apply(run)


# ------------------------------------------------------------------ the closed vocabulary


def test_every_way_to_change_a_host_is_named_at_every_layer():
    """Named at each layer, and the layers are allowed to answer different questions.

    The agent gained a second mutating method and capability when Docker arrived, and a
    third of each when the connection guard did — a method that writes nothing and can cause
    a kernel write with no further request, which is exactly why it is counted here. The
    systemd dispatch transport is the fourth: one closed method on the agent's own
    unprivileged bus connection, under whatever PolicyKit allows at the moment it asks.

    **The privileged helper gained nothing, again**, and that is the load-bearing half of
    this test: a guard is a deferred use of the write the helper already performs, and the
    systemd path does not reach the root process at all, so its method set is byte-for-byte
    what it was before any of these arrived.

    Capability and protocol still answer different questions and may still differ.
    Capability describes whether a mechanism exists on this host; protocol describes what
    this build can invoke. The systemd lifecycle capability has been declared since Part A,
    from passive introspection, and the method that uses it arrived later. Any other growth,
    or any helper growth, fails here.
    """
    from localplane.helper.protocol import HELPER_METHODS, HELPER_PARAMS, MUTATING_HELPER_METHODS
    from localplane.protocol.capabilities import CAPABILITIES, MUTATING_CAPABILITIES
    from localplane.protocol.wire import METHODS, MUTATING_METHODS

    assert MUTATING_METHODS == {
        "network.set_interface_mtu", "network.arm_mtu_guard",
        "docker.container_lifecycle", "systemd.service_lifecycle"} <= METHODS
    assert MUTATING_CAPABILITIES == {
        "network.interface.set_mtu", "network.interface.mtu_guard",
        "docker.container.lifecycle", "systemd.service.lifecycle"}
    # Exactly one systemd method can change a host, and it is that one.
    assert {
        method for method in MUTATING_METHODS if method.startswith("systemd.")
    } == {"systemd.service_lifecycle"}
    # The two guard methods that can only *prevent* or *report* a write are not among them,
    # and that distinction is the point of the classification rather than an oversight.
    assert {"network.disarm_mtu_guard", "network.mtu_guard_status"} <= METHODS
    assert not {"network.disarm_mtu_guard", "network.mtu_guard_status"} & MUTATING_METHODS
    assert all(CAPABILITIES[name].mutating is True for name in MUTATING_CAPABILITIES)
    assert {n for n, d in CAPABILITIES.items() if d.mutating} == MUTATING_CAPABILITIES

    # The privileged component is exactly where it was. Docker does not go through it.
    assert HELPER_METHODS == {"helper.hello", "network.interface.set_mtu"}
    assert MUTATING_HELPER_METHODS == {"network.interface.set_mtu"}
    # And the mutating helper method takes five typed scalars and nothing else.
    assert HELPER_PARAMS["network.interface.set_mtu"] == {
        "attempt_id", "ifindex", "expected_current_mtu", "desired_mtu", "expected_interface_name"}
    assert HELPER_PARAMS["helper.hello"] == frozenset()


def test_the_agent_reports_every_mutating_method_and_holds_no_kernel_privilege(estate: Estate):
    hello = estate.client.hello()
    assert hello["agent"]["mutating_methods"] == [
        "docker.container_lifecycle",
        "network.arm_mtu_guard",
        "network.set_interface_mtu",
        "systemd.service_lifecycle",
    ]
    assert hello["agent"]["privilege"] == ("root" if os.geteuid() == 0 else "unprivileged")


@pytest.mark.parametrize(
    "forbidden",
    ["subprocess", "os.system", "os.exec", "popen", "shell=", "eval(", "argv", "importlib",
     "__import__", "getattr(", "ioctl", "SIOCSIFMTU",
     # Every rtnetlink message an operator would fear, none of which this package spells.
     "RTM_DELLINK", "RTM_SETLINK", "RTM_NEWADDR", "RTM_DELADDR", "RTM_NEWROUTE",
     "RTM_DELROUTE", "RTM_NEWRULE", "RTM_DELRULE", "RTM_NEWNEIGH", "RTM_NEWQDISC",
     "IFLA_ADDRESS", "IFLA_OPERSTATE"],
)
def test_the_privileged_helper_contains_no_way_to_name_a_thing_to_run(forbidden: str):
    """Read out of the source rather than trusted from a docstring.

    This is the component that runs as root. It must contain no way to name a thing to run —
    no command, argument vector, shell, subprocess, module or dynamic attribute lookup
    (``getattr(`` is on the list because a dispatch table resolved from a caller's string is
    the shape every one of the others eventually takes) — and no netlink message type beyond
    the two constants it needs. This is also the reason the helper does not use a
    general-purpose netlink library: it would make the same guarantee a matter of code review.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "localplane" / "helper"
    for module in sorted(root.rglob("*.py")):
        source = _code_only(module).lower().replace("shell=false", "")
        assert forbidden.lower() not in source, f"{module.name} mentions {forbidden}"


def test_the_only_mutating_frame_is_rtm_newlink_with_an_mtu_and_no_flag_change():
    """The frame is read back off the wire, and its builders take no message type.

    ``ifi_change`` is zero: this request moves an attribute, not a link's flags, and a
    non-zero change mask is how a link is brought up or down. Not asking is stronger than
    asking for nothing.
    """
    import inspect

    from localplane.helper import mtu as module

    assert set(inspect.signature(module._set_mtu_request).parameters) == {
        "ifindex", "mtu", "sequence"}
    assert "type" not in inspect.signature(module._get_link_request).parameters
    source = _code_only(Path(module.__file__))
    assert "def send(" not in source and "def call(" not in source

    frame = module._set_mtu_request(7, 1400, 1)
    length, kind, flags, _sequence, _pid = module._NLMSGHDR.unpack_from(frame, 0)
    assert kind == module.RTM_NEWLINK
    assert flags == 0x01 | 0x04  # REQUEST | ACK, and no CREATE, EXCL or REPLACE
    family, _pad, _type, ifindex, link_flags, change = module._IFINFOMSG.unpack_from(
        frame[module._NLMSGHDR.size : length], 0)
    assert (family, ifindex, link_flags, change) == (0, 7, 0, 0)


def _code_only(module: Path) -> str:
    """A module's source with its docstrings and comments removed.

    The prose explains the absences by naming them, and naming a thing is what prose is for.
    What must not contain them is the code, so the code is what is read.
    """
    source = module.read_text()
    without_comments = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#"))
    return "".join(p for i, p in enumerate(without_comments.split('"""')) if i % 2 == 0)


# --------------------------------------------------------------- the privilege boundary


def test_an_unauthorized_peer_reaches_neither_the_parser_nor_the_kernel(tmp_path: Path):
    """The boundary is a kernel fact about the process on the other end, not a token.

    ``SO_PEERCRED`` cannot be forged by the peer and cannot be supplied in a message, which is
    what makes it usable as an authorisation boundary in a product with no authentication. The
    check happens before the request is decoded, so an unauthorised caller does not reach the
    parser — and certainly not the kernel.
    """
    from localplane.helper.client import HelperError

    kernel = FakeKernelLinks(links={2: ("eth0", 1500)})
    refused_uid = 0 if os.geteuid() != 0 else 65534
    server = HelperServer(tmp_path / "closed.sock", HelperService(transport=kernel),
                          allowed_uids={refused_uid})
    server.serve_in_thread()
    try:
        client = HelperClient(server.socket_path, timeout_s=5.0)
        for call in (lambda: client.hello(),
                     lambda: client.set_interface_mtu(attempt_id="a1", ifindex=2,
                                                      expected_current_mtu=1500,
                                                      desired_mtu=1400)):
            with pytest.raises(HelperError) as raised:
                call()
            assert raised.value.code == "unauthorized_peer"
            assert raised.value.detail["reason"] == "peer_uid_not_allowed"
            assert raised.value.detail["peer_uid"] == os.geteuid()
        assert kernel.mutations == [] and kernel.queries == []
    finally:
        server.shutdown()
        server.server_close()


def test_an_empty_allowlist_refuses_everybody_rather_than_everybody(tmp_path: Path):
    """The one default that must not exist on a socket that reaches privilege."""
    from localplane.helper.client import HelperError

    server = HelperServer(tmp_path / "none.sock", HelperService(transport=FakeKernelLinks()),
                          allowed_uids=set(), allowed_gids=set())
    server.serve_in_thread()
    try:
        with pytest.raises(HelperError) as raised:
            HelperClient(server.socket_path, timeout_s=5.0).hello()
        assert raised.value.code == "unauthorized_peer"
        assert raised.value.detail["reason"] == "no_peer_is_allowed"
    finally:
        server.shutdown()
        server.server_close()


def test_the_helper_refuses_every_method_and_parameter_it_does_not_declare(estate: Estate):
    """There is nowhere to smuggle a command, an executable or a message type."""
    from localplane.helper.client import HelperError

    client = HelperClient(estate.helper_server.socket_path, timeout_s=5.0)
    for method in ("network.interface.set_admin_up", "network.address.add", "exec", "shell"):
        with pytest.raises(HelperError) as raised:
            client.call(method, {})
        assert raised.value.code == "unsupported_method"
    for extra in ("command", "argv", "shell", "executable", "provider", "message_type"):
        with pytest.raises(HelperError) as raised:
            client.call("network.interface.set_mtu", {
                "attempt_id": "a1", "ifindex": 2, "expected_current_mtu": 1500,
                "desired_mtu": 1400, extra: "anything"})
        assert raised.value.code == "unknown_field"
        assert extra in raised.value.detail["unknown"]
    assert estate.kernel.mutations == []
    assert estate.helper_server.socket_path.stat().st_mode & 0o777 == 0o600
    assert estate.helper_server.socket_path.parent.stat().st_mode & 0o077 == 0


# ------------------------------------------------------------------ the typed outcomes


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"desired_mtu": 0}, "mtu_out_of_range"),
        ({"desired_mtu": 67}, "mtu_out_of_range"),
        ({"desired_mtu": 65537}, "mtu_out_of_range"),
        ({"ifindex": 99}, "interface_not_found"),
        ({"expected_current_mtu": 9000}, "mtu_precondition_failed"),
        ({"expected_interface_name": "eth9"}, "interface_identity_mismatch"),
        ({"desired_mtu": 1500}, "desired_equals_current"),
    ],
)
def test_a_precondition_that_fails_is_not_written_and_the_kernel_never_sees_it(
    kwargs: dict, reason: str
):
    """Refused before the mutating frame exists, which is what makes "not written" a proof.

    The current-value check is the final race guard and the one the kernel itself enforces
    for LocalPlane: between the plan and this instant somebody may have set another value,
    and writing over it would be acting on a world that has moved.
    """
    kernel = FakeKernelLinks(links={2: ("eth0", 1500)})
    result = InterfaceMtuSetter(transport=kernel).set_mtu(
        **{"attempt_id": "a1", "ifindex": 2, "expected_current_mtu": 1500,
           "desired_mtu": 1400, **kwargs})
    assert result.outcome is HelperOutcome.NOT_WRITTEN
    assert result.reason == reason
    assert kernel.mutations == []


def test_a_correlated_acknowledgement_is_what_makes_a_write_written():
    kernel = FakeKernelLinks(links={2: ("eth0", 1500)})
    result = InterfaceMtuSetter(transport=kernel).set_mtu(
        attempt_id="a1", ifindex=2, expected_current_mtu=1500, desired_mtu=1400)
    assert result.outcome is HelperOutcome.WRITTEN
    assert result.reason == "kernel_acknowledged"
    assert kernel.mutations == [(2, 1400)]


def test_a_definite_kernel_error_is_not_written_and_carries_the_errno():
    """The kernel answered, and its answer was no. That is a proof, not an ambiguity."""
    import errno as errno_module

    kernel = FakeKernelLinks(links={2: ("eth0", 1500)})
    kernel.on_mutate["*"] = lambda f: netlink_ack(_sequence(f), errno=errno_module.EPERM)
    result = InterfaceMtuSetter(transport=kernel).set_mtu(
        attempt_id="a1", ifindex=2, expected_current_mtu=1500, desired_mtu=1400)
    assert result.outcome is HelperOutcome.NOT_WRITTEN
    assert result.reason == "kernel_rejected"
    assert (result.kernel_errno, result.kernel_error) == (
        errno_module.EPERM, "Operation not permitted")


@pytest.mark.parametrize(
    "scripted, reason",
    [
        (NetlinkFailure("acknowledgement_timeout", {}, True), "acknowledgement_timeout"),
        (NetlinkFailure("acknowledgement_not_from_kernel", {}, True),
         "acknowledgement_not_from_kernel"),
        (lambda f: netlink_ack(_sequence(f) + 7, errno=0), "acknowledgement_uncorrelated"),
        (lambda f: netlink_ack(_sequence(f), errno=0, echo_type=18),
         "acknowledgement_echoes_another_request"),
        # A header declaring more bytes than the datagram carries.
        (lambda f: _truncated_ack(_sequence(f)), "malformed_netlink_response"),
    ],
)
def test_a_dispatch_without_a_trustworthy_answer_is_write_unknown(scripted, reason: str):
    """The request may already have been accepted. There is no honest fourth answer."""
    kernel = FakeKernelLinks(links={2: ("eth0", 1500)})
    kernel.on_mutate["*"] = scripted
    result = InterfaceMtuSetter(transport=kernel).set_mtu(
        attempt_id="a1", ifindex=2, expected_current_mtu=1500, desired_mtu=1400)
    assert result.outcome is HelperOutcome.WRITE_UNKNOWN
    assert result.reason == reason


def test_a_request_that_never_left_the_process_is_not_written():
    kernel = FakeKernelLinks(links={2: ("eth0", 1500)})
    kernel.on_mutate["*"] = NetlinkFailure("netlink_send_failed", {}, False)
    result = InterfaceMtuSetter(transport=kernel).set_mtu(
        attempt_id="a1", ifindex=2, expected_current_mtu=1500, desired_mtu=1400)
    assert result.outcome is HelperOutcome.NOT_WRITTEN
    assert result.reason == "netlink_send_failed"


def test_an_attempt_is_dispatched_at_most_once(estate: Estate):
    """A client that retries is told what happened, not given a second write."""
    client = HelperClient(estate.helper_server.socket_path, timeout_s=5.0)
    first = client.set_interface_mtu(attempt_id="attempt-1", ifindex=2,
                                     expected_current_mtu=1500, desired_mtu=1400)
    again = client.set_interface_mtu(attempt_id="attempt-1", ifindex=2,
                                     expected_current_mtu=1500, desired_mtu=1400)
    assert (first["outcome"], first["replayed"]) == ("written", False)
    assert (again["outcome"], again["replayed"]) == ("written", True)
    assert estate.kernel.mutations == [(2, 1400)], "a retry became a second write"


def test_the_two_mutation_vocabularies_are_the_same_three_words():
    """Two enums rather than one shared import, because the privilege boundary is a boundary.

    The helper's protocol is its own and the backend translates across it. What must never
    happen is the two drifting, so the translation is asserted total rather than assumed.
    """
    assert {str(m) for m in HelperOutcome} == {str(m) for m in MutationOutcome}
    assert host_effect_for(MutationOutcome.NOT_WRITTEN) is HostEffect.NONE
    assert host_effect_for(MutationOutcome.WRITTEN) is HostEffect.WRITTEN
    assert host_effect_for(MutationOutcome.WRITE_UNKNOWN) is HostEffect.WRITE_UNKNOWN


# ------------------------------------------------------------------------ the happy path


def test_the_whole_path_runs_and_the_host_carries_the_intended_value(ready: Estate):
    """Plan, confirm, arm, cross the boundary, write, verify, succeed.

    Every claim is checked against a different source: the Run's state, the Change's outcome,
    the transcript's order, the simulated kernel's own record of what it was asked, and the
    sysfs tree the provider reads back.
    """
    run = ready.plan("eth0").run
    assert run.state == str(RunState.PREVIEW)
    assert ready.changes.change_for_run(run.run_id) is None

    ready.confirm(run)
    assert ready.changes.change_for_run(run.run_id) is None, "confirming creates no change"
    assert ready.kernel.mutations == []
    assert ready.runs.get(run.run_id).host_effect == "none"

    outcome = ready.apply(run)
    change = outcome.change

    assert outcome.run.state == str(RunState.SUCCEEDED)
    assert outcome.run.host_effect == str(HostEffect.WRITTEN)
    assert change.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert change.mutation_provider == "linux.link"
    assert change.mutation_method == "netlink_rtm_newlink_mtu"
    assert change.verification_outcome == str(VerificationOutcome.VERIFIED)
    assert change.result == str(ChangeResult.SUCCEEDED)
    assert (change.before_value, change.desired_value) == (1400, 1500)

    assert ready.kernel.mutations == [(2, 1500)], "the kernel was asked once, for this"
    assert ready.mtu_of("eth0") == 1500
    assert ready.mtu_of("eth1") == 1500, "the operator's own path was not touched"
    assert ready.changes.lock_for(run.run_id) is None, "a finished run releases the object"

    assert ready.events(run.run_id) == [
        str(RunEvent.RUN_PLANNED), str(RunEvent.CONFIRMATION_SATISFIED),
        str(RunEvent.CONFIRMATION_REQUIRED), str(RunEvent.CONFIRMATION_CONSUMED),
        str(RunEvent.ARMING_STARTED), str(RunEvent.CHECKPOINT_WRITTEN),
        str(RunEvent.WRITE_BOUNDARY_CROSSED), str(RunEvent.MUTATION_DISPATCHED),
        str(RunEvent.MUTATION_RESULT), str(RunEvent.VERIFICATION_STARTED),
        str(RunEvent.VERIFICATION_RESULT), str(RunEvent.RUN_FINISHED),
    ]


def test_success_resolves_the_drift_finding_from_the_verification_observation(ready: Estate):
    """The Change did not resolve the drift. The evidence did, and it is named.

    Verification goes through the ordinary observation path — the same sweep, provider,
    normalisation, store and finding lifecycle every other judgement rests on — so the
    resolution is what any sweep would have produced, attributed to the reading that proved it.
    """
    findings = FindingRepository(ready.database)
    eth0 = ready.object_named("eth0")
    open_before = [f for f in findings.open_for_object(eth0.object_id) if f.subject == MTU]
    assert len(open_before) == 1, "the fixture should start with an open MTU drift finding"

    sweeps_before = len(ready.rows("observation_sweeps"))
    change = applied(ready).change

    assert change.verification_observation_id is not None
    observation = ready.database.query_one(
        "SELECT * FROM observations WHERE observation_id = ?",
        (change.verification_observation_id,))
    assert (observation["capability"], observation["provider"]) == (
        "network.observe", "linux.network")
    assert len(ready.rows("observation_sweeps")) > sweeps_before, "a real sweep, like any other"

    resolved = findings.get(open_before[0].finding_id)
    assert resolved.status == "resolved"
    assert resolved.resolution == "observed_matches_intent"
    assert resolved.resolved_by_observation_id == change.verification_observation_id
    assert findings.open_for_object(eth0.object_id) == []
    reconciliation = ready.management.reconciliation_for(
        ready.object_named("eth0"),
        ready.runs.intents.get(ready.object_named("eth0").active_intent_id))
    assert str(reconciliation.state) == "in_sync"


# --------------------------------------------------------- the boundary and the crash window


def test_the_checkpoint_is_on_disk_before_the_mutation_is_dispatched(ready: Estate):
    """"Recovery is armed" means a row exists, and it exists before anything can be written.

    Proved by watching the store from inside the dispatch: when the privileged helper is first
    asked to mutate anything, the checkpoint is already committed, the Change already exists,
    its dispatch marker is already set, and it claims no host effect yet.
    """
    seen: dict[str, Any] = {}
    original = ready.kernel.mutate

    def watching(frame: bytes) -> bytes:
        seen["checkpoint"] = dict(ready.database.query_one("SELECT * FROM run_checkpoints") or {})
        seen["change"] = dict(ready.database.query_one("SELECT * FROM changes") or {})
        return original(frame)

    ready.kernel.mutate = watching  # type: ignore[method-assign]
    try:
        run = applied(ready).run
    finally:
        ready.kernel.mutate = original  # type: ignore[method-assign]

    checkpoint, change = seen["checkpoint"], seen["change"]
    assert checkpoint, "no checkpoint was on disk when the kernel was asked to write"
    assert (checkpoint["before_value"], checkpoint["desired_value"]) == (1400, 1500)
    assert checkpoint["protection_management_path"] == "not_on_management_path"
    assert change, "no change existed when the kernel was asked to write"
    assert change["dispatch_began_at"] is not None
    assert change["mutation_outcome"] is None, "an outcome existed before the answer did"
    assert change["host_effect"] == "none"

    # And the checkpoint carries enough to reconstruct the restoration from itself.
    stored = ready.changes.checkpoint_for(run.run_id)
    assert (stored.run_id, stored.preview_id) == (run.run_id, run.preview.preview_id)
    assert (stored.field, stored.before_value, stored.desired_value) == (MTU, 1400, 1500)
    assert stored.intent_id == run.preview.intent_id
    assert stored.observation_id == run.preview.observation_id
    assert stored.protection_evidence_id == run.preview.protection_evidence_id
    assert stored.execution_correlation == {
        "ifindex": ready.ifindex_of("eth0"), "interface_name": "eth0",
        "observation_id": run.preview.observation_id}


def test_a_checkpoint_that_cannot_be_written_ends_the_run_before_the_boundary(ready: Estate):
    """Arming is an execution in its own right, and its failure leaves nothing behind.

    ``failed`` is honest here for the one reason it is honest anywhere: the code that could
    perform a write had not been reached. No Change exists, the host was not contacted, and the
    lock is released so the next attempt is not blocked by this one.
    """
    run = ready.plan("eth0").run
    ready.confirm(run)
    before = ready.link_snapshot()
    # A checkpoint already occupying this Run's UNIQUE slot is the narrowest way to make arming
    # fail for real, through the store rather than through a patched function.
    with ready.database.transaction():
        ready.changes.checkpoints.insert({
            "checkpoint_id": "ckp_squatter", "run_id": run.run_id,
            "preview_id": run.preview.preview_id, "host_id": run.host_id,
            "object_id": run.object_id, "intent_id": run.preview.intent_id,
            "intent_version": run.preview.intent_version, "field": MTU,
            "value_type": "integer", "before_value": 1400, "desired_value": 1500,
            "observation_id": run.preview.observation_id,
            "observed_at": run.preview.observed_at,
            "protection_management_path": "not_on_management_path",
            "protection_evidence_id": None, "execution_correlation": "{}", "armed_at": _NOW})

    with pytest.raises(RunRefused) as raised:
        ready.apply(run)
    assert raised.value.code == "checkpoint_not_written"

    finished = ready.runs.get(run.run_id)
    assert finished.state == str(RunState.FAILED)
    assert finished.host_effect == str(HostEffect.NONE)
    assert ready.changes.change_for_run(run.run_id) is None
    assert ready.rows("changes") == []
    assert ready.kernel.mutations == [], "the host was contacted after arming failed"
    assert ready.link_snapshot() == before
    assert ready.changes.lock_for(run.run_id) is None
    assert str(RunEvent.WRITE_BOUNDARY_CROSSED) not in ready.events(run.run_id)
    assert str(RunEvent.ARMING_FAILED) in ready.events(run.run_id)


def test_a_change_is_created_only_by_applying_and_is_none_of_the_other_records(ready: Estate):
    """Planning, confirming and arming create no Change; only crossing the boundary does.

    And a Change is not a management transition or an intent revision: those two tables are
    still structurally incapable of claiming a host write, and a test reads their CHECKs.
    """
    run = ready.plan("eth0").run
    assert ready.rows("changes") == []
    ready.confirm(run)
    assert ready.rows("changes") == []
    change = ready.apply(run).change
    assert len(ready.rows("changes")) == 1

    assert [r["transition"] for r in ready.rows("management_transitions")] == ["adopt"]
    assert ready.rows("intent_revisions") == []
    for table in ("management_transitions", "intent_revisions"):
        sql = ready.database.query_one(
            "SELECT sql FROM sqlite_master WHERE name = ?", (table,))["sql"]
        assert "host_effect = 'none'" in sql
    assert change.host_effect == str(HostEffect.WRITTEN)
    assert ready.runs.get(run.run_id).host_effect == str(HostEffect.WRITTEN)


@pytest.mark.parametrize("dispatched", [True, False])
def test_an_interrupted_change_is_settled_conservatively_on_restart(
    ready: Estate, dispatched: bool
):
    """The crash window, made visible and then read back under the only safe rule.

    A Change that says dispatch began and does not say what happened is ``write_unknown``,
    conservatively, on every restart — never "nothing happened", and never resolved by reading
    the value back, which would answer what the host holds rather than what LocalPlane did. One
    whose dispatch had not begun is ``not_written``, which is a proof rather than an assumption.
    """
    run = applied(ready).run
    change = ready.changes.change_for_run(run.run_id)

    # Rewind to the instant a process death in that window leaves behind.
    with ready.database.transaction():
        ready.changes.changes.update(change.change_id, {
            "dispatch_began_at": change.dispatch_began_at if dispatched else None,
            "mutation_outcome": None, "mutation_reason": None, "settled_at": None,
            "host_effect": "none", "verification_outcome": "not_attempted",
            "verification_observation_id": None, "verification_observed_value": None,
            "verification_reason": None, "result": "in_flight", "finished_at": None})
        ready.runs.runs.set_state(run_id=run.run_id, state=str(RunState.APPLYING),
                                  host_effect="none")
        ready.database.connection.execute(
            "UPDATE runs SET finished_at = NULL WHERE run_id = ?", (run.run_id,))
        ready.changes.locks.acquire(host_id=run.host_id, object_id=run.object_id, field=MTU,
                                    run_id=run.run_id, now=_NOW)

    settled = ready.changes.settle_interrupted()
    assert [c.change_id for c in settled] == [change.change_id]
    recovered = ready.changes.get_change(change.change_id)

    if dispatched:
        assert recovered.mutation_outcome == str(MutationOutcome.WRITE_UNKNOWN)
        assert recovered.host_effect == str(HostEffect.WRITE_UNKNOWN)
        assert recovered.result == str(ChangeResult.RECOVERY_REQUIRED)
        assert recovered.recovery_reason == str(RecoveryReason.APPLY_WRITE_UNKNOWN)
        assert ready.runs.get(run.run_id).state == str(RunState.RECOVERY_REQUIRED)
        # The object stays held: its value is unproven and a second change would build on it.
        assert ready.changes.lock_for(run.run_id) is not None
    else:
        assert recovered.mutation_outcome == str(MutationOutcome.NOT_WRITTEN)
        assert recovered.host_effect == str(HostEffect.NONE)
        assert recovered.result == str(ChangeResult.FAILED)
        assert recovered.recovery_required is False
        assert ready.runs.get(run.run_id).state == str(RunState.FAILED)
        assert ready.changes.lock_for(run.run_id) is None
    # Nothing was written to put it right: recovery on a restart does not write.
    assert ready.kernel.mutations == [(2, 1500)]


def _interrupt(estate: Estate, run: Any, change: Any, **columns: Any) -> None:
    """Rewind the durable record to the instant a death *after* the result leaves behind.

    The mutation columns are left exactly as the caller wants them: this is the window
    between `_settle` and `_finish`, where what became of the mutation is already on disk
    and the Run has not ended. The lock is retaken because the original apply released it.
    """
    with estate.database.transaction():
        estate.changes.changes.update(
            change.change_id, {"result": "in_flight", "finished_at": None, **columns})
        estate.runs.runs.set_state(
            run_id=run.run_id, state=str(RunState.VERIFYING),
            host_effect=columns.get("host_effect", change.host_effect))
        estate.database.connection.execute(
            "UPDATE runs SET finished_at = NULL WHERE run_id = ?", (run.run_id,))
        if estate.changes.lock_for(run.run_id) is None:
            estate.changes.locks.acquire(
                host_id=run.host_id, object_id=run.object_id, field=MTU,
                run_id=run.run_id, now=_NOW)


@pytest.mark.parametrize(
    "columns, result, state, reason, released",
    [
        pytest.param(
            {}, ChangeResult.SUCCEEDED, RunState.SUCCEEDED, None, True,
            id="written_and_verified_succeeds"),
        pytest.param(
            {"verification_outcome": str(VerificationOutcome.MISMATCH),
             "verification_observation_id": None, "verification_reason": "value_differs"},
            ChangeResult.RECOVERY_REQUIRED, RunState.RECOVERY_REQUIRED,
            RecoveryReason.APPLY_INTERRUPTED_AFTER_WRITE, False,
            id="written_and_unproven_holds"),
        pytest.param(
            {"mutation_outcome": str(MutationOutcome.NOT_WRITTEN),
             "mutation_reason": "precondition_did_not_hold", "host_effect": "none",
             "verification_outcome": str(VerificationOutcome.NOT_ATTEMPTED),
             "verification_observation_id": None, "verification_observed_value": None},
            ChangeResult.FAILED, RunState.FAILED, None, True,
            id="not_written_stays_a_proof"),
        pytest.param(
            {"mutation_outcome": str(MutationOutcome.WRITE_UNKNOWN),
             "mutation_reason": "dispatch_answer_untrustworthy",
             "host_effect": "write_unknown",
             "verification_outcome": str(VerificationOutcome.NOT_ATTEMPTED),
             "verification_observation_id": None, "verification_observed_value": None},
            ChangeResult.RECOVERY_REQUIRED, RunState.RECOVERY_REQUIRED,
            RecoveryReason.APPLY_WRITE_UNKNOWN, False,
            id="write_unknown_stays_unknown"),
    ],
)
def test_a_recorded_mutation_outcome_is_never_rewritten_by_a_settlement(
    ready: Estate, columns: dict[str, Any], result: ChangeResult, state: RunState,
    reason: RecoveryReason | None, released: bool,
):
    """A restart is not entitled to a second opinion about what the dispatch recorded.

    The crash window is wider than the one before the result lands: between the mutation
    result and the end of the Run there is a whole verification, and an operation that
    interrupts LocalPlane itself lands in it by design. What the dispatching process wrote
    there was written by the path that knew.

    Both directions are lies and both are tested: reading a recorded `written` back as
    `write_unknown` destroys something LocalPlane established, and reading a recorded
    `not_written` back as `write_unknown` invents a possible host write that provably did
    not happen — and then holds an object nothing ever touched.

    What the preserved outcome decides is only where the Run ends.
    """
    run = applied(ready).run
    before = ready.changes.change_for_run(run.run_id)
    _interrupt(ready, run, before, **columns)
    expected = ready.changes.get_change(before.change_id)

    settled = ready.changes.settle_interrupted()

    assert [c.change_id for c in settled] == [before.change_id]
    after = ready.changes.get_change(before.change_id)
    # The mutation is untouched in every column, not merely in its outcome.
    assert after.mutation_outcome == expected.mutation_outcome
    assert after.mutation_reason == expected.mutation_reason
    assert after.mutation_reason != "interrupted_before_a_result_was_recorded"
    assert after.mutation_provider == expected.mutation_provider
    assert after.mutation_method == expected.mutation_method
    assert after.mutation_detail == expected.mutation_detail
    assert after.settled_at == expected.settled_at
    assert after.host_effect == str(host_effect_for(MutationOutcome(after.mutation_outcome)))
    assert after.verification_outcome == expected.verification_outcome

    assert after.result == str(result)
    assert after.recovery_reason == (str(reason) if reason else None)
    assert ready.runs.get(run.run_id).state == str(state)
    assert (ready.changes.lock_for(run.run_id) is None) is released
    # No second mutation result is claimed, and the settlement says it preserved one.
    assert ready.events(run.run_id).count(str(RunEvent.MUTATION_RESULT)) == 1
    assert [
        e.detail["mutation_outcome_preserved"]
        for e in ready.changes.transcript(run.run_id)
        if e.event in (str(RunEvent.RUN_FINISHED), str(RunEvent.RECOVERY_REQUIRED))
        and "mutation_outcome_preserved" in e.detail
    ] == [True]
    # Nothing was written to put anything right: a settlement on a restart does not write.
    assert ready.kernel.mutations == [(2, 1500)]


# ------------------------------------------------------------------ what the store refuses


def test_the_store_refuses_a_run_state_that_disagrees_with_what_the_host_did(ready: Estate):
    """The state machine's honesty, where no code has to remember it.

    ``failed`` requires that nothing was written; ``succeeded`` that something was;
    ``rolled_back`` and ``recovery_required`` that something may have been; and nothing before
    the boundary may claim a host effect at all. Two of the fourteen run states are refused
    outright: ``draft``, because creating a Run *is* planning one, and ``guarded``, because a
    guarded change is one to the object carrying the operator's connection and this build
    blocks those.
    """
    run_id = ready.plan("eth0").run.run_id
    for state in ("draft", "guarded"):
        with pytest.raises(sqlite3.IntegrityError):
            ready.database.connection.execute(
                "UPDATE runs SET state = ? WHERE run_id = ?", (state, run_id))
    for state, effect in (
        ("failed", "written"), ("failed", "write_unknown"),   # the rule that matters most
        ("succeeded", "none"), ("succeeded", "write_unknown"),
        ("rolled_back", "none"), ("recovery_required", "none"),
        ("preview", "written"), ("awaiting_confirmation", "written"),
        ("arming", "write_unknown"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            ready.database.connection.execute(
                "UPDATE runs SET state = ?, host_effect = ?, finished_at = ? WHERE run_id = ?",
                (state, effect, _NOW, run_id))


def test_the_store_refuses_a_change_that_claims_more_than_its_outcome_supports(ready: Estate):
    change = applied(ready).change
    for outcome, effect in (("written", "write_unknown"), ("written", "none"),
                            ("write_unknown", "written"), ("not_written", "written")):
        with pytest.raises(sqlite3.IntegrityError):
            ready.database.connection.execute(
                "UPDATE changes SET mutation_outcome = ?, host_effect = ? WHERE change_id = ?",
                (outcome, effect, change.change_id))
    # `failed` after a possible write, and a claim of a write with no dispatch behind it.
    for sql, params in (
        ("UPDATE changes SET result = 'failed', mutation_outcome = 'written', "
         "host_effect = 'written' WHERE change_id = ?", (change.change_id,)),
        ("UPDATE changes SET dispatch_began_at = NULL WHERE change_id = ?", (change.change_id,)),
        ("UPDATE changes SET verification_outcome = 'verified', "
         "verification_observation_id = NULL WHERE change_id = ?", (change.change_id,)),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            ready.database.connection.execute(sql, params)


def test_a_checkpoint_for_a_management_path_target_cannot_be_armed_at_all(ready: Estate):
    """The block is structural, not a policy an evaluator could relax.

    The store accepts only ``not_on_management_path`` on a checkpoint, so a build that decided
    to arm one for the object carrying the operator's connection would fail to write the row
    rather than reach the boundary.
    """
    run = applied(ready).run
    checkpoint = ready.changes.checkpoint_for(run.run_id)
    for relation in ("on_management_path", "unknown"):
        with pytest.raises(sqlite3.IntegrityError):
            with ready.database.transaction():
                ready.changes.checkpoints.insert({
                    "checkpoint_id": f"ckp_{relation}", "run_id": "run_other",
                    "preview_id": checkpoint.preview_id, "host_id": checkpoint.host_id,
                    "object_id": checkpoint.object_id, "intent_id": checkpoint.intent_id,
                    "intent_version": checkpoint.intent_version, "field": MTU,
                    "value_type": "integer", "before_value": 1400, "desired_value": 1500,
                    "observation_id": checkpoint.observation_id,
                    "observed_at": checkpoint.observed_at,
                    "protection_management_path": relation, "protection_evidence_id": None,
                    "execution_correlation": "{}", "armed_at": _NOW})


def test_a_change_cannot_exist_without_a_consumed_confirmation(ready: Estate):
    change = applied(ready).change
    columns = {k: getattr(change, k) for k in
               ("preview_id", "host_id", "object_id", "operation")}
    with pytest.raises(sqlite3.IntegrityError, match="consumed confirmation"):
        with ready.database.transaction():
            ready.changes.changes.insert({
                **columns, "change_id": "chg_forged", "run_id": "run_forged",
                # Shaped as an action so that this row violates the confirmation rule and
                # nothing else: an action names no checkpoint, so the checkpoint rule has
                # nothing to say about it and the refusal can only be the one being tested.
                "change_kind": "action", "action": "start",
                "observed_state": "exited", "expected_state": "running",
                "checkpoint_id": None, "created_at": _NOW,
                "apply_attempt_id": "apl_forged"})


def test_a_change_may_not_name_a_checkpoint_armed_for_another_run(ready: Estate):
    """The rule the NOT NULL foreign key used to imply, now that the column is nullable."""
    change = applied(ready).change
    columns = {k: getattr(change, k) for k in
               ("run_id", "preview_id", "host_id", "object_id", "operation",
                "change_kind", "field", "value_type")}
    with pytest.raises(sqlite3.IntegrityError, match="armed for its own run"):
        with ready.database.transaction():
            # The run and plan are this Run's own, so the confirmation rule is satisfied and
            # the only thing wrong with the row is the checkpoint it names.
            ready.changes.changes.insert({
                **columns, "change_id": "chg_borrowed", "checkpoint_id": "ckp_someone_else",
                "before_value": 1400, "desired_value": 1500, "created_at": _NOW,
                "apply_attempt_id": "apl_borrowed"})


def test_the_transcript_and_the_checkpoint_are_immutable(ready: Estate):
    run = applied(ready).run
    for sql in ("UPDATE run_events SET event = 'run_planned' WHERE run_id = ?",
                "DELETE FROM run_events WHERE run_id = ?"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            ready.database.connection.execute(sql, (run.run_id,))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ready.database.connection.execute(
            "UPDATE run_checkpoints SET before_value = 9000 WHERE run_id = ?", (run.run_id,))


# ---------------------------------------------------------------------- the confirmation


def test_a_confirmation_names_a_run_and_a_preview_not_just_a_digest(ready: Estate):
    """Two identical concurrent plans share a digest. A confirmation must not.

    The store enforces it rather than the code remembering to: a trigger refuses a confirmation
    whose preview is not the one its own Run published, and the digest is kept beside it as
    evidence of *what* was confirmed rather than as the thing that authorises.
    """
    first, second = ready.plan("eth0").run, ready.plan("eth0").run
    assert first.preview.preview_digest == second.preview.preview_digest
    assert first.preview.preview_id != second.preview.preview_id

    confirmation = ready.confirm(first)
    assert confirmation.run_id == first.run_id
    assert confirmation.preview_id == first.preview.preview_id
    assert confirmation.source == "authenticated_request"
    assert confirmation.consumed is False

    # Naming another Run's preview is refused by the service and by the store.
    for digest in (None, first.preview.preview_digest):
        with pytest.raises(RunRefused) as raised:
            ready.confirm(second, preview_id=first.preview.preview_id,
                          expected_preview_digest=digest)
        assert raised.value.code == "confirmation_preview_mismatch"
    with pytest.raises(sqlite3.IntegrityError, match="its own run published"):
        with ready.database.transaction():
            ready.changes.confirmations.insert({
                "confirmation_id": "cnf_forged", "run_id": second.run_id,
                "preview_id": first.preview.preview_id,
                "preview_digest": first.preview.preview_digest,
                "digest_version": first.preview.digest_version,
                "required_method": "acknowledge", "method": "acknowledge", "policy": "p",
                "source": "unauthenticated_request", "satisfied_at": _NOW})

    # And the second Run cannot apply on the strength of the first's confirmation.
    with pytest.raises(RunRefused) as raised:
        ready.apply(second)
    assert raised.value.code == "confirmation_required"
    assert ready.kernel.mutations == []


def test_a_confirmation_is_consumed_once_and_records_no_actor(ready: Estate):
    run = ready.plan("eth0").run
    confirmation = ready.confirm(run)
    row = dict(ready.database.query_one("SELECT * FROM run_confirmations"))
    assert row["source"] == "authenticated_request"
    assert not any(k in row for k in ("actor", "actor_id", "user", "user_id", "token"))
    with pytest.raises(sqlite3.IntegrityError):
        ready.database.connection.execute(
            "UPDATE run_confirmations SET source = 'operator' WHERE run_id = ?", (run.run_id,))

    ready.apply(run)
    consumed = ready.changes.confirmation_for(run.run_id)
    assert consumed.consumed is True and consumed.consumed_by_attempt_id is not None
    with pytest.raises(RunRefused) as raised:
        ready.confirm(run)
    assert raised.value.code == "run_not_confirmable"
    with pytest.raises(sqlite3.IntegrityError, match="single-use"):
        ready.database.connection.execute(
            "UPDATE run_confirmations SET consumed_at = ?, consumed_by_attempt_id = ? "
            "WHERE confirmation_id = ?", (_NOW, "att_second", confirmation.confirmation_id))
    assert ready.changes.confirmations.consume(
        confirmation_id=confirmation.confirmation_id, attempt_id="att_second", now=_NOW) is False


def test_a_run_with_no_confirmation_waits_for_one_and_says_so(ready: Estate):
    """``awaiting_confirmation`` is a real, durable state and this is how it is reached."""
    run = ready.plan("eth0").run
    with pytest.raises(RunRefused) as raised:
        ready.apply(run)
    assert raised.value.code == "confirmation_required"
    waiting = ready.runs.get(run.run_id)
    assert waiting.state == str(RunState.AWAITING_CONFIRMATION)
    assert waiting.host_effect == "none"
    assert ready.changes.change_for_run(run.run_id) is None
    assert ready.events(run.run_id)[-2:] == [
        str(RunEvent.CONFIRMATION_REQUIRED), str(RunEvent.APPLY_REFUSED)]
    ready.confirm(waiting)
    assert ready.apply(waiting).run.state == str(RunState.SUCCEEDED)


# ------------------------------------------------------------------------- the apply gates


def test_apply_takes_no_desired_value_interface_command_or_provider():
    """Not validated away — absent. There is no parameter at any layer to carry one."""
    import dataclasses
    import inspect

    from localplane.backend.domain.changes import MutationRequest
    from localplane.backend.operations import InterfaceMtuExecutor

    # `systemd_lifecycle_context` is evidence, not a value: it is request-scoped safety
    # proof the caller reads from the socket this request arrived on, and an operation whose
    # protection rests on it cannot be re-derived without one. There is still no parameter
    # here for a desired value, an interface, a command or a provider.
    assert set(inspect.signature(ChangeService.apply).parameters) == {
        "self", "run", "management_path", "systemd_lifecycle_context"}
    # The executor takes one typed request and nothing else, and the request's whole field
    # set is enumerated here: there is no command, argv, shell, executable, path, payload,
    # provider, method or timeout on it, at any layer, for any operation.
    assert set(inspect.signature(InterfaceMtuExecutor.mutate).parameters) == {"self", "request"}
    assert {f.name for f in dataclasses.fields(MutationRequest)} == {
        "kind", "attempt_id", "correlation", "expected_current", "desired", "action"}


def test_the_desired_value_comes_from_the_intent_and_from_nowhere_else(ready: Estate):
    """Revise the intent and the write follows it; nothing else can move the target."""
    intent = ready.runs.intents.get(ready.object_named("eth0").active_intent_id)
    ready.management.revise_intent(ready.object_named("eth0"),
                                   expected_intent_id=intent.intent_id, fields={"mtu": 9000})
    ready.prove_path_on("eth1")
    run = ready.plan("eth0").run
    assert run.preview.desired_value == 9000
    ready.confirm(run)
    ready.apply(run)
    assert ready.kernel.mutations == [(2, 9000)]
    assert ready.mtu_of("eth0") == 9000


@pytest.mark.parametrize(
    "disturb, codes",
    [
        # The plan's own two ends moved: somebody set the MTU while the operator decided.
        (lambda e: (e.write("eth0", "mtu", "1300"),
                    e.kernel.links.__setitem__(2, ("eth0", 1300)), e.observe()),
         {"preview_stale"}),
        # Nobody has looked at this host recently enough.
        (lambda e: e.database.connection.execute(
            "UPDATE observations SET observed_at = '2020-01-01T00:00:00+00:00'"),
         {"preview_stale"}),
        # The object stopped being managed.
        (lambda e: e.management.release(e.object_named("eth0")), {"preview_stale"}),
        # A provider started configuring it.
        (lambda e: (e.networkmanager_takes_over_eth0(), e.observe()),
         {"preview_stale", "execution_blocked"}),
        # The host stopped being able to do this at all.
        (lambda e: e.database.connection.execute(
            "UPDATE agent_capabilities SET status = 'unavailable' "
            "WHERE capability = 'network.interface.set_mtu'"),
         {"preview_stale"}),
    ],
)
def test_apply_rechecks_current_truth_and_refuses_when_it_moved(
    ready: Estate, disturb, codes: set[str]
):
    """Everything the plan rested on is re-proved for this request, and nothing is replanned.

    The remedy for a stale Run is a new Run — never a quietly rewritten one — so each of these
    is a refusal with no Change, no dispatch and no host effect.
    """
    run = ready.plan("eth0").run
    ready.confirm(run)
    disturb(ready)
    with pytest.raises(RunRefused) as raised:
        ready.apply(run)
    assert raised.value.code in codes
    assert ready.kernel.mutations == []
    assert ready.changes.change_for_run(run.run_id) is None
    assert ready.runs.get(run.run_id).host_effect == "none"


def test_a_helper_that_disappears_before_the_write_ends_the_run_failed_and_not_written(
    ready: Estate,
):
    """The safest failure there is: a Change that provably wrote nothing.

    The privileged path is gone by the time the mutation is dispatched, so the agent never
    reaches it and answers with a typed negative — a *proof*, from the component that would
    have done the writing. The Change may say ``not_written``, and ``failed`` is honest.
    """
    run = ready.plan("eth0").run
    ready.confirm(run)
    before = ready.link_snapshot()
    ready.helper_server.shutdown()
    ready.helper_server.server_close()

    outcome = ready.apply(run)
    change = outcome.change
    assert outcome.run.state == str(RunState.FAILED)
    assert outcome.run.host_effect == str(HostEffect.NONE)
    assert change is not None, "the boundary was crossed, so a change must exist"
    assert change.mutation_outcome == str(MutationOutcome.NOT_WRITTEN)
    assert change.mutation_reason == "helper_unavailable"
    assert change.result == str(ChangeResult.FAILED)
    assert change.rollback_required is False
    assert change.verification_outcome == str(VerificationOutcome.NOT_ATTEMPTED)
    assert ready.kernel.mutations == []
    assert ready.link_snapshot() == before
    assert ready.changes.lock_for(run.run_id) is None

    # And a Change that wrote nothing resolves no finding.
    findings = FindingRepository(ready.database)
    assert [f.subject for f in findings.open_for_object(run.object_id)] == [MTU]


# ------------------------------------------------------------- verification and rollback


def test_a_kernel_acknowledgement_alone_is_not_success(ready: Estate):
    """The write landed and the object still does not carry the value. Not succeeded.

    Modelled by a kernel that acknowledges and does not move — which is what a provider that
    clamps, a driver that refuses silently, or a second writer immediately afterwards looks
    like from here.
    """
    run = ready.plan("eth0").run
    ready.confirm(run)
    ready.kernel.on_mutate[2] = lambda f: netlink_ack(_sequence(f), errno=0)
    outcome = ready.apply(run)
    assert outcome.change.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert outcome.change.verification_outcome == str(VerificationOutcome.MISMATCH)
    assert outcome.change.verification_observed_value == 1400
    assert outcome.change.result != str(ChangeResult.SUCCEEDED)
    assert outcome.run.state != str(RunState.SUCCEEDED)


def test_a_failed_verification_after_a_written_mutation_rolls_back_and_proves_it(
    ready: Estate,
):
    """Write, fail verification, restore through the same path, read it back.

    Verification is made to fail by a third party moving the value the instant the write lands.
    The restoration goes through the same privileged typed path — not a second executor — and
    ``rolled_back`` is claimed only once a fresh reading proves the checkpoint's value.
    """
    run = ready.plan("eth0").run
    ready.confirm(run)
    outcome = _apply_with_competing_writer(ready, run)
    change = outcome.change

    assert change.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert change.verification_outcome == str(VerificationOutcome.MISMATCH)
    assert change.verification_observed_value == 9000
    assert change.rollback_required is True
    assert change.rollback_attempt_id != change.apply_attempt_id
    assert change.rollback_outcome == str(MutationOutcome.WRITTEN)
    assert change.rollback_verification_outcome == str(VerificationOutcome.VERIFIED)
    assert change.rollback_verification_observed_value == 1400
    assert change.result == str(ChangeResult.ROLLED_BACK)
    assert outcome.run.state == str(RunState.ROLLED_BACK)
    assert ready.mtu_of("eth0") == 1400
    assert ready.kernel.mutations == [(2, 1500), (2, 1400)]
    assert ready.events(run.run_id)[-6:] == [
        str(RunEvent.ROLLBACK_STARTED), str(RunEvent.ROLLBACK_MUTATION_DISPATCHED),
        str(RunEvent.ROLLBACK_MUTATION_RESULT), str(RunEvent.ROLLBACK_VERIFICATION_STARTED),
        str(RunEvent.ROLLBACK_VERIFICATION_RESULT), str(RunEvent.RUN_FINISHED)]


def test_a_write_unknown_apply_never_ends_failed_and_enters_recovery(ready: Estate):
    """Once a write may have happened, ``failed`` is a lie and the store agrees.

    No verification is attempted: reading the value back would answer what the host holds, not
    what LocalPlane did. Restoration is the only safe move, and here it succeeds and is proven.
    """
    run = ready.plan("eth0").run
    ready.confirm(run)

    calls = {"n": 0}
    original = ready.kernel.mutate

    def lose_the_acknowledgement(frame: bytes) -> bytes:
        calls["n"] += 1
        if calls["n"] == 1:
            original(frame)  # the write really lands
            raise NetlinkFailure("acknowledgement_timeout", {"timeout_s": 2.0}, True)
        return original(frame)

    ready.kernel.mutate = lose_the_acknowledgement  # type: ignore[method-assign]
    try:
        outcome = ready.apply(run)
    finally:
        ready.kernel.mutate = original  # type: ignore[method-assign]

    change = outcome.change
    assert change.mutation_outcome == str(MutationOutcome.WRITE_UNKNOWN)
    assert change.host_effect == str(HostEffect.WRITE_UNKNOWN)
    assert change.result != str(ChangeResult.FAILED)
    assert outcome.run.state != str(RunState.FAILED)
    assert change.verification_outcome == str(VerificationOutcome.NOT_ATTEMPTED)
    assert change.rollback_required is True
    assert change.rollback_verification_outcome == str(VerificationOutcome.VERIFIED)
    assert change.result == str(ChangeResult.ROLLED_BACK)
    assert ready.mtu_of("eth0") == 1400


@pytest.mark.parametrize(
    "sabotage, reason",
    [
        # The restoration's own write may or may not have landed.
        ("rollback_unknown", RecoveryReason.ROLLBACK_WRITE_UNKNOWN),
        # It was acknowledged and the value did not move.
        ("rollback_acknowledged_only", RecoveryReason.ROLLBACK_VERIFICATION_FAILED),
        # The privileged path was gone by the time the restoration was attempted.
        ("helper_gone", RecoveryReason.ROLLBACK_NOT_DISPATCHED),
        # The object could not be observed afterwards at all.
        ("object_vanished", RecoveryReason.TARGET_ABSENT_AFTER_MUTATION),
    ],
)
def test_a_restoration_that_cannot_be_proven_ends_in_recovery_required(
    ready: Estate, sabotage: str, reason: RecoveryReason
):
    """A rollback acknowledgement is not a restoration, and the typed reason says which.

    ``recovery_required`` is a truthful ending rather than an error path: LocalPlane cannot
    prove a safe final state after a mutation that may have happened, so it says so and holds
    the object rather than reporting something it did not establish.
    """
    run = ready.plan("eth0").run
    ready.confirm(run)
    calls = {"n": 0}
    original = ready.kernel.mutate

    def sabotaged(frame: bytes) -> bytes:
        calls["n"] += 1
        if calls["n"] == 1:
            reply = original(frame)
            ready.kernel.links[2] = ("eth0", 9000)
            ready.write("eth0", "mtu", "9000")
            if sabotage == "helper_gone":
                ready.helper_server.shutdown()
                ready.helper_server.server_close()
            elif sabotage == "object_vanished":
                ready.kernel.links.pop(2, None)
                shutil.rmtree(ready.sysfs / "eth0")
            return reply
        if sabotage == "rollback_unknown":
            raise NetlinkFailure("acknowledgement_timeout", {"timeout_s": 2.0}, True)
        return netlink_ack(_sequence(frame), errno=0)  # acknowledged, nothing moves

    ready.kernel.mutate = sabotaged  # type: ignore[method-assign]
    try:
        outcome = ready.apply(run)
    finally:
        ready.kernel.mutate = original  # type: ignore[method-assign]

    change = outcome.change
    assert change.mutation_outcome == str(MutationOutcome.WRITTEN)
    assert change.result == str(ChangeResult.RECOVERY_REQUIRED)
    assert change.recovery_required is True
    assert change.recovery_reason == str(reason)
    assert outcome.run.state == str(RunState.RECOVERY_REQUIRED)
    # The object stays held, and a second change against it is refused rather than queued.
    assert ready.changes.lock_for(run.run_id) is not None


def _apply_with_competing_writer(estate: Estate, run: Any) -> Any:
    """Apply with a third party moving the value the instant LocalPlane's write lands."""
    state = {"first": True}
    original = estate.kernel.mutate

    def interfering(frame: bytes) -> bytes:
        reply = original(frame)
        if state["first"]:
            state["first"] = False
            estate.kernel.links[2] = ("eth0", 9000)
            estate.write("eth0", "mtu", "9000")
        return reply

    estate.kernel.mutate = interfering  # type: ignore[method-assign]
    try:
        return estate.apply(run)
    finally:
        estate.kernel.mutate = original  # type: ignore[method-assign]


# ------------------------------------------------------------ concurrency and idempotency


def test_only_one_mutating_run_may_hold_an_object_and_field(ready: Estate):
    """A mutex in one backend serialises one backend and silently permits two.

    The lock is a primary key, so a second writer fails whoever is asking — asserted here
    through a *separate connection* to the same file, which a process-local lock could not
    notice.
    """
    first = ready.plan("eth0").run
    ready.confirm(first)
    assert ready.changes.locks.acquire(host_id=first.host_id, object_id=first.object_id,
                                       field=MTU, run_id=first.run_id, now=_NOW)

    second = ready.plan("eth0").run
    ready.confirm(second)
    with pytest.raises(RunRefused) as raised:
        ready.apply(second)
    assert raised.value.code == "object_write_locked"
    assert raised.value.detail["held_by_run_id"] == first.run_id
    assert ready.kernel.mutations == []

    other = sqlite3.connect(str(ready.database.path))
    try:
        other.execute("PRAGMA foreign_keys=ON")
        for key in (f"{first.host_id}|{first.object_id}|{MTU}", "a-different-key"):
            with pytest.raises(sqlite3.IntegrityError):
                other.execute(
                    "INSERT INTO object_write_locks "
                    "(lock_key, host_id, object_id, field, run_id, acquired_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (key, first.host_id, first.object_id, MTU, "run_other", _NOW))
    finally:
        other.close()


def test_applying_an_already_applied_run_is_refused_rather_than_dispatched_again(
    ready: Estate,
):
    """A network retry after the boundary is where duplicate writes come from."""
    run = applied(ready).run
    assert ready.runs.get(run.run_id).state == str(RunState.SUCCEEDED)
    for _ in range(3):
        with pytest.raises(RunRefused) as raised:
            ready.apply(ready.runs.get(run.run_id))
        assert raised.value.code == "run_not_appliable"
    assert ready.kernel.mutations == [(2, 1500)], "a retry became a second write"
    assert len(ready.rows("changes")) == 1


# ---------------------------------------------------------------------------- cancellation


def test_cancelling_before_the_boundary_leaves_no_change(ready: Estate):
    for confirm_first in (False, True):
        run = ready.plan("eth0").run
        if confirm_first:
            ready.confirm(run)
        cancelled = ready.runs.cancel(run)
        assert cancelled.state == str(RunState.CANCELLED)
        assert cancelled.host_effect == "none"
        assert ready.changes.change_for_run(run.run_id) is None
        assert str(RunEvent.RUN_CANCELLED) in ready.events(run.run_id)
    # And from `awaiting_confirmation`, which is still before the boundary.
    waiting = ready.plan("eth0").run
    with pytest.raises(RunRefused):
        ready.apply(waiting)
    assert ready.runs.cancel(ready.runs.get(waiting.run_id)).state == str(RunState.CANCELLED)
    assert ready.rows("changes") == []
    assert ready.kernel.mutations == []


@pytest.mark.parametrize(
    "state", ["arming", "applying", "verifying", "rolling_back", "rollback_verifying"])
def test_cancelling_during_an_apply_is_refused_rather_than_pretended(
    ready: Estate, state: str
):
    """Cancelling after the boundary would have to answer what it interrupted.

    The effect of an interrupted step is unknown, and an interrupted rollback is not a
    rollback. Neither is designed here, so cancellation is *refused* — setting
    ``state = 'cancelled'`` over a mutation that may have happened is the lie the write
    boundary exists to make unstorable, and the schema agrees.
    """
    run = ready.plan("eth0").run
    ready.confirm(run)
    with ready.database.transaction():
        ready.runs.runs.set_state(run_id=run.run_id, state=state)
    with pytest.raises(RunRefused) as raised:
        ready.runs.cancel(ready.runs.get(run.run_id))
    assert raised.value.code == "run_not_cancellable"
    assert ready.runs.get(run.run_id).state == state


def test_a_run_that_may_have_written_can_never_become_cancelled(ready: Estate):
    run = ready.plan("eth0").run
    ready.confirm(run)
    ready.kernel.on_mutate[2] = lambda f: netlink_ack(_sequence(f), errno=0)
    _apply_with_competing_writer(ready, run)
    with pytest.raises(RunRefused):
        ready.runs.cancel(ready.runs.get(run.run_id))
    with pytest.raises(sqlite3.IntegrityError):
        ready.database.connection.execute(
            "UPDATE runs SET state = 'cancelled', cancelled_at = ? WHERE run_id = ?",
            (_NOW, run.run_id))


# ------------------------------------------------------------ old runs and old previews


def test_a_preview_published_before_execution_existed_cannot_be_applied(ready: Estate):
    """A Run planned by an earlier build stays readable and stays non-executable.

    Its preview says ``not_implemented`` and ``blocked``, which is what its operator was shown,
    and the plan an operator was shown is the plan that may be executed. Nothing rewrites it
    into a newer opinion — the refusal comes from the document itself. And the document is
    immutable: reproducing one here needs the trigger dropped, which no product code can do.
    """
    run = ready.plan("eth0").run
    ready.confirm(run)
    ready.database.connection.execute("DROP TRIGGER run_previews_are_immutable")
    try:
        ready.database.connection.execute(
            "UPDATE run_previews SET execution_availability = 'not_implemented', "
            "execution_eligibility = 'blocked', execution_provider = NULL "
            "WHERE preview_id = ?", (run.preview.preview_id,))
    finally:
        ready.database.connection.execute(
            "CREATE TRIGGER run_previews_are_immutable BEFORE UPDATE ON run_previews "
            "BEGIN SELECT RAISE(ABORT, 'a published preview is immutable; plan again "
            "instead of rewriting'); END")

    with pytest.raises(RunRefused) as raised:
        ready.apply(ready.runs.get(run.run_id))
    assert raised.value.code == "preview_not_executable"
    assert ready.kernel.mutations == []
    assert ready.changes.change_for_run(run.run_id) is None


def test_applying_neither_rewrites_the_plan_it_applied_nor_allows_one_to_be_rewritten(
    ready: Estate,
):
    run = ready.plan("eth0").run
    published = dict(ready.database.query_one(
        "SELECT * FROM run_previews WHERE preview_id = ?", (run.preview.preview_id,)))
    ready.confirm(run)
    ready.apply(run)
    assert dict(ready.database.query_one(
        "SELECT * FROM run_previews WHERE preview_id = ?",
        (run.preview.preview_id,))) == published
    for column, value in (("execution_availability", "available"), ("desired_value", 9000),
                          ("risk_tier", "low")):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            ready.database.connection.execute(
                f"UPDATE run_previews SET {column} = ? WHERE preview_id = ?",
                (value, run.preview.preview_id))


# --------------------------------------------------------------------- migration 0007


def test_the_first_six_migrations_are_byte_identical():
    """A migration that changed after it was applied is a hard failure, not a repair.

    Read as bytes against what ``HEAD`` holds, because a checksum test would pass on two files
    that differ in a way the hash happens not to see — and these six are what every store in
    existence was built from.
    """
    import subprocess  # noqa: S404 - reading git's own record of these files

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.stem.split("_")[0]) > 6:
            continue
        committed = subprocess.run(
            ["git", "show", f"HEAD:src/localplane/backend/db/migrations/{path.name}"],
            cwd=str(Path(__file__).resolve().parents[1]), capture_output=True)
        if committed.returncode != 0:
            pytest.skip("not a git checkout")
        assert path.read_bytes() == committed.stdout, f"{path.name} changed on disk"


def test_upgrading_a_0006_store_keeps_its_data_its_checksums_and_its_schema(tmp_path: Path):
    """The staged upgrade, end to end: nothing lost, nothing invented, nothing diverged."""
    staged = tmp_path / "at_0006"
    staged.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.stem.split("_")[0]) <= 6:
            shutil.copy(path, staged / path.name)

    old = open_database(tmp_path / "staged.db", staged)
    _seed_a_pre_write_store(old)
    before = {r["version"]: r["checksum"] for r in old.query("SELECT * FROM schema_migrations")}
    assert sorted(before) == [1, 2, 3, 4, 5, 6]
    old.close()

    upgraded, fresh = open_database(tmp_path / "staged.db"), open_database(tmp_path / "fresh.db")
    try:
        after = {r["version"]: r["checksum"]
                 for r in upgraded.query("SELECT * FROM schema_migrations")}
        assert sorted(after) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
        assert {v: before[v] for v in before} == {v: after[v] for v in before}

        # Old rows survive unchanged, and the previews still say what they said.
        preview = dict(upgraded.query_one("SELECT * FROM run_previews WHERE preview_id='prv_old'"))
        assert preview["execution_availability"] == "not_implemented"
        assert preview["execution_eligibility"] == "blocked"
        assert preview["execution_provider"] is None
        assert preview["preview_digest"] == "sha256:old"
        runs = {r["run_id"]: dict(r) for r in upgraded.query("SELECT * FROM runs")}
        assert runs["run_old"]["state"] == "preview"
        assert runs["run_old"]["host_effect"] == "none"
        assert runs["run_old"]["finished_at"] is None
        # A run cancelled before this migration finished when it was cancelled.
        assert runs["run_cancelled"]["finished_at"] == runs["run_cancelled"]["cancelled_at"]

        # The new tables exist and are empty: an upgrade invents no history.
        for table in ("changes", "run_checkpoints", "run_confirmations", "run_events",
                      "object_write_locks", "change_recovery_attempts"):
            assert upgraded.query(f"SELECT * FROM {table}") == [], table

        assert upgraded.query("PRAGMA foreign_key_check") == []
        assert upgraded.query("PRAGMA integrity_check")[0][0] == "ok"

        def schema_of(database) -> list[str]:
            return sorted(" ".join(row["sql"].split())
                          for row in database.query("SELECT sql FROM sqlite_master") if row["sql"])

        assert schema_of(upgraded) == schema_of(fresh)
    finally:
        upgraded.close()
        fresh.close()


def _seed_a_pre_write_store(database) -> None:
    """A 0001–0006 store holding a published plan, a Run, and a cancelled Run."""
    connection = database.connection
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO hosts (host_id, identity_basis, identity_confidence, hostname, "
        "first_seen_at, last_seen_at) VALUES ('h','machine_id','high','fixture','t','t')")
    connection.execute(
        "INSERT INTO objects (object_id, host_id, kind, identity_basis, identity_value, "
        "identity_confidence, display_name, management_state, management_reason, "
        "active_intent_id, first_seen_at, last_seen_at) VALUES "
        "('o','h','network.interface','kernel_name','eth0','low','eth0','observed','seen',"
        "NULL,'t','t')")
    connection.execute(
        "INSERT INTO observation_sweeps (sweep_id, host_id, agent_instance_id, capability, "
        "provider, provider_version, status, started_at, completed_at, received_at, "
        "object_count, missing, issues) VALUES ('s','h',NULL,'network.observe',"
        "'linux.network','1','ok','t','t','t',1,'[]','[]')")
    connection.execute(
        "INSERT INTO observations (observation_id, sweep_id, host_id, object_id, capability, "
        "provider, provider_version, method, fidelity, observed_at, received_at, "
        "health_state, health_reason, gaps, facts, evidence) VALUES "
        "('ob','s','h','o','network.observe','linux.network','1','sysfs','complete','t','t',"
        "'healthy','up','[]','{\"mtu\":1400}','{}')")
    connection.execute(
        "INSERT INTO intents (intent_id, object_id, host_id, version, supersedes, "
        "schema_version, origin, capability, provider, provider_version, observation_id, "
        "sweep_id, observed_at, created_at) VALUES ('i','o','h',1,NULL,1,'adopt',"
        "'network.observe','linux.network','1','ob','s','t','t')")
    connection.execute(
        "UPDATE objects SET management_state='managed', active_intent_id='i' WHERE object_id='o'")
    columns = [row[1] for row in connection.execute("PRAGMA table_info(run_previews)")]
    values = {
        "preview_id": "prv_old", "preview_digest": "sha256:old", "digest_version": 2,
        "operation": "network.interface.reconcile_mtu", "field": "mtu",
        "value_type": "integer", "current_value": 1400, "desired_value": 1500,
        "intent_id": "i", "intent_version": 1, "intent_capability": "network.observe",
        "intent_provider": "linux.network", "observation_id": "ob", "sweep_id": "s",
        "observed_at": "t", "drift_finding_id": None, "ownership_state": "localplane",
        "ownership_reason": "host_kernel_only", "ownership_claims": "[]",
        "ownership_gaps": "[]", "provider_readings": "{}", "protection_status": "unknown",
        "protection_reasons": "[]", "protection_unresolved": '["management_path"]',
        "protection_management_path": "unknown",
        "protection_reason": "management_path_unobserved",
        "protection_missing_evidence": "[]", "protection_evidence_id": None,
        "protection_evidence_observed_at": None, "risk_tier": "medium", "risk_factors": "[]",
        "confirmation_required": 1, "confirmation_method": "typed",
        "confirmation_source": "policy", "confirmation_reasons": "[]",
        "confirmation_policy": "p", "confirmation_token_issued": 0,
        "execution_availability": "not_implemented", "execution_eligibility": "blocked",
        "execution_blockers": '["execution_not_implemented"]', "execution_provider": None,
        "required_capability": "network.interface.set_mtu", "capability_declared": 0,
        "recovery_mode": "auto", "recovery_rollback_possible": 1, "recovery_armed": 0,
        "recovery_guarantee": "none", "recovery_reason": "r",
        "verification_capability": "network.observe", "verification_provider": "linux.network",
        "verification_condition": "c", "verification_executed": 0, "published_at": "t",
    }
    assert set(columns) == set(values), set(columns) ^ set(values)
    for preview_id in ("prv_old", "prv_cancelled"):
        row = dict(values, preview_id=preview_id)
        connection.execute(
            f"INSERT INTO run_previews ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})", tuple(row[c] for c in columns))
    for run_id, preview_id, state, cancelled in (
        ("run_old", "prv_old", "preview", None),
        ("run_cancelled", "prv_cancelled", "cancelled", "2026-08-01T00:00:00+00:00"),
    ):
        connection.execute(
            "INSERT INTO runs (run_id, host_id, object_id, operation, state, preview_id, "
            "host_effect, created_at, cancelled_at) VALUES (?,'h','o',"
            "'network.interface.reconcile_mtu',?,?,'none','t',?)",
            (run_id, state, preview_id, cancelled))
    connection.execute("COMMIT")


def _sequence(frame: bytes) -> int:
    from localplane.helper.mtu import _NLMSGHDR

    return _NLMSGHDR.unpack_from(frame, 0)[3]


def _truncated_ack(sequence: int) -> bytes:
    """A netlink header claiming a length the datagram does not have."""
    from localplane.helper.mtu import NLMSG_ERROR, _NLMSGHDR

    return _NLMSGHDR.pack(4096, NLMSG_ERROR, 0, sequence, 0)
