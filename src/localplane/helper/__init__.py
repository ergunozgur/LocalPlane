"""The LocalPlane privileged helper.

The one component in LocalPlane that holds privilege, and the smallest one that could.

**It is a capability executor, not a root shell.** Its method set is closed, fixed and
versioned in :mod:`localplane.helper.protocol`; exactly one of its methods mutates
anything, and that method sets an interface's MTU and can do nothing else. There is no
command, no argv, no executable, no shell, no subprocess, no module name, no expression,
no netlink message type, no ioctl number, no provider call and no generic RPC anywhere in
this package — and a test reads the source to prove each of those absences rather than
trusting this sentence.

**The privilege boundary is the AF_UNIX socket.** The agent stays unprivileged and talks
to the helper over a socket whose peer credentials are checked with ``SO_PEERCRED`` before
a byte of the request is parsed. Fail closed: an unreadable credential, an unexpected uid
or an unexpected gid is one structured refusal and a closed connection.

**Dependencies are the standard library and nothing else**, for the same reason the agent's
are: every third-party package here would be a package that has to be trusted with root on
an operator's host.
"""

from __future__ import annotations
