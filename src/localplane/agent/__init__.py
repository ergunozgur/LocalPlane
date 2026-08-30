"""The LocalPlane Agent.

The agent is the only component that touches the host. It is a separate process reached
over an AF_UNIX socket, and it exposes typed operations rather than a shell: there is no
method here that accepts a command, a path to an executable or an argument vector, and
adding one would be a change to the protocol rather than a change to a caller.

This package is standard-library only, on purpose. The agent is the component that will
eventually hold privilege, and the smaller its dependency surface, the smaller the set of
things that have to be trusted to hold it.

The agent holds no privilege at all: every source it reads — sysfs,
/proc, /etc/os-release, rtnetlink through iproute2's JSON output — is world-readable, and
it reports ``privilege: unprivileged`` rather than implying a separation it has not made.
"""
