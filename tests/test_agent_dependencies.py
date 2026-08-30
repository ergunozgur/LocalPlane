"""The agent's dependency surface is part of its contract.

The unprivileged agent has one deliberately narrow third-party boundary: Jeepney in the
systemd provider. Protocol, IPC and logging remain standard-library-only, and the
privileged helper has the stricter zero-third-party rule. These are checked rather than
remembered.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import localplane

PACKAGE_ROOT = Path(localplane.__file__).parent
STDLIB_ONLY_PACKAGES = ("agent", "protocol")
SYSTEMD_PROVIDER = PACKAGE_ROOT / "agent" / "providers" / "systemd.py"
SOCKET_DIAG_PROVIDER = PACKAGE_ROOT / "agent" / "providers" / "linux_socket_diag.py"


def _module_files() -> list[Path]:
    files: list[Path] = []
    for package in STDLIB_ONLY_PACKAGES:
        files.extend(sorted((PACKAGE_ROOT / package).rglob("*.py")))
    files.append(PACKAGE_ROOT / "log.py")
    files.append(PACKAGE_ROOT / "__init__.py")
    for path in files:
        assert path.exists(), f"the audit is pointed at a file that no longer exists: {path}"
    return files


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_only_the_systemd_provider_imports_the_agents_one_third_party_dependency():
    """Both sides of the privilege boundary, and the helper with more force than the agent.

    Everything the helper imports would have to be trusted with root on an operator's host and
    audited for it. The agent gained a dependency on the helper's protocol and client, which
    is the intended direction — the unprivileged side talks to the privileged one
    — and it must not have cost either of them the rule.
    """
    offenders: dict[str, set[str]] = {}
    for path in _module_files() + _helper_files():
        third_party = {
            name
            for name in _top_level_imports(path)
            if name != "localplane" and name not in sys.stdlib_module_names
        }
        allowed = {"jeepney"} if path == SYSTEMD_PROVIDER else set()
        third_party -= allowed
        if third_party:
            offenders[str(path.relative_to(PACKAGE_ROOT))] = third_party
    assert offenders == {}, f"agent gained third-party dependencies: {offenders}"

    jeepney_importers = {
        str(path.relative_to(PACKAGE_ROOT))
        for path in _module_files()
        if "jeepney" in _top_level_imports(path)
    }
    assert jeepney_importers == {"agent/providers/systemd.py"}


def test_protocol_ipc_log_and_helper_remain_stdlib_only():
    files = sorted((PACKAGE_ROOT / "protocol").rglob("*.py"))
    files += [PACKAGE_ROOT / "ipc.py", PACKAGE_ROOT / "log.py"]
    files += _helper_files()
    offenders = {
        str(path.relative_to(PACKAGE_ROOT)): {
            name
            for name in _top_level_imports(path)
            if name != "localplane" and name not in sys.stdlib_module_names
        }
        for path in files
    }
    assert {path: names for path, names in offenders.items() if names} == {}


def test_the_backend_does_not_import_jeepney():
    offenders = [
        str(path.relative_to(PACKAGE_ROOT))
        for path in sorted((PACKAGE_ROOT / "backend").rglob("*.py"))
        if "jeepney" in _top_level_imports(path)
    ]
    assert offenders == []


def test_linux_socket_diag_is_a_stdlib_only_typed_netlink_reader():
    imports = _top_level_imports(SOCKET_DIAG_PROVIDER)
    assert {
        name for name in imports
        if name != "localplane" and name not in sys.stdlib_module_names
    } == set()
    source = SOCKET_DIAG_PROVIDER.read_text(encoding="utf-8")
    for forbidden in (
        "subprocess", "shell=True", "nsenter", "setns(", "CAP_SYS_ADMIN",
        "netstat", "lsof",
    ):
        assert forbidden not in source
    assert "SOCK_DIAG_BY_FAMILY" in source
    assert "INET_DIAG_CGROUP_ID" in source


def test_the_agent_does_not_import_the_backend():
    """The dependency runs one way. The privileged side must not need the API to work."""
    for path in _module_files():
        source = path.read_text(encoding="utf-8")
        assert "localplane.backend" not in source, f"{path} imports the backend"


@pytest.mark.parametrize(
    "forbidden",
    ["os.system", "shell=True", "eval(", "exec(", "subprocess.getoutput", "popen"],
)
def test_the_agent_has_no_general_execution_path(forbidden):
    """There is no ``execute_root(command)`` here, and there is no way to build one."""
    for path in _module_files():
        source = path.read_text(encoding="utf-8").lower()
        # The one mention of shell=False in the subprocess call is the point, not a hit.
        assert forbidden.lower() not in source.replace("shell=false", ""), (
            f"{path} contains {forbidden}"
        )


def test_the_only_commands_the_agent_runs_are_read_only():
    from localplane.agent.providers.linux_network import ADDR_ARGV, LINK_ARGV

    for argv in (LINK_ARGV, ADDR_ARGV):
        assert argv[0] == "ip"
        assert "show" in argv
        assert not ({"set", "add", "del", "delete", "change", "flush"} & set(argv))


# --------------------------------------------------------------- the privileged helper


HELPER_PACKAGE = PACKAGE_ROOT / "helper"


def _helper_files() -> list[Path]:
    files = sorted(HELPER_PACKAGE.rglob("*.py"))
    assert files, "the helper package has disappeared"
    return files


def test_the_privileged_helper_imports_nothing_from_the_backend_or_the_agent():
    """The dependency runs one way, and the privileged end is the end of it.

    A helper that imported the backend would be a root process that has to be rebuilt when
    an API model changes; one that imported the agent would blur the boundary the socket
    exists to draw. It imports its own protocol and the package version, and nothing else.
    """
    for path in _helper_files():
        source = path.read_text(encoding="utf-8")
        assert "localplane.backend" not in source, f"{path} imports the backend"
        assert "localplane.agent" not in source, f"{path} imports the agent"
        assert "localplane.protocol" not in source, f"{path} imports the agent protocol"
