import errno
import hashlib
import io
import json
import os
import stat
from types import SimpleNamespace

import pytest

from localplane.protocol.diagnostic_policy import InvalidDiagnosticPolicy, LoadedDiagnosticPolicy, parse_diagnostic_policy, tcp_target_permitted
from localplane.protocol import diagnostic_policy


def test_policy_is_immutable_and_digest_is_normalized():
    first = parse_diagnostic_policy(b'{"version":1,"dns":["NAS.ops.example.internal."],"tcp":[{"address":"192.168.50.20","port":443}]}')
    second = parse_diagnostic_policy(b'{"tcp":[{"port":443,"address":"192.168.50.20"}],"dns":["nas.ops.example.internal."],"version":1}')
    assert first == second
    assert first.dns == ("nas.ops.example.internal.",)
    assert first.tcp[0].address == "192.168.50.20"
    with pytest.raises(AttributeError):
        first.digest = "changed"


def test_private_test_domain_is_allowed():
    assert parse_diagnostic_policy(b'{"version":1,"dns":["nas.lab.test."],"tcp":[]}').dns == ("nas.lab.test.",)


@pytest.mark.parametrize("document", [
    b'{"version":1,"dns":[],"tcp":[],"version":1}',
    b'{"version":true,"dns":[],"tcp":[]}',
    b'{"version":1,"dns":["localhost."],"tcp":[]}',
    b'{"version":1,"dns":["nas.local."],"tcp":[]}',
    b'{"version":1,"dns":["192.168.1.1."],"tcp":[]}',
    b'{"version":1,"dns":[],"tcp":[{"address":"127.0.0.1","port":80}]}',
    b'{"version":1,"dns":[],"tcp":[{"address":"169.254.169.254","port":80}]}',
    b'{"version":1,"dns":[],"tcp":[{"address":"168.63.129.16","port":80}]}',
    b'{"version":1,"dns":[],"tcp":[{"address":"::ffff:192.168.1.1","port":80}]}',
    b'{"version":1,"dns":[],"tcp":[{"address":"64:ff9b::c000:201","port":80}]}',
    b'{"version":1,"dns":[],"tcp":[{"address":"192.168.1.1","port":true}]}',
    b'{"version":1,"dns":[],"tcp":[],"extra":1}',
])
def test_invalid_documents_are_not_empty_policies(document):
    with pytest.raises(InvalidDiagnosticPolicy):
        parse_diagnostic_policy(document)


def test_tcp_exclusions_require_complete_current_evidence():
    policy = parse_diagnostic_policy(b'{"version":2,"control_plane":{"complete":true,"addresses":[]},"dns":[],"tcp":[{"address":"192.168.1.20","port":443}]}')
    loaded = LoadedDiagnosticPolicy("valid", policy)
    def permit(local=(), broadcasts=(), management=()):
        return tcp_target_permitted(loaded, policy.digest, "192.168.1.20", 443, local_addresses=local, broadcast_addresses=broadcasts, management_addresses=management)
    assert permit()
    assert not permit(local=None)
    assert not permit(broadcasts=None)
    assert not permit(management=None)
    assert not permit(local=("192.168.1.20",))
    assert not permit(broadcasts=("192.168.1.20",))
    assert not permit(management=("192.168.1.20",))
    assert not permit(local=("malformed",))


def v2(*, complete=True, addresses=(), dns=("nas.example.internal.",), tcp=None):
    return {"version": 2, "dns": list(dns), "tcp": tcp if tcp is not None else [
        {"address": "192.168.50.20", "port": 443}, {"address": "fd00::20", "port": 443},
    ], "control_plane": {"complete": complete, "addresses": list(addresses)}}


def parse(document):
    return parse_diagnostic_policy(json.dumps(document).encode())


def permit(policy, address="192.168.50.20", port=443, *, digest=None, local=(), broadcast=(), management=()):
    return tcp_target_permitted(LoadedDiagnosticPolicy("valid", policy), policy.digest if digest is None else digest,
        address, port, local_addresses=local, broadcast_addresses=broadcast, management_addresses=management)


def test_v1_canonical_digest_and_constructor_defaults_remain_exactly_legacy_dns_only():
    policy = parse_diagnostic_policy(b'{"version":1,"dns":["NAS.example.internal."],"tcp":[{"address":"192.168.50.20","port":443}]}')
    canonical = b'{"dns":["nas.example.internal."],"tcp":[{"address":"192.168.50.20","port":443}],"version":1}'
    assert policy.digest == hashlib.sha256(canonical).hexdigest()
    assert policy.version == 1 and policy.control_plane_complete is False and policy.control_plane_addresses == ()
    assert policy.dns == ("nas.example.internal.",)
    assert not permit(policy)


def test_v2_normalization_and_declared_deny_identity_are_order_independent():
    first = parse(v2(addresses=("fd00:0:0:0:0:0:0:1", "192.168.50.1", "::ffff:c0a8:3201"), dns=("NAS.example.internal.",)))
    second = parse(v2(addresses=("192.168.50.1", "fd00::1")))
    assert first == second
    assert first.version == 2 and first.control_plane_complete is True
    assert first.control_plane_addresses == ("192.168.50.1", "fd00::1")
    assert permit(first) and permit(first, "fd00::20")
    assert first.digest != parse(v2(addresses=("192.168.50.2", "fd00::1"))).digest
    assert first.digest != parse(v2(complete=False, addresses=("192.168.50.1", "fd00::1"))).digest
    assert first.digest != parse(v2(addresses=("192.168.50.1", "fd00::1"), tcp=[])).digest
    with pytest.raises(AttributeError):
        first.control_plane_complete = False


@pytest.mark.parametrize("address,declared", [("192.168.50.20", "192.168.50.20"), ("fd00::20", "fd00:0:0:0:0:0:0:20"), ("192.168.50.20", "::ffff:192.168.50.20")])
@pytest.mark.parametrize("port", [1, 22, 443, 65535])
def test_address_wide_declared_denial_wins_over_an_explicit_allowed_pair_at_every_port(address, declared, port):
    policy = parse(v2(addresses=(declared,), tcp=[{"address": address, "port": port}]))
    assert not permit(policy, address, port, management=())


def test_live_exclusions_only_add_to_declaration_and_unknown_required_sets_deny():
    policy = parse(v2(addresses=("192.168.50.1",)))
    assert permit(policy)
    for field in ("local", "broadcast", "management"):
        assert not permit(policy, **{field: ("192.168.50.20",)})
        assert not permit(policy, **{field: None})
        assert not permit(policy, **{field: (True,)})
    assert not permit(policy, digest="b" * 64)


def test_valid_incomplete_v2_keeps_dns_but_never_grants_tcp():
    policy = parse(v2(complete=False, addresses=()))
    assert policy.dns == ("nas.example.internal.",)
    assert policy.control_plane_complete is False and not permit(policy)


@pytest.mark.parametrize("declaration", [None, {}, {"complete": True}, {"addresses": []},
    {"complete": 1, "addresses": []}, {"complete": "true", "addresses": []},
    {"complete": None, "addresses": []}, {"complete": True, "addresses": None},
    {"complete": True, "addresses": {}, "extra": 1}, {"complete": True, "addresses": [], "extra": 1}])
def test_v2_declaration_requires_exact_keys_and_strict_bool_and_array(declaration):
    document = v2()
    document["control_plane"] = declaration
    with pytest.raises(InvalidDiagnosticPolicy):
        parse(document)


@pytest.mark.parametrize("address", [True, 1, None, "nas.example.internal.", "192.168.50.0/24", "192.168.50.1-20", "fd00::1%eth0", "192.168.050.020", "*", "", float("nan"), float("inf")])
def test_invalid_control_plane_address_forms_cannot_become_empty_or_complete(address):
    with pytest.raises(InvalidDiagnosticPolicy):
        parse(v2(addresses=(address,)))


@pytest.mark.parametrize("change", ["missing", "extra", "v1_declaration", "bool_version", "float_version", "unknown_version"])
def test_policy_version_and_exact_top_level_keys(change):
    document = v2()
    if change == "missing":
        del document["control_plane"]
    elif change == "extra":
        document["extra"] = True
    elif change == "v1_declaration":
        document["version"] = 1
    else:
        document["version"] = {"bool_version": True, "float_version": 2.0, "unknown_version": 3}[change]
    with pytest.raises(InvalidDiagnosticPolicy):
        parse(document)


def test_v2_rejects_duplicate_nested_keys_and_nonfinite_numbers():
    for document in (
        b'{"version":2,"dns":[],"tcp":[],"control_plane":{"complete":false,"complete":true,"addresses":[]}}',
        b'{"version":2,"dns":[],"tcp":[],"control_plane":{"complete":true,"addresses":[],"addresses":[]}}',
        b'{"version":2,"dns":[],"tcp":[],"control_plane":{"complete":NaN,"addresses":[]}}',
    ):
        with pytest.raises(InvalidDiagnosticPolicy):
            parse_diagnostic_policy(document)


def test_address_and_existing_target_count_and_document_bounds_are_preserved():
    assert len(parse(v2(addresses=tuple(f"192.168.0.{i}" for i in range(1, 129)))).control_plane_addresses) == 128
    with pytest.raises(InvalidDiagnosticPolicy):
        parse(v2(addresses=("192.168.50.1",) * 129))
    with pytest.raises(InvalidDiagnosticPolicy):
        parse(v2(dns=("nas.example.internal.",) * 33))
    with pytest.raises(InvalidDiagnosticPolicy):
        parse(v2(tcp=[{"address": "192.168.50.20", "port": 443}] * 33))
    with pytest.raises(InvalidDiagnosticPolicy):
        parse_diagnostic_policy(b" " * 16385)


def fake_load(monkeypatch, document, *, leaf_mode=stat.S_IFREG | 0o640, ancestor_mode=stat.S_IFDIR | 0o755,
              owner=0, group=2345, links=1, leaf_error=None, acl=False):
    opened, closed = [], []
    class Stream(io.BytesIO):
        def close(self):
            if not self.closed:
                closed.append(12)
            super().close()
    def open_fd(path, flags, *, dir_fd=None):
        assert (path, dir_fd) in (("/", None), ("policy", 10), ("targets.json", 11))
        if path == "targets.json" and leaf_error:
            raise leaf_error
        if path != "/":
            assert flags & os.O_NOFOLLOW
        fd = {"/": 10, "policy": 11, "targets.json": 12}[path]
        opened.append(fd)
        return fd
    def fstat(fd):
        return SimpleNamespace(st_uid=owner if fd == 12 else 0, st_gid=group,
            st_mode=leaf_mode if fd == 12 else ancestor_mode, st_nlink=links)
    def getxattr(*args):
        if acl:
            return b"synthetic ACL"
        raise OSError(errno.ENODATA, "no fixture ACL")
    def fdopen(fd, mode):
        assert (fd, mode) == (12, "rb")
        return Stream(document)
    proxy = SimpleNamespace(**{key: getattr(os, key) for key in ("O_RDONLY", "O_DIRECTORY", "O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK")},
        open=open_fd, fstat=fstat, getxattr=getxattr, close=closed.append, fdopen=fdopen)
    with monkeypatch.context() as patch:
        patch.setattr(diagnostic_policy, "os", proxy)
        patch.setattr(diagnostic_policy, "grp", SimpleNamespace(getgrnam=lambda name: SimpleNamespace(gr_gid=2345)))
        loaded = diagnostic_policy.load_diagnostic_policy("/policy/targets.json")
    assert sorted(opened) == sorted(closed)
    return loaded


def test_protected_startup_loader_accepts_v2_without_real_files_or_host_groups(monkeypatch):
    loaded = fake_load(monkeypatch, json.dumps(v2(addresses=("192.168.50.1",))).encode())
    assert loaded.status == "valid" and loaded.policy.version == 2
    assert permit(loaded.policy)


@pytest.mark.parametrize("options,status", [
    ({"leaf_mode": stat.S_IFREG | 0o660}, "unsafe"), ({"leaf_mode": stat.S_IFLNK | 0o640}, "unsafe"),
    ({"ancestor_mode": stat.S_IFDIR | 0o777}, "unsafe"), ({"owner": 1000}, "unsafe"),
    ({"group": 999}, "unsafe"), ({"links": 2}, "unsafe"), ({"acl": True}, "unsafe"),
    ({"leaf_error": OSError(errno.ELOOP, "fixture symlink")}, "unsafe"),
    ({"leaf_error": PermissionError(errno.EACCES, "fixture inaccessible")}, "unreadable"),
])
def test_protected_loader_safety_stays_fail_closed_with_fake_filesystem(monkeypatch, options, status):
    loaded = fake_load(monkeypatch, json.dumps(v2()).encode(), **options)
    assert loaded.status == status and loaded.policy is None


def test_missing_invalid_and_valid_empty_are_distinct_startup_states(monkeypatch):
    missing = fake_load(monkeypatch, b"", leaf_error=FileNotFoundError(errno.ENOENT, "fixture missing"))
    invalid = fake_load(monkeypatch, b'{"version":2,"dns":[],"tcp":[]}')
    empty = fake_load(monkeypatch, json.dumps(v2(tcp=[], dns=(), addresses=())).encode())
    assert missing.status == "missing" and missing.policy.tcp == missing.policy.dns == ()
    assert invalid.status == "invalid" and invalid.policy is None
    assert empty.status == "valid" and empty.policy.tcp == empty.policy.dns == ()
    assert empty.policy.control_plane_complete is True
