"""The management path: proving it, refusing to guess it, and never reusing somebody else's.

Three claims are asserted over and over, because they are the whole of this behaviour:

* **it is proven from the transport, or it is unknown** — never from a name, a default
  route, a shape, a header or a claim in a body;
* **evidence belongs to one connection** — a proof taken from a remote operator's session
  answers for that session and no other, and localhost inherits nothing;
* **disagreement resolves nothing** — when the local endpoint says one object and the
  kernel's route says another, neither wins.

Everything runs through the real agent service, the real route provider and the real store.
What is scripted is the netlink datagram exchange, through the same kind of seam the other
providers use for their commands — so a host with no route, a kernel that refuses and an
agent that is not running are reachable every run rather than only on a machine that
happens to be broken.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from localplane.agent.service import AgentService
from localplane.backend.agent_client import AgentError
from localplane.backend.db.repositories import ManagementPathRepository
from localplane.backend.domain.identity import OBJECT_KIND_NETWORK_INTERFACE
from localplane.backend.domain.management_path import (
    MANAGEMENT_PATH_EVIDENCE,
    ManagementPathReason,
    read_transport,
)
from localplane.backend.domain.protection import (
    ManagementPathRelation,
    ProtectionReason,
    ProtectionStatus,
    assess_resource_protection,
)
from localplane.backend.ingest import Ingestor
from localplane.backend.management import ManagementService
from localplane.backend.management_path import ManagementPathService
from localplane.backend.provenance import ProvenanceService
from localplane.protocol.wire import ErrorCode, ProtocolError
from tests.conftest import FakeRouteQuery, FakeRunner, json_result, netlink_error, netlink_route
from tests.conftest import write_interface

def _provider_argv() -> frozenset:
    """The fixed argv the ownership providers use. Nothing here takes an argument."""
    from localplane.agent.providers.network_manager import DEVICE_ARGV, GENERAL_ARGV
    from localplane.agent.providers.tailscale import STATUS_ARGV

    return frozenset({DEVICE_ARGV, GENERAL_ARGV, STATUS_ARGV})


_PROVIDER_ARGV = _provider_argv()

OPERATOR = "192.0.2.130"
ENDPOINT = "192.0.2.215"
BRIDGE_ADDRESS = "172.19.0.1"


# --------------------------------------------------------------------------------- estate


@dataclass
class DirectAgentClient:
    """The backend's client, wired straight to an agent service in this process.

    The socket is exercised in ``test_agent_server.py`` and against the real thing in
    ``test_live_host.py``; what these tests need is the agent's real dispatch, its real
    capability gate and its real provider, without a thread per test.
    """

    service: AgentService
    calls: list[dict[str, Any]] = field(default_factory=list)
    unavailable: AgentError | None = None

    def observe_route(self, destination: str) -> dict[str, Any]:
        self.calls.append({"destination": destination})
        if self.unavailable is not None:
            raise self.unavailable
        try:
            return self.service.handle(
                "network.observe_route", {"destination": destination}
            )
        except ProtocolError as exc:
            raise AgentError(exc.code, exc.message, exc.detail) from exc


@dataclass
class Estate:
    database: Any
    sysfs: Path
    runner: FakeRunner
    routes: FakeRouteQuery
    service: AgentService
    client: DirectAgentClient
    ingestor: Ingestor
    paths: ManagementPathService
    host_id: str = ""

    def observe(self) -> Any:
        payload = self.service.handle("network.observe_interfaces", {})
        result = self.ingestor.ingest_network_sweep({**payload, "providers": None})
        self.host_id = result.host_id
        return result

    def object_named(self, name: str) -> Any:
        for record in self.ingestor.objects.list_by_kind(
            self.host_id, OBJECT_KIND_NETWORK_INTERFACE
        ):
            if record.display_name == name:
                return record
        raise AssertionError(f"no object named {name}")

    def id_of(self, name: str) -> str:
        return self.object_named(name).object_id

    def transport(self, peer: str | None = OPERATOR, local: str | None = ENDPOINT):
        return read_transport(peer, local)

    def observe_path(self, peer: str | None = OPERATOR, local: str | None = ENDPOINT):
        return self.paths.observe(self.host_id, self.transport(peer, local))

    def assess(self, peer: str | None = OPERATOR, local: str | None = ENDPOINT):
        return self.paths.assess(self.host_id, self.transport(peer, local))

    def rows(self) -> list[tuple]:
        return [
            tuple(r)
            for r in self.database.query(
                "SELECT * FROM management_path_observations ORDER BY rowid"
            )
        ]

    def age_interface_observations(self, seconds: int = 3600) -> None:
        """Backdate every interface observation, leaving path evidence where it is.

        The one state that cannot be reached by construction: evidence about a connection
        that is current, resting on a reading of the object that is not.
        """
        from datetime import datetime, timedelta, timezone

        when = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(
            timespec="microseconds"
        )
        self.database.connection.execute(
            "UPDATE observations SET observed_at = ?", (when,)
        )

    def with_ttl(self, ttl_s: float) -> ManagementPathService:
        return ManagementPathService(self.database, self.client, ttl_s)


def addresses(*entries: tuple[int, str, list[str]]) -> Any:
    from localplane.agent.providers.linux_network import ADDR_ARGV

    return json_result(
        ADDR_ARGV,
        [
            {
                "ifindex": index,
                "ifname": name,
                "addr_info": [
                    {"family": "inet", "local": a, "prefixlen": 24, "scope": "global"}
                    for a in listed
                ],
            }
            for index, name, listed in entries
        ],
    )


@pytest.fixture
def estate(database, fake_root: Path, sysfs_net: Path, tmp_path: Path) -> Estate:
    """Four links: loopback, an unaddressed ethernet, the one the operator reaches, a bridge.

    ``eth1`` carries ``192.0.2.215`` and has kernel index 3, and the scripted kernel routes
    the operator's address out of index 3. That agreement is the only thing that can confirm
    a management path, and every test that wants it unconfirmed breaks exactly one half.
    """
    from localplane.agent.providers.linux_network import ADDR_ARGV, LINK_ARGV

    write_interface(
        sysfs_net, "lo", ifindex=1, address="00:00:00:00:00:00", arphrd="772", flags="0x9",
        operstate="unknown", carrier="1", mtu="65536", speed=None, duplex=None,
    )
    write_interface(
        sysfs_net, "eth0", ifindex=2, address="02:00:00:00:00:10", flags="0x1003",
        operstate="up", carrier="1", mtu="1500", device="fd580000.ethernet",
        subsystem="platform",
    )
    write_interface(
        sysfs_net, "eth1", ifindex=3, address="02:00:00:00:00:12", flags="0x1003",
        operstate="up", carrier="1", mtu="1500", speed="1000", duplex="full",
        device="1-1.4:1.0", subsystem="usb",
    )
    write_interface(
        sysfs_net, "br0", ifindex=4, address="02:00:00:00:00:13", addr_assign_type="3",
        flags="0x1003", operstate="up", carrier="1", mtu="1500", devtype="bridge",
        bridge=True, speed=None, duplex=None,
    )

    runner = FakeRunner(
        {
            LINK_ARGV: json_result(
                LINK_ARGV,
                [
                    {"ifindex": 2, "ifname": "eth0"},
                    {"ifindex": 3, "ifname": "eth1"},
                    {"ifindex": 4, "ifname": "br0", "linkinfo": {"info_kind": "bridge"}},
                ],
            ),
            ADDR_ARGV: addresses(
                (1, "lo", []),
                (2, "eth0", []),
                (3, "eth1", [ENDPOINT]),
                (4, "br0", [BRIDGE_ADDRESS]),
            ),
        }
    )
    routes = FakeRouteQuery(
        {
            OPERATOR: netlink_route(
                destination=OPERATOR, oif_index=3, preferred_source=ENDPOINT
            ),
            "127.0.0.1": netlink_route(
                destination="127.0.0.1", oif_index=1, preferred_source="127.0.0.1",
                route_type=2,
            ),
        }
    )
    service = AgentService(
        root=fake_root,
        sysfs_net=sysfs_net,
        runner=runner,
        docker_socket=tmp_path / "docker-absent.sock",
        route_query=routes,
    )
    client = DirectAgentClient(service)
    provenance = ProvenanceService(database)
    management = ManagementService(database, freshness_ttl_s=60.0, provenance=provenance)
    ingestor = Ingestor(database, management)
    ingestor.ingest_handshake(service.handle("agent.hello", {}))
    estate = Estate(
        database=database,
        sysfs=sysfs_net,
        runner=runner,
        routes=routes,
        service=service,
        client=client,
        ingestor=ingestor,
        paths=ManagementPathService(database, client, 60.0),
    )
    estate.observe()
    # Constructing the agent probed the route capability against its fixed loopback
    # destination. That is a real call and it belongs in the capability tests; here it
    # would just be noise in front of every "what did this ask the kernel" assertion.
    estate.routes.requests.clear()
    estate.routes.frames.clear()
    estate.client.calls.clear()
    return estate


# ---------------------------------------------------------------- transport classification


def test_a_remote_peer_and_a_remote_endpoint_are_usable():
    transport = read_transport(OPERATOR, ENDPOINT)
    assert transport.usable
    assert transport.peer_address == OPERATOR
    assert transport.local_address == ENDPOINT
    assert transport.peer_family == transport.local_family == "inet"


@pytest.mark.parametrize("peer", ["127.0.0.1", "127.0.0.53", "::1"])
def test_a_loopback_peer_can_never_establish_a_remote_management_path(peer: str):
    """The 2026-07-22 lockout in one assertion: localhost is not an operator."""
    transport = read_transport(peer, ENDPOINT)
    assert not transport.usable
    assert transport.unusable_reason == ManagementPathReason.TRANSPORT_PEER_LOCAL


@pytest.mark.parametrize("local", ["127.0.0.1", "::1"])
def test_a_loopback_local_endpoint_establishes_nothing_either(local: str):
    """A connection that terminated on loopback did not arrive over any interface."""
    transport = read_transport(OPERATOR, local)
    assert not transport.usable
    assert transport.unusable_reason == ManagementPathReason.LOCAL_ENDPOINT_LOCAL


def test_a_missing_peer_is_unavailable_and_not_a_default():
    transport = read_transport(None, ENDPOINT)
    assert transport.unusable_reason == ManagementPathReason.TRANSPORT_PEER_UNAVAILABLE
    assert transport.peer_address is None


def test_a_missing_local_endpoint_is_unavailable():
    transport = read_transport(OPERATOR, None)
    assert transport.unusable_reason == ManagementPathReason.LOCAL_ENDPOINT_UNAVAILABLE


@pytest.mark.parametrize("value", ["testclient", "testserver", "eth0", "not-an-address", ""])
def test_an_unusable_peer_address_is_a_typed_refusal_not_an_exception(value: str):
    transport = read_transport(value, ENDPOINT)
    assert not transport.usable
    assert transport.unusable_reason in {
        ManagementPathReason.TRANSPORT_PEER_UNPARSEABLE,
        ManagementPathReason.TRANSPORT_PEER_UNAVAILABLE,
    }


def test_an_unusable_local_endpoint_is_a_typed_refusal():
    assert (
        read_transport(OPERATOR, "testserver").unusable_reason
        == ManagementPathReason.LOCAL_ENDPOINT_UNPARSEABLE
    )


def test_a_wildcard_endpoint_is_not_a_place():
    assert (
        read_transport(OPERATOR, "0.0.0.0").unusable_reason
        == ManagementPathReason.LOCAL_ENDPOINT_UNSPECIFIED
    )
    assert (
        read_transport("0.0.0.0", ENDPOINT).unusable_reason
        == ManagementPathReason.TRANSPORT_PEER_UNSPECIFIED
    )


def test_a_link_local_address_is_refused_because_its_scope_is_not_carried():
    """A route lookup for one returns whichever interface the kernel picked, not the one used."""
    assert (
        read_transport("fe80::1", ENDPOINT).unusable_reason
        == ManagementPathReason.TRANSPORT_PEER_LINK_LOCAL
    )
    assert (
        read_transport(OPERATOR, "169.254.1.1").unusable_reason
        == ManagementPathReason.LOCAL_ENDPOINT_LINK_LOCAL
    )


def test_mismatched_families_cannot_both_describe_one_connection():
    assert (
        read_transport(OPERATOR, "2001:db8::1").unusable_reason
        == ManagementPathReason.ADDRESS_FAMILY_MISMATCH
    )


def test_an_ipv4_mapped_peer_is_normalised_to_ipv4():
    """A server bound to [::] reports every v4 peer this way. It is the same address."""
    transport = read_transport(f"::ffff:{OPERATOR}", f"::ffff:{ENDPOINT}")
    assert transport.usable
    assert transport.peer_address == OPERATOR
    assert transport.local_address == ENDPOINT
    assert transport.peer_family == "inet"


# ------------------------------------------------------------------------- confirming it


def test_agreement_between_the_endpoint_and_the_route_confirms_the_path(estate: Estate):
    """Two independent sources, and only their agreement is proof."""
    verdict = estate.observe_path()
    assert verdict.confirmed
    assert verdict.resource_id == estate.id_of("eth1")
    assert verdict.reason == ManagementPathReason.CONFIRMED
    assert verdict.evidence_id is not None
    assert verdict.missing_evidence == ()


def test_the_stored_evidence_is_what_made_the_answer_believable(estate: Estate):
    verdict = estate.observe_path()
    evidence = estate.paths.evidence(verdict)
    assert evidence is not None
    assert evidence.transport_peer_address == OPERATOR
    assert evidence.local_endpoint_address == ENDPOINT
    assert evidence.route.status == "resolved"
    assert evidence.route.oif_index == 3
    assert evidence.route.preferred_source == ENDPOINT
    assert evidence.provider == "linux.route"
    assert evidence.method == "netlink_rtm_getroute"
    assert evidence.capability == "network.route.observe"


def test_the_stored_evidence_holds_no_conclusion(estate: Estate):
    """No object id, no "protected", no verdict. Those are derived and can go stale."""
    estate.observe_path()
    row = dict(estate.database.query("SELECT * FROM management_path_observations")[0])
    assert estate.id_of("eth1") not in str(row)
    for column in row:
        assert "object" not in column
        assert "protect" not in column
        assert "management_path" not in column


def test_the_route_lookup_is_asked_about_the_peer_and_nothing_else(estate: Estate):
    estate.observe_path()
    assert estate.routes.requests == [OPERATOR]
    assert estate.client.calls == [{"destination": OPERATOR}]


def test_the_peer_address_never_reaches_a_command_line(estate: Estate):
    """The reason the lookup is netlink and not ``ip route get``, asserted end to end.

    The command seam records every argv the agent ever builds. An address that arrived on a
    socket must not appear in any of them, and the set of commands must be exactly the two
    fixed read-only ones the interface provider was already running.
    """
    from localplane.agent.providers.linux_network import ADDR_ARGV, LINK_ARGV

    estate.observe_path()
    for argv in estate.runner.calls:
        assert OPERATOR not in " ".join(argv)
        assert ENDPOINT not in " ".join(argv)
        assert not ({"set", "add", "del", "delete", "change", "flush", "get"} & set(argv))
    assert set(estate.runner.calls) <= {LINK_ARGV, ADDR_ARGV} | _PROVIDER_ARGV


def test_a_route_lookup_runs_no_command_at_all(estate: Estate):
    before = list(estate.runner.calls)
    estate.observe_path()
    assert estate.runner.calls == before


def test_a_confirmed_path_is_re_derived_and_not_replayed(estate: Estate):
    """Reading again reaches the same answer from the same evidence, contacting nothing."""
    estate.observe_path()
    before = len(estate.routes.requests)
    again = estate.assess()
    assert again.confirmed
    assert len(estate.routes.requests) == before


# ------------------------------------------------------------- refusing to guess it


def test_a_local_endpoint_on_no_object_is_unmapped(estate: Estate):
    verdict = estate.observe_path(local="10.9.9.9")
    assert not verdict.confirmed
    assert verdict.reason == ManagementPathReason.LOCAL_ENDPOINT_UNMAPPED


def test_a_local_endpoint_on_two_objects_is_ambiguous(estate: Estate):
    """A duplicated address is a real state on a real host, and picking one is a coin toss."""
    from localplane.agent.providers.linux_network import ADDR_ARGV

    estate.runner.responses[ADDR_ARGV] = addresses(
        (1, "lo", []),
        (2, "eth0", [ENDPOINT]),
        (3, "eth1", [ENDPOINT]),
        (4, "br0", [BRIDGE_ADDRESS]),
    )
    estate.observe()
    verdict = estate.observe_path()
    assert not verdict.confirmed
    assert verdict.reason == ManagementPathReason.LOCAL_ENDPOINT_AMBIGUOUS


def test_a_route_leaving_a_different_object_is_a_conflict_and_neither_side_wins(
    estate: Estate,
):
    """The rule the whole model turns on: disagreement resolves nothing."""
    estate.routes.replies[OPERATOR] = netlink_route(
        destination=OPERATOR, oif_index=4, preferred_source=BRIDGE_ADDRESS
    )
    verdict = estate.observe_path()
    assert not verdict.confirmed
    assert verdict.reason == ManagementPathReason.ROUTE_CONFLICTS_WITH_LOCAL_ENDPOINT
    assert verdict.resource_id is None
    # Neither candidate is named as the answer anywhere.
    assert estate.id_of("eth1") != verdict.resource_id
    assert estate.id_of("br0") != verdict.resource_id


def test_a_route_out_of_an_index_no_object_carries_is_unmapped(estate: Estate):
    estate.routes.replies[OPERATOR] = netlink_route(destination=OPERATOR, oif_index=99)
    verdict = estate.observe_path()
    assert verdict.reason == ManagementPathReason.ROUTE_INTERFACE_UNMAPPED


def test_a_route_with_no_egress_at_all_settles_nothing(estate: Estate):
    estate.routes.replies[OPERATOR] = netlink_route(destination=OPERATOR)
    verdict = estate.observe_path()
    assert verdict.reason == ManagementPathReason.ROUTE_EGRESS_UNSPECIFIED


def test_an_unreachable_peer_leaves_the_path_unresolved(estate: Estate):
    import errno

    estate.routes.replies[OPERATOR] = netlink_error(errno.ENETUNREACH)
    verdict = estate.observe_path()
    assert verdict.reason == ManagementPathReason.ROUTE_UNREACHABLE


def test_a_failed_lookup_leaves_the_path_unresolved(estate: Estate):
    import errno

    estate.routes.replies[OPERATOR] = netlink_error(errno.ENOMEM)
    verdict = estate.observe_path()
    assert verdict.reason == ManagementPathReason.ROUTE_LOOKUP_FAILED


def test_an_agent_that_cannot_be_reached_leaves_the_path_unresolved(estate: Estate):
    """The agent being down is one source being unavailable, not a server error."""
    estate.client.unavailable = AgentError(
        ErrorCode.AGENT_UNAVAILABLE, "no agent socket", {"socket": "/nowhere"}
    )
    verdict = estate.observe_path()
    assert verdict.reason == ManagementPathReason.ROUTE_LOOKUP_UNAVAILABLE
    evidence = estate.paths.evidence(verdict)
    assert evidence is not None
    assert evidence.route.status == "unavailable"
    assert evidence.route.reason == "agent_unavailable"
    assert evidence.route.oif_index is None


def test_an_agent_without_the_route_capability_leaves_the_path_unresolved(
    estate: Estate, tmp_path: Path, fake_root: Path
):
    """An agent that declares no route capability refuses, and the refusal is the reason."""
    from localplane.agent.providers.linux_route import NetlinkUnavailable

    estate.client.service = AgentService(
        root=fake_root,
        sysfs_net=estate.sysfs,
        runner=estate.runner,
        docker_socket=tmp_path / "docker-absent.sock",
        route_query=FakeRouteQuery(default=NetlinkUnavailable("netlink_unavailable")),
    )
    verdict = estate.observe_path()
    assert verdict.reason == ManagementPathReason.ROUTE_LOOKUP_UNAVAILABLE


def test_a_stale_interface_observation_cannot_prove_where_a_connection_lands(
    estate: Estate,
):
    """An address recorded an hour ago is not evidence that it is there now."""
    estate.observe_path()
    estate.age_interface_observations()
    verdict = estate.assess()
    assert not verdict.confirmed
    assert verdict.reason == ManagementPathReason.INTERFACE_OBSERVATION_STALE


def test_stale_path_evidence_stops_proving_anything(estate: Estate):
    """Time-sensitive by nature: a route and an address can both move at any moment."""
    estate.observe_path()
    assert estate.assess().confirmed
    expired = estate.with_ttl(0.0)
    verdict = expired.assess(estate.host_id, estate.transport())
    assert not verdict.confirmed
    assert verdict.reason == ManagementPathReason.EVIDENCE_STALE
    assert verdict.evidence_id is not None, "the aged evidence is still named"


def test_the_evidence_horizon_is_the_one_every_other_observation_uses(estate: Estate):
    from localplane.backend.domain.states import DEFAULT_FRESHNESS_TTL_S

    assert estate.paths.freshness_ttl_s == 60.0 == DEFAULT_FRESHNESS_TTL_S


def test_nothing_observed_yet_is_its_own_reason(estate: Estate):
    verdict = estate.assess()
    assert not verdict.confirmed
    assert verdict.reason == ManagementPathReason.UNOBSERVED
    assert set(verdict.missing_evidence) == set(MANAGEMENT_PATH_EVIDENCE)


def test_an_unusable_transport_never_reaches_the_kernel_or_the_store(estate: Estate):
    verdict = estate.observe_path(peer="127.0.0.1")
    assert verdict.reason == ManagementPathReason.TRANSPORT_PEER_LOCAL
    assert estate.routes.requests == []
    assert estate.client.calls == []
    assert estate.rows() == []


# ------------------------------------------------------- evidence belongs to one connection


def test_one_peers_proof_is_not_another_peers_proof(estate: Estate):
    """Operator A proving the path says nothing about operator B."""
    assert estate.observe_path(peer=OPERATOR).confirmed
    other = estate.assess(peer="192.0.2.131")
    assert not other.confirmed
    assert other.reason == ManagementPathReason.UNOBSERVED


def test_a_remote_proof_is_not_inherited_by_localhost(estate: Estate):
    """The exact substitution the lockout was made of, refused."""
    assert estate.observe_path().confirmed
    local = estate.assess(peer="127.0.0.1", local="127.0.0.1")
    assert not local.confirmed
    assert local.reason == ManagementPathReason.TRANSPORT_PEER_LOCAL
    assert local.resource_id is None


def test_a_proof_for_one_local_endpoint_is_not_a_proof_for_another(estate: Estate):
    """The same operator arriving on a different address of ours has proven nothing yet."""
    assert estate.observe_path(local=ENDPOINT).confirmed
    other = estate.assess(local=BRIDGE_ADDRESS)
    assert not other.confirmed
    assert other.reason == ManagementPathReason.UNOBSERVED


def test_evidence_from_another_host_does_not_answer_for_this_one(estate: Estate):
    estate.observe_path()
    verdict = estate.paths.assess("host_somewhere_else", estate.transport())
    assert not verdict.confirmed
    assert verdict.reason == ManagementPathReason.UNOBSERVED


def test_the_stored_match_is_on_addresses_and_not_on_an_ephemeral_port(estate: Estate):
    """A source port changes every connection; matching on it would make reuse impossible."""
    estate.observe_path()
    assert estate.assess().confirmed
    row = dict(estate.database.query("SELECT * FROM management_path_observations")[0])
    assert not any(column.endswith("_port") for column in row)
    assert "transport_peer_port" not in row and "local_endpoint_port" not in row


# --------------------------------------------------------------------------- persistence


def test_evidence_is_append_only_and_immutable(estate: Estate):
    import sqlite3

    verdict = estate.observe_path()
    with pytest.raises(sqlite3.IntegrityError):
        estate.database.connection.execute(
            "UPDATE management_path_observations SET route_oif_index = 4 "
            "WHERE observation_id = ?",
            (verdict.evidence_id,),
        )
    estate.observe_path()
    assert len(estate.rows()) == 2, "a second observation appends rather than replacing"


def test_a_lookup_that_did_not_resolve_carries_no_route_facts_and_says_why(estate: Estate):
    import errno
    import sqlite3

    estate.routes.replies[OPERATOR] = netlink_error(errno.ENETUNREACH)
    estate.observe_path()
    row = dict(estate.database.query("SELECT * FROM management_path_observations")[0])
    assert row["route_status"] == "unreachable"
    assert row["route_reason"] == "network_unreachable"
    assert row["route_oif_index"] is None
    assert row["route_preferred_source"] is None

    repository = ManagementPathRepository(estate.database)
    with pytest.raises(sqlite3.IntegrityError):
        repository.insert(
            {
                **{k: v for k, v in row.items() if k != "observation_id"},
                "observation_id": "mpo_fabricated",
                "route_status": "failed",
                "route_reason": None,
            }
        )


def test_a_lookup_that_did_not_resolve_cannot_smuggle_an_egress_in(estate: Estate):
    import sqlite3

    estate.observe_path()
    row = dict(estate.database.query("SELECT * FROM management_path_observations")[0])
    with pytest.raises(sqlite3.IntegrityError):
        ManagementPathRepository(estate.database).insert(
            {
                **{k: v for k, v in row.items() if k != "observation_id"},
                "observation_id": "mpo_fabricated",
                "route_status": "failed",
                "route_reason": "netlink_timeout",
                "route_oif_index": 3,
            }
        )


def test_the_two_ends_of_a_connection_must_be_the_same_family(estate: Estate):
    import sqlite3

    estate.observe_path()
    row = dict(estate.database.query("SELECT * FROM management_path_observations")[0])
    with pytest.raises(sqlite3.IntegrityError):
        ManagementPathRepository(estate.database).insert(
            {
                **{k: v for k, v in row.items() if k != "observation_id"},
                "observation_id": "mpo_fabricated",
                "transport_peer_family": "inet6",
            }
        )


def test_observing_writes_evidence_and_nothing_else(estate: Estate):
    """The store's other tables are untouched: this observes a connection, not the estate."""
    before = {
        table: [tuple(r) for r in estate.database.query(f"SELECT * FROM {table}")]
        for table in ("objects", "observations", "observation_sweeps", "intents", "findings")
    }
    estate.observe_path()
    after = {
        table: [tuple(r) for r in estate.database.query(f"SELECT * FROM {table}")]
        for table in ("objects", "observations", "observation_sweeps", "intents", "findings")
    }
    assert after == before


def test_observing_leaves_the_fixture_host_byte_identical(estate: Estate):
    def snapshot() -> dict:
        return {
            entry.name: {f.name: f.read_text() for f in sorted(entry.iterdir()) if f.is_file()}
            for entry in sorted(estate.sysfs.iterdir())
            if (entry / "ifindex").exists()
        }

    before = snapshot()
    estate.observe_path()
    estate.assess()
    assert snapshot() == before


def test_reading_writes_nothing_at_all(estate: Estate):
    estate.observe_path()
    before = estate.rows()
    for _ in range(3):
        estate.assess()
        estate.assess(peer="192.0.2.131")
        estate.assess(peer="127.0.0.1")
    assert estate.rows() == before
    assert estate.routes.requests == [OPERATOR], "assessing must not query the kernel"


# ---------------------------------------------------------------------------- protection


def test_the_confirmed_object_is_protected_and_the_reason_is_the_management_path(
    estate: Estate,
):
    verdict = estate.observe_path()
    protection = assess_resource_protection(verdict, resource_id=estate.id_of("eth1"))
    assert protection.status is ProtectionStatus.PROTECTED
    assert protection.reasons == (ProtectionReason.MANAGEMENT_PATH,)
    assert protection.management_path is ManagementPathRelation.ON_MANAGEMENT_PATH
    assert protection.unresolved == ()
    assert protection.evidence_id == verdict.evidence_id


def test_another_object_is_not_the_target_only_because_the_path_itself_is_proven(
    estate: Estate,
):
    verdict = estate.observe_path()
    protection = assess_resource_protection(verdict, resource_id=estate.id_of("eth0"))
    assert protection.status is ProtectionStatus.CLEAR
    assert protection.management_path is ManagementPathRelation.NOT_ON_MANAGEMENT_PATH
    assert protection.reason == "not_the_management_path"
    assert protection.reasons == ()


def test_an_unresolved_path_leaves_every_object_unknown_and_none_of_them_clear(
    estate: Estate,
):
    """A fleet of confident negatives with one unresolved positive is how the wrong thing goes."""
    verdict = estate.assess()  # nothing observed
    for name in ("lo", "eth0", "eth1", "br0"):
        protection = assess_resource_protection(verdict, resource_id=estate.id_of(name))
        assert protection.status is ProtectionStatus.UNKNOWN
        assert protection.management_path is ManagementPathRelation.UNKNOWN
        assert protection.unresolved == (ProtectionReason.MANAGEMENT_PATH,)
        assert protection.reasons == ()


def test_protection_is_not_ownership(estate: Estate):
    """Two axes, two questions. Neither answer implies the other."""
    verdict = estate.observe_path()
    protection = assess_resource_protection(verdict, resource_id=estate.id_of("br0"))
    assert protection.status is ProtectionStatus.CLEAR
    for word in ("owner", "configured_by", "created_by", "docker", "externally"):
        assert word not in str(protection).lower()


def test_the_protection_core_evaluates_at_least_one_reason_or_refuses(estate: Estate):
    """"Nothing was evaluated" must never be reported as "nothing applied"."""
    from localplane.backend.domain.protection import roll_up_protection

    with pytest.raises(ValueError):
        roll_up_protection((), management_path=ManagementPathRelation.UNKNOWN)


def test_the_protection_core_knows_nothing_about_linux_or_networking():
    """The reason it can grow a second member that is a service, a database or a socket."""
    from localplane.backend.domain import protection as core

    source = Path(core.__file__).read_text()
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    without_docstrings = "".join(
        part for i, part in enumerate(code.split('"""')) if i % 2 == 0
    ).lower()
    for token in (
        "mtu",
        "interface",
        "ethernet",
        "sysfs",
        "ifindex",
        "address",
        "route",
        "netlink",
        "kernel",
        "linux",
        "socket",
    ):
        assert token not in without_docstrings, f"the protection core mentions {token}"


def test_the_protection_core_declares_only_reasons_it_implements():
    assert {str(r) for r in ProtectionReason} == {
        "management_path",
        "localplane_agent",
    }
    assert {str(s) for s in ProtectionStatus} == {"protected", "clear", "unknown"}
