# Connectivity diagnostics

## Reading the workspace

Profile configuration, applied configuration and observed kernel facts are independent
readings, not an automatic drift verdict. Each section retains its own source,
observation time, freshness, completeness and gaps. Stored evidence is not live health.
Unknown fields and unavailable sections are not empty inventories; a reported empty
list describes only that source at its observation time.

Configuration methods, gateways, route metrics, DNS priorities and ignore-automatic-DNS
flags are shown per IP family when reported. Automatic configuration can supply runtime
addresses, routes and DNS even when configured lists are empty. Empty configured DNS
does not mean global DNS absence. A configured route metric of `-1` means
automatic/provider-selected; it does not establish an effective metric. DNS priority is
not an IP route metric, and the workspace does not compare priorities to choose a resolver.
The **How to compare configuration sources** disclosure keeps these distinctions near
the three configuration plates; the family disclosures retain the actual values.

Resolver evidence separates global servers and domains, per-link contributions,
resolver-selected current server, and client resolver configuration. Current-server
scope ifindex `0` means resolver-global; a positive ifindex means link-scoped. Missing
scope stays unknown: a current server is neither silently added to the global list nor
proof of which server answered a diagnostic. Domains prefixed with `~` are route-only;
explicit routing-domain entries retain their reported route-only flag. DNS default-route
is not the IP default route. A client stub such as `127.0.0.53` is not an upstream server.
The **Resolver scope and domain details** disclosure provides the local interpretation.

Route and rule inventories describe installed entries, not a reimplementation of kernel
selection. Next hops retain separately labelled gateway, interface, ifindex and weight;
unknown metrics or selectors are not assumed zero or absent. Interface links require
authoritative identity evidence, not a matching display name.

## Observation and explicit diagnostics

Route selection does not establish reachability or identify the operator's management
path. Connectivity observation is explicit and stores section evidence. Reading the workspace
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

## Provider and collection mechanics

Profile evidence uses NetworkManager's read-only device and settings interfaces where
available. Applied-connection collection is enabled for NetworkManager 1.46.0; other
versions remain unavailable until their secret-exclusion behavior is verified. IP-family settings retain the provider's actual method, metric, gateway,
DNS priority and ignore-auto-DNS values; unknown and legacy DNS encodings are not treated
as empty. Resolved's global and per-link observations are independently qualified from
the client resolver file. Legacy resolver properties do not establish server port, TLS
name, or the server that answered a query. The route and rule inventories may be partial
when an address family or a provider field is unavailable.

## Administrator-provisioned target policy

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

## Execution bounds and retained results

DNS-name and TCP IP:port authorizations are independent: resolving an approved name does
not authorize a connection to its answers. Each action requires explicit submission;
reading evidence, opening details, changing a target or returning to the workspace
does not start a diagnostic. Retained earlier output describes its submitted target,
not a newly selected target. Results in the current workspace are not durable diagnostic
history across reloads.

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
