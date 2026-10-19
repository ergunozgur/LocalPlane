"""Synthetic storage provider for API tests and separately authenticated preview harnesses.

Inject this provider into AgentService(storage_provider=...). It never opens a host path.
Use an ordinary disposable agent socket/backend and normal authentication; production
has no fixture flag, fixture route, credential shortcut or environment-selected provider.
"""

from localplane.protocol.storage import StorageCounters, StorageMount, StorageObservation
from localplane.protocol.wire import ErrorCode, Method, ProtocolError


class FixtureStorageProvider:
    def __init__(self, state="pressure"):
        self.state = state
        self.calls = 0

    def observe(self):
        self.calls += 1
        if self.state in {"busy", "unavailable"}:
            return StorageObservation(self.state, "Synthetic collector unavailable.").as_dict()
        if self.state == "empty":
            return StorageObservation("partial", "No mount evidence in this synthetic observation.", namespace="mnt:[42]").as_dict()
        byte_pressure = StorageMount(11, 1, "/example/data", "/", "ext4", "8:1", False,
            status="observed", reason="Synthetic pinned mount evidence.",
            bytes=StorageCounters("10737418240", "9663676416", "1073741824", "0", "1073741824"),
            inodes=StorageCounters("1000", "100", "900", "900", "0"),
            inode_reason="Synthetic inode evidence.", backing_name="example1",
            backing_reason="Synthetic immediate kernel block-device join.")
        inode_pressure = StorageMount(12, 1, "/example/small-files", "/", "xfs", "8:2", False,
            status="observed", reason="Synthetic pinned mount evidence.",
            bytes=StorageCounters("10737418240", "1073741824", "9663676416", "9663676416", "0"),
            inodes=StorageCounters("1000", "1000", "0", "0", "0"),
            inode_reason="Synthetic inode evidence.")
        unsupported = StorageMount(13, 1, "/example/remote", "/", "nfs", "0:42", True)
        return StorageObservation("partial", "Synthetic byte/inode pressure and unsupported mount.",
            namespace="mnt:[42]", mounts=[byte_pressure, inode_pressure, unsupported]).as_dict()


class FixtureStorageService:
    """Test-only AgentServer seam with no startup probes in any host domain.

    An authenticated development backend with observe_on_startup=False can point to
    its disposable socket. Unimplemented methods refuse explicitly; no host collector,
    helper, runner or daemon is constructed. Not imported by production code.
    """
    instance_id = "synthetic-storage"
    capabilities = ()

    def __init__(self, state="pressure"):
        self.provider = FixtureStorageProvider(state)

    def handle(self, method, params):
        if method != Method.STORAGE_OBSERVE_FILESYSTEMS:
            raise ProtocolError(ErrorCode.UNSUPPORTED_METHOD, "This synthetic service provides only storage observations.")
        if params:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, "Storage observation accepts no parameters.")
        return self.provider.observe()
