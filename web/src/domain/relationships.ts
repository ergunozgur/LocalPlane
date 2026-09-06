/**
 * Evidence-backed relationships for the interface and container workspaces.
 *
 * This module deliberately joins only on identifiers published by the backend. Names are
 * displayed, but never used to discover a relationship (the kernel's `link.master` is the
 * one exception: it is itself the published master identifier).
 */
import type { DockerContainer, ManagementPath, NetworkInterface } from '@/api/types';

export interface RelationshipTarget {
  kind: string;
  name: string;
  objectId: string | null;
  href: string | null;
  unresolved?: boolean;
}

export interface ObjectRelationship {
  type: string;
  target: RelationshipTarget;
  evidence: string;
  guarded?: boolean;
}

export interface RelationshipInput {
  subject: NetworkInterface | DockerContainer;
  interfaces: readonly NetworkInterface[];
  containers: readonly DockerContainer[];
  managementPath: ManagementPath | null;
}

function networkTarget(networkId: string | null, name: string): RelationshipTarget {
  return networkId
    ? { kind: 'Docker network', name: name || networkId, objectId: networkId, href: null }
    : {
        kind: 'Docker network',
        name: `unresolved Docker network${name ? ` ${name}` : ''}`,
        objectId: null,
        href: null,
        unresolved: true,
      };
}

function interfaceTarget(item: NetworkInterface): RelationshipTarget {
  return {
    kind: item.interface_kind,
    name: item.name,
    objectId: item.object_id,
    href: `/network/${encodeURIComponent(item.object_id)}`,
  };
}

function containerTarget(item: DockerContainer): RelationshipTarget {
  return {
    kind: 'container',
    name: item.name,
    objectId: item.object_id,
    href: `/workloads/${encodeURIComponent(item.object_id)}`,
  };
}

function isInterface(subject: NetworkInterface | DockerContainer): subject is NetworkInterface {
  return subject.kind === 'network.interface';
}

function interfaceRelationships(input: RelationshipInput): ObjectRelationship[] {
  const subject = input.subject as NetworkInterface;
  const relationships: ObjectRelationship[] = [];
  const createdBy = subject.ownership.created_by;

  // Narrowed on `createdBy` rather than on `owner`: the evidence sentence below reads
  // `createdBy.reason`, and narrowing the derived `owner` does not tell TypeScript that the
  // attribution it came from is present.
  if (createdBy && createdBy.owner && createdBy.owner.provider === 'docker') {
    const owner = createdBy.owner;
    const networkId = owner.instance || null;
    const networkName = owner.label ?? '';
    relationships.push({
      type: 'attributed Docker network',
      target: networkTarget(networkId, networkName),
      evidence: networkId
        ? `ownership.created_by.owner.instance=${networkId}; ownership.created_by.owner.label=${owner.label ?? 'null'}; evidence=${createdBy.reason}.`
        : 'ownership.created_by.owner.instance was not published; the Docker network is unresolved.',
    });

    if (networkId) {
      for (const container of input.containers) {
        if (!container.networks.some((network) => network.network_id === networkId)) continue;
        relationships.push({
          type: 'contains container',
          target: containerTarget(container),
          evidence: `containers[].networks[].network_id=${networkId} matches ownership.created_by.owner.instance.`,
        });
      }
    }
  }

  const masterName = subject.link?.master;
  if (masterName) {
    // Filtered rather than found. If the estate ever published two interfaces under one name
    // the identifier does not identify anything, and naming the first match would be the one
    // place this module could state a confidently wrong object. Ambiguity degrades to
    // unresolved, which is how every other unmatched identifier here already behaves.
    const masters = input.interfaces.filter((item) => item.name === masterName);
    const master = masters.length === 1 ? masters[0] : undefined;
    relationships.push({
      type: 'member of interface',
      target: master
        ? interfaceTarget(master)
        : {
            kind: 'interface',
            name: `unresolved interface ${masterName}`,
            objectId: null,
            href: null,
            unresolved: true,
          },
      evidence: master
        ? `link.master=${masterName} matches interface.name=${master.name}; object_id=${master.object_id}.`
        : masters.length > 1
          ? `link.master=${masterName} matched ${masters.length} interfaces, so it does not identify one.`
          : `link.master=${masterName} was published, but no matching interface was read.`,
    });
  }

  const managementPath = input.managementPath;
  if (managementPath?.state === 'confirmed' && managementPath.object_id === subject.object_id) {
    relationships.push({
      type: 'carries management path',
      target: interfaceTarget(subject),
      evidence: `management_path.object_id=${managementPath.object_id} and state=confirmed.`,
      guarded: true,
    });
  }

  return relationships;
}

function containerRelationships(input: RelationshipInput): ObjectRelationship[] {
  const subject = input.subject as DockerContainer;
  const relationships: ObjectRelationship[] = [];
  const bridges = input.interfaces.filter(
    (item) => item.ownership.created_by?.owner?.provider === 'docker',
  );

  for (const network of subject.networks) {
    const networkId = network.network_id;
    // Same rule as link.master above: two bridges claiming one network id identify nothing.
    const matchingBridges = networkId
      ? bridges.filter((item) => item.ownership.created_by?.owner?.instance === networkId)
      : [];
    const bridge = matchingBridges.length === 1 ? matchingBridges[0] : undefined;
    const target = networkTarget(networkId, network.name);

    relationships.push({
      type: 'connected to Docker network',
      target,
      evidence: networkId
        ? `networks[].network_id=${networkId}; Docker published network name=${network.name}.`
        : `networks[].network_id is null for Docker network name=${network.name}; the network is unresolved.`,
    });

    if (networkId && matchingBridges.length > 0) {
      relationships.push({
        type: 'is carried by host bridge',
        target: bridge
          ? interfaceTarget(bridge)
          : {
              kind: 'bridge',
              name: `ambiguous host bridge for network ${networkId}`,
              objectId: null,
              href: null,
              unresolved: true,
            },
        evidence: bridge
          ? `ownership.created_by.owner.instance=${networkId} matches networks[].network_id.`
          : `${matchingBridges.length} interfaces claim ownership.created_by.owner.instance=${networkId}, so the host bridge is not identified.`,
      });
    }

    if (networkId) {
      for (const sibling of input.containers) {
        if (sibling.object_id === subject.object_id) continue;
        if (!sibling.networks.some((candidate) => candidate.network_id === networkId)) continue;
        relationships.push({
          type: 'shares Docker network',
          target: containerTarget(sibling),
          evidence: `sibling networks[].network_id=${networkId} matches this container's networks[].network_id.`,
        });
      }
    }
  }

  return relationships;
}

export function deriveRelationships(input: RelationshipInput): ObjectRelationship[] {
  return isInterface(input.subject) ? interfaceRelationships(input) : containerRelationships(input);
}
