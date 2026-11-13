# Connectivity diagnostics

The connectivity API distinguishes profile, applied, kernel, route, rule and resolver
evidence. A missing reading is not an empty configuration. Route selection does not
establish reachability or identify the operator's management path. The connectivity
observation is explicit and stores independent section evidence. Reading the workspace
does not trigger a refresh. A failed latest refresh is unavailable, not an empty result.
An interrupted or timed-out refresh may still be running. Read the stored evidence before
explicitly retrying; the agent refuses a second collection while the first remains active.
The explicit numeric route lookup queries kernel route selection without sending a packet;
it is not an active reachability check. Explicit DNS diagnostics use the configured
systemd-resolved service when policy and current host identity permit. They use DNS-only
resolution without search expansion; resolver cache, CNAME, DNSSEC, upstream behavior and
internal retries remain provider-controlled and may continue after a timeout. TCP
diagnostics connect once to an independently authorized numeric address/port and close
the socket without sending or reading application payload, TLS or banners. A handshake
does not prove service health; refusal does not identify the rejector, and timeout does
not identify a firewall. Neither action confirms the management path or binds traffic
to the workspace's selected interface.

When available, the returned passive route lookup is sampled between matching kernel
egress identity reads. Its label can differ from the selected workspace interface.
A stored object link additionally requires matching current host, boot, namespace,
agent, inventory and ifindex plus durable hardware identity. A name alone never creates
that relationship; changing, stale or unavailable associations remain unknown while
independently valid route facts are retained.

Profile evidence uses NetworkManager's read-only device and settings interfaces where
available. Applied-connection collection is enabled for NetworkManager 1.46.0; other
versions remain unavailable until their secret-exclusion behavior is verified. IP-family settings retain the provider's actual method, metric, gateway,
DNS priority and ignore-auto-DNS values; unknown and legacy DNS encodings are not treated
as empty. Resolved's global and per-link observations are independently qualified from
the client resolver file. Legacy resolver properties do not establish server port, TLS
name, or the server that answered a query. The route and rule inventories may be partial
when an address family or a provider field is unavailable.

Active diagnostics require an administrator-controlled target policy. Both backend and
agent load the policy once at startup. The default is
`/etc/localplane/diagnostic-targets.json`; `LOCALPLANE_DIAGNOSTIC_POLICY_PATH` selects an
absolute alternate path on each process. The policy file must be a regular non-symlink
owned by root and group `localplane`, mode `0640`, with root-owned ancestors not writable
by group or others. Both processes must use the same normalized policy and be restarted
after an administrative change. Missing policy means no targets; malformed, unsafe or
unreadable policy is separately reported and fails closed.

The version-1 file contains only `version`, `dns` and `tcp`; version 1 does not authorize
TCP execution. DNS names are absolute,
ASCII fully qualified names ending in a dot. TCP targets are exact numeric unicast
addresses and integer ports. Up to 32 names and 32 address/port pairs are accepted:

```json
{"version":1,"dns":["nas.ops.example.internal."],"tcp":[]}
```

Version 2 adds the administrator-attested complete control-plane IP deny set required
for TCP eligibility:

```json
{"version":2,"dns":["nas.ops.example.internal."],"tcp":[{"address":"192.168.50.20","port":443}],"control_plane":{"complete":true,"addresses":["192.168.50.1"]}}
```

The declaration accepts at most 128 exact numeric IP addresses, not ranges or names;
each denied address takes precedence over all allowed ports. `complete` is an
administrator attestation, not automatically discovered topology. LocalPlane cannot
detect every undeclared topology change. If the declaration cannot be maintained, use
DNS only with TCP targets empty. Policy metadata describes the backend's loaded policy,
not confirmation that the agent has loaded an identical one or that TCP is operational.

Before changing management topology, provision `tcp: []`, restart **both** backend and
agent, and wait for any active attempt to finish before making the change. Afterwards,
reconcile the control-plane addresses and desired TCP pairs, then restart both processes
with matching policy before re-enabling TCP. Saving the file alone does not disable the
previously loaded targets; there is no hot reload, policy editor or enable flag.

There are no default targets, wildcards, ranges, caller-selected DNS servers, or policy
editors. A listed target is not by itself permission to send traffic: current host and
local and broadcast exclusions, supported network and monotonic-clock namespaces,
matching backend/agent policy, and runtime limits must also be established before
dispatch. Backend and agent independently validate their loaded policy; the agent repeats
the strict live inventory immediately before connecting. Two matching snapshots reduce
an observation race but cannot make a subsequent connection atomic with topology changes.
No policy is provisioned by the application.

Both boundaries admit at most one diagnostic at a time across DNS and TCP and at most
six starts per rolling minute. The response budget is five seconds, including passive
preparation; a TCP connect receives at most three seconds and the remaining budget.
DNS results contain at most 32 address answers and report truncation. There are no
automatic retries, interface binding, DNS-to-TCP chaining or cancellation guarantees.

Execution consumes an exact, one-use, process-local admission ticket. A subsequent
explicit attempt may check the previous ticket's status, but a global idle flag or
transport timeout is not completion proof. A lost response keeps backend admission
held until the same agent proves that exact request completed or its unused ticket
expired without execution. Issued unused tickets expire at their original deadline;
they cannot execute afterward. Pending execution is never evicted. Completion metadata
is bounded to 32 tickets, is not durable diagnostic history, and contains no results.
Unknown, evicted, mismatched or failed-cleanup evidence remains fail-closed and requires
operator assessment; it is never silently reset or treated as cancellation.
