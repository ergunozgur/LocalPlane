/**
 * Deriving the relationship graph from published backend evidence.
 *
 * Kept apart from the rendering so it can be reasoned about — and tested — as what it is: a
 * pure function from four API responses to a list of nodes. Nothing here decides whether
 * anything is safe, and every uncertainty becomes a node that says it is uncertain rather
 * than a node that is omitted; an absent column would read as "nothing there", which is a
 * different claim from "not established".
 *
 * **Every node is backed by evidence the backend published, and the joins are on identifiers
 * rather than on names.** That distinction is the whole reason this is implementable
 * honestly:
 *
 *  - A Docker bridge is not recognised by its `br-` prefix. The backend attributes it with
 *    `ownership.created_by`, whose `owner.instance` is the full Docker network id and whose
 *    `owner.label` is the network's name, on the evidence code
 *    `docker_ipam_gateway_on_link`. Containers carry `networks[].network_id`. The bridge and
 *    its containers are joined on that id.
 *  - Subnets are computed from an address and its prefix length — arithmetic on observed
 *    values, not a guess.
 *  - The upstream gateway comes from `management_path.evidence.route`, which exists only
 *    when the path is confirmed. When it is not, the column says so and names the evidence
 *    that would settle it.
 *
 * The Internet, carrier, tailnet and relay nodes are **not** produced: there is no
 * reachability probe, no modem provider and no Tailscale peer endpoint in this build, and
 * inventing them is precisely what this product exists not to do.
 */
import type {
  AgentStatus,
  DockerContainer,
  Host,
  ManagementPath,
  NetworkInterface,
} from '@/api/types';
import type { Semantic } from '@/domain/vocabulary';
import { health as healthOf, managementPathState, notKnown } from '@/domain/vocabulary';

/**
 * An edge, and why it exists.
 *
 * The graph draws four kinds of line and they are not decoration: `active` is a path
 * something is proven to take, `drift` is a relationship whose object disagrees with its
 * intent, `standby` is a relationship that exists but carries nothing established, and
 * `plain` is everything else. `why` is the sentence the evidence strip writes out when the
 * edge is selected — the one place the whole relationship is stated, so nothing is lost
 * when a label is shortened or dropped at a narrow width.
 *
 * There is no `flow` kind. The design direction animates a dashed overlay along an edge to
 * suggest traffic; this build has no throughput series of any kind, and an animation
 * implying live traffic would be the most persuasive lie on the page.
 */
export interface TopologyEdge {
  from: string;
  kind: 'plain' | 'active' | 'drift' | 'standby';
  /** The short mono label drawn on the curve. Dropped before the diagram is crowded. */
  label?: string;
  /** The full statement, written out in the relationship strip. Never abbreviated. */
  why: string;
}

/** One of the host's own attachments, rendered as a `.port` row inside the host node. */
export interface TopologyPort {
  name: string;
  detail: string;
  tone: Semantic['tone'];
  objectId: string;
  /** The reconciliation state, when the object has one. Drives the row's own marker. */
  drifted?: boolean;
}

export interface TopologyNode {
  id: string;
  /** Which column this belongs to. */
  column: number;
  name: string;
  /** The short right-aligned classifier beside a node name. */
  kind: string;
  mark: Semantic;
  /** Primary detail, in mono — an address, an identifier, a count. */
  detail?: string | null;
  /** Secondary detail, clamped to two lines. */
  note?: string | null;
  to?: string | undefined;
  /** Short count lines listed under the node. Used by the host, which is the focal node. */
  facts?: readonly string[];
  /** A small table of related objects inside the node. The host's interfaces use this. */
  ports?: readonly TopologyPort[];
  /** The heading over the port rows, when there are any. */
  portsLabel?: string;
  /** The object disagrees with what LocalPlane retains for it. */
  drifted?: boolean;
  /** What the evidence strip says when this node is selected. */
  evidence?: string;
  /** The published evidence codes this node rests on. */
  sources?: readonly string[];
  /** Edges into this node from the column before it, with the evidence for each. */
  edges?: readonly TopologyEdge[];
}

export const COLUMNS = ['Upstream', 'Path', 'Host', 'Attached'] as const;

/** How many interfaces the host node lists before stopping. */
const INTERFACE_LINE_LIMIT = 6;

/** How many nodes the Attached column draws before folding the remainder into one. */
const ATTACHED_NODE_LIMIT = 4;

/* ----------------------------------------------------------------- derivation helpers */

/** The subnet a bridge holds a global address on, when it holds one. */
function subnetFor(bridge: NetworkInterface): string | null {
  for (const address of bridge.addresses ?? []) {
    if (address.scope !== 'global' || address.family !== 'inet') continue;
    const subnet = subnetOf(address.address, address.prefix_length);
    if (subnet) return subnet;
  }
  return null;
}

/** The network address of `address/prefix`, for IPv4. Arithmetic, not inference. */
function subnetOf(address: string, prefix: number): string | null {
  const parts = address.split('.');
  if (parts.length !== 4) return null;
  const octets = parts.map((p) => Number.parseInt(p, 10));
  if (octets.some((o) => Number.isNaN(o) || o < 0 || o > 255)) return null;
  const value = ((octets[0]! << 24) >>> 0) + (octets[1]! << 16) + (octets[2]! << 8) + octets[3]!;
  const mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
  const network = (value & mask) >>> 0;
  return `${(network >>> 24) & 255}.${(network >>> 16) & 255}.${(network >>> 8) & 255}.${network & 255}/${prefix}`;
}

/**
 * Build the node graph from what the backend actually published.
 *
 * Returns nodes only. Nothing here decides whether anything is safe, and every uncertainty
 * becomes a node that says it is uncertain rather than a node that is omitted — an absent
 * column would read as "nothing there", which is a different claim from "not established".
 */
export function buildTopology(input: {
  host: Host;
  agent: AgentStatus;
  path: ManagementPath;
  interfaces: readonly NetworkInterface[];
  containers: readonly DockerContainer[];
  /**
   * Whether each estate read actually succeeded.
   *
   * An empty list and an unread list are different claims, and the Attached column has to
   * say which it is holding. Defaults to read, so a caller with real data need not think
   * about it.
   */
  estateRead?: { interfaces: boolean; containers: boolean };
  /** systemd unit count, when that estate was read. Omitted rather than shown as zero. */
  unitCount?: number;
}): TopologyNode[] {
  const { host, agent, path, interfaces, containers } = input;
  const estateRead = input.estateRead ?? { interfaces: true, containers: true };
  const nodes: TopologyNode[] = [];

  /* -------------------------------------------------------------------- 0 · Upstream */
  const route = path.evidence?.route ?? null;
  if (route && route.gateway) {
    nodes.push({
      id: 'gateway',
      column: 0,
      name: route.gateway,
      kind: 'gateway',
      mark: { tone: 'good', label: 'route resolved', description: 'The kernel answered which route it would use.' },
      detail: route.destination
        ? `${route.destination}/${route.destination_prefix_length ?? ''}`.replace(/\/$/, '')
        : null,
      note: route.protocol ? `${route.protocol}${route.scope ? ` · ${route.scope}` : ''}` : null,
      evidence: `The kernel answered RTM_GETROUTE for the peer address with this gateway${route.protocol ? `, learned by ${route.protocol}` : ''}.`,
      sources: ['management_path.evidence.route'],
    });
  } else {
    nodes.push({
      id: 'gateway',
      column: 0,
      name: 'not established',
      kind: 'route',
      mark: notKnown(),
      detail: route?.status ?? path.reason,
      note:
        'A route is read only as part of proving the management path. Nothing upstream of this host has been established.',
      evidence:
        'No route was resolved, so nothing upstream of this host is established. This is an unknown, not an absence.',
      sources: path.missing_evidence,
    });
  }

  /* ------------------------------------------------------------------------ 1 · Path */
  const pathInterface = path.object_id
    ? interfaces.find((item) => item.object_id === path.object_id)
    : undefined;

  nodes.push({
    id: 'path',
    column: 1,
    name: path.state === 'confirmed' && path.object_name ? path.object_name : 'unresolved',
    kind: 'management path',
    mark: managementPathState(path.state),
    detail: path.transport.peer_address ?? null,
    note:
      path.state === 'confirmed'
        ? 'This request arrives over this interface. Changing it is guarded.'
        : `${path.reason} — ${path.missing_evidence.length} piece(s) of evidence missing`,
    ...(pathInterface ? { to: `/network/${pathInterface.object_id}` } : {}),
    evidence:
      path.state === 'confirmed'
        ? `This request arrives over ${path.object_name}. A change to that interface is guarded, because losing it would lose the way back in.`
        : `The interface carrying this connection is not established — ${path.reason}.`,
    sources: path.state === 'confirmed' ? ['management_path.evidence'] : path.missing_evidence,
    edges: [
      {
        from: 'gateway',
        kind: path.state === 'confirmed' && route?.gateway ? 'active' : 'standby',
        ...(route?.destination ? { label: route.destination } : {}),
        why:
          path.state === 'confirmed' && route?.gateway
            ? `The kernel would reach the peer through ${route.gateway}.`
            : 'No route was resolved for the peer, so this leg is not established.',
      },
    ],
  });

  /* ------------------------------------------------------------------------ 2 · Host */

  // The host is the node the whole graph is about, so it carries what the machine is *made
  // of* rather than repeating its identity. Counts come from the estate reads; a count that
  // could not be read is omitted rather than shown as zero.
  const estateCounts: string[] = [];
  if (estateRead.interfaces) estateCounts.push(`${interfaces.length} interfaces`);
  if (estateRead.containers) estateCounts.push(`${containers.length} workloads`);
  if (input.unitCount !== undefined) estateCounts.push(`${input.unitCount} units`);

  // The interfaces this machine is actually reachable through. They belong here rather than
  // only in the Network table: the plane's whole subject is what the host is attached by,
  // and a node that omits it is a label.
  //
  // "Carries traffic" is the kernel's own answer — a global address, or a carrier — not a
  // judgement made here. Bridges are excluded because they appear in Attached as the
  // networks they are, and listing them twice would say the same thing in two places.
  const reachableThrough = interfaces
    // Loopback goes nowhere, so it is not a way this host is reached.
    .filter((item) => item.interface_kind !== 'loopback' && item.name !== 'lo')
    // A bridge appears in Attached as the network it is, and a member of one (a veth) is
    // part of that network rather than a separate path. Listing either here would say the
    // same thing twice. `master` is the kernel's own answer to "is this a member".
    .filter((item) => item.interface_kind !== 'bridge' && item.link?.link_kind !== 'bridge')
    .filter((item) => !item.link?.master)
    .filter(
      (item) =>
        item.link?.carrier === true ||
        (item.addresses ?? []).some((address) => address.scope === 'global'),
    )
    .slice(0, INTERFACE_LINE_LIMIT)
    .map((item) => {
      const address = (item.addresses ?? []).find((a) => a.scope === 'global');
      return {
        name: item.name,
        detail: address ? `${address.address}/${address.prefix_length}` : (item.link?.operstate ?? ''),
        tone: healthOf(item.health?.state).tone,
        objectId: item.object_id,
        drifted: item.reconciliation === 'drifted',
      };
    });

  nodes.push({
    id: 'host',
    column: 2,
    name: host.hostname ?? host.host_id,
    kind: 'this host',
    mark: agent.reachable
      ? { tone: 'good', label: 'agent reachable', description: 'The agent answered this request.' }
      : { tone: 'bad', label: 'agent unreachable', description: 'Nothing here is current.' },
    detail: [host.os_pretty_name, host.architecture].filter(Boolean).join(' · ') || null,
    note: host.kernel_release ?? null,
    facts: estateCounts,
    ports: reachableThrough,
    portsLabel: 'reachable through',
    evidence: `${host.identity_basis} identifies this host. ${agent.reachable ? 'The agent answered this request.' : 'The agent did not answer, so everything here is what was last recorded.'}`,
    sources: ['host.observe'],
    edges: [
      {
        from: 'path',
        kind: path.state === 'confirmed' ? 'active' : 'standby',
        ...(path.transport.peer_address ? { label: path.transport.peer_address } : {}),
        why:
          path.state === 'confirmed'
            ? `The connection carrying this page terminates on ${path.object_name}.`
            : 'Which interface carries this connection is not established.',
      },
    ],
  });

  /* -------------------------------------------------------------------- 3 · Attached */

  // Subnets this host holds an address on. Global scope only: a link-local address is not a
  // network the host is attached to in any operationally useful sense.
  const subnets = new Map<string, { via: string[]; family: string }>();
  for (const item of interfaces) {
    for (const address of item.addresses ?? []) {
      if (address.scope !== 'global') continue;
      const subnet =
        address.family === 'inet' ? subnetOf(address.address, address.prefix_length) : null;
      if (!subnet) continue;
      const existing = subnets.get(subnet);
      if (existing) existing.via.push(item.name);
      else subnets.set(subnet, { via: [item.name], family: address.family });
    }
  }

  // Docker networks, joined to their bridge by the network id the backend attributed, and to
  // their containers by the same id. No name matching anywhere on this path.
  const dockerBridges = interfaces.filter(
    (item) => item.ownership.created_by?.owner.provider === 'docker',
  );
  const bridgeByNetworkId = new Map<string, NetworkInterface>();
  for (const bridge of dockerBridges) {
    const id = bridge.ownership.created_by?.owner.instance;
    if (id) bridgeByNetworkId.set(id, bridge);
  }

  const containersByNetworkId = new Map<string, DockerContainer[]>();
  for (const container of containers) {
    for (const network of container.networks) {
      if (!network.network_id) continue;
      const list = containersByNetworkId.get(network.network_id);
      if (list) list.push(container);
      else containersByNetworkId.set(network.network_id, [container]);
    }
  }

  // Networks with something attached come first; an operator cares about the busy ones.
  const networkEntries = [...bridgeByNetworkId.entries()]
    .map(([networkId, bridge]) => {
      const attached = containersByNetworkId.get(networkId) ?? [];
      return { networkId, bridge, attached };
    })
    .sort((a, b) => b.attached.length - a.attached.length);

  for (const { networkId, bridge, attached } of networkEntries) {
    const running = attached.filter((c) => c.runtime.state === 'running').length;
    const subnet = subnetFor(bridge);
    nodes.push({
      id: `net:${networkId}`,
      column: 3,
      name: bridge.ownership.created_by?.owner.label ?? bridge.name,
      kind: 'container net',
      mark: healthOf(bridge.health?.state),
      // The bridge and its subnet are the same fact about the same object, so they share a
      // node rather than producing two that say half of it each.
      detail: [bridge.name, subnet].filter(Boolean).join(' · '),
      note:
        attached.length === 0
          ? 'no container is attached to this network'
          : `${attached.length} container${attached.length === 1 ? '' : 's'} · ${running} running`,
      to: `/network/${bridge.object_id}`,
      drifted: bridge.reconciliation === 'drifted',
      evidence: `Docker attributes this network to the link ${bridge.name}; its gateway address sits on that link. Containers are joined to it by network id, never by name.`,
      sources: bridge.ownership.created_by?.evidence_sources ?? [],
      edges: [
        {
          from: 'host',
          kind: bridge.reconciliation === 'drifted' ? 'drift' : 'plain',
          ...(subnet ? { label: subnet } : { label: bridge.name }),
          why: `${bridge.name} carries this network's gateway address (docker attribution: ${bridge.ownership.created_by?.reason ?? 'unstated'}).`,
        },
      ],
    });
  }

  // Subnets that a container network already accounts for are not repeated: they would be a
  // second node making the same claim about the same interface.
  const bridgeNames = new Set(dockerBridges.map((bridge) => bridge.name));
  for (const [subnet, info] of subnets) {
    if (info.via.every((name) => bridgeNames.has(name))) continue;
    nodes.push({
      id: `subnet:${subnet}`,
      column: 3,
      name: subnet,
      kind: info.family === 'inet' ? 'lan' : 'network',
      mark: {
        tone: 'neutral',
        label: 'observed',
        description: 'A network this host holds a global address on.',
      },
      detail: info.via.join(', '),
      note: null,
      evidence: `This host holds a global address inside ${subnet}. The network address is arithmetic on the observed address and prefix, not a guess.`,
      sources: ['network.observe'],
      edges: [
        {
          from: 'host',
          kind: 'plain',
          label: info.via.join(', '),
          why: `The host is attached to ${subnet} through ${info.via.join(', ')}.`,
        },
      ],
    });
  }

  // Published ports. `published` is Docker's own flag, and an unpublished exposed port is
  // deliberately not counted — exposure is the operationally interesting fact.
  const published = containers.flatMap((container) =>
    container.ports
      .filter((port) => port.published && port.host_port !== null)
      .map((port) => ({ container, port })),
  );
  if (published.length > 0) {
    const sample = published
      .slice(0, 3)
      .map((entry) => `${entry.port.host_port}/${entry.port.protocol}`)
      .join(' ');
    nodes.push({
      id: 'exposed',
      column: 3,
      name: 'Exposed',
      kind: 'listeners',
      mark: {
        tone: 'attention',
        label: 'reachable from off-host',
        description:
          'Docker reports these container ports as published to a host address. What can reach them is not established here.',
      },
      detail: `${published.length} published`,
      note: `${sample}${published.length > 3 ? ' …' : ''}`,
      to: '/workloads',
      evidence:
        'Docker reports these container ports as published to a host address. What can actually reach them from off-host is not established here — no reachability probe exists in this build.',
      sources: ['docker.containers.observe'],
      edges: [
        {
          from: 'host',
          kind: 'plain',
          label: `${published.length} published`,
          why: `${published.length} container port${published.length === 1 ? ' is' : 's are'} published to a host address.`,
        },
      ],
    });
  }

  // An unread estate is stated, not implied by an empty column. Without this a Docker socket
  // that could not be opened would look exactly like a host running nothing.
  if (!estateRead.interfaces || !estateRead.containers) {
    const unread = [
      estateRead.interfaces ? null : 'interfaces',
      estateRead.containers ? null : 'containers',
    ].filter(Boolean);
    nodes.push({
      id: 'estate-unread',
      column: 3,
      name: 'not read',
      kind: unread.join(' · '),
      mark: notKnown(),
      detail: null,
      note: `This console could not read ${unread.join(' or ')}. What is missing from this column is unknown, not absent.`,
      evidence: `The ${unread.join(' and ')} read failed. This column is incomplete by an unknown amount.`,
      edges: [
        {
          from: 'host',
          kind: 'standby',
          why: `${unread.join(' and ')} could not be read, so what attaches here is unknown.`,
        },
      ],
    });
  }

  return capAttached(nodes);
}

/**
 * Keep the Attached column to a readable height.
 *
 * The plane's columns hold about three or four nodes. This host has seven container networks, a
 * LAN and an exposure summary, and drawing all of them turns the plane into a scrolling
 * list — which stops it reading as a diagram at all. The least interesting nodes are folded
 * into one that states how many were folded, so the count stays true and the full list is
 * one click away.
 */
function capAttached(nodes: TopologyNode[]): TopologyNode[] {
  const attached = nodes.filter((node) => node.column === 3);
  if (attached.length <= ATTACHED_NODE_LIMIT) return nodes;

  const rest = nodes.filter((node) => node.column !== 3);
  const kept = attached.slice(0, ATTACHED_NODE_LIMIT - 1);
  const folded = attached.slice(ATTACHED_NODE_LIMIT - 1);

  return [
    ...rest,
    ...kept,
    {
      id: 'attached:more',
      column: 3,
      name: `${folded.length} more`,
      kind: 'attached',
      mark: {
        tone: 'neutral',
        label: 'observed',
        description: 'Further attached networks and subnets, listed in full on the Network surface.',
      },
      detail: null,
      note: folded
        .map((node) => node.name)
        .slice(0, 4)
        .join(' · '),
      to: '/network',
      evidence: `${folded.length} further attached network${folded.length === 1 ? '' : 's'} are not drawn, to keep the plane readable. They are listed in full on the Network surface.`,
      edges: [
        {
          from: 'host',
          kind: 'plain',
          why: `${folded.length} further attachment${folded.length === 1 ? '' : 's'} folded into one node.`,
        },
      ],
    },
  ];
}
