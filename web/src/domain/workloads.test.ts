/**
 * Provider-derived container grouping.
 *
 * Grouping must come from labels the backend publishes, never from names. The result is a
 * presentation projection and does not claim durable LocalPlane Application identity.
 */
import { describe, expect, it } from 'vitest';
import {
  engineFacts,
  groupContainers,
  imagesInUse,
  runtimeRows,
  containerGroupOf,
  LABEL_CONFIG_FILES,
  LABEL_PROJECT,
  LABEL_SERVICE,
} from './workloads';
import type { DockerContainer, Sweep } from '@/api/types';

function container(
  name: string,
  labels: Record<string, string> = {},
  state = 'running',
  image = 'example/image:1',
): DockerContainer {
  return {
    object_id: `obj_${name}`,
    name,
    runtime: { state },
    image: { reference: image, image_id: `sha256:${name}` },
    labels,
    networks: [],
    ports: [],
    container_health: { checked: false, status: null, failing_streak: null },
    management: { state: 'observed', reason: 'observe_only' },
  } as unknown as DockerContainer;
}

describe('grouping', () => {
  it('groups containers by their Compose project label', () => {
    const groups = groupContainers([
      container('grafana', { [LABEL_PROJECT]: 'monitoring', [LABEL_SERVICE]: 'grafana' }),
      container('prometheus', { [LABEL_PROJECT]: 'monitoring', [LABEL_SERVICE]: 'prometheus' }),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.name).toBe('monitoring');
    expect(groups[0]?.origin).toBe('compose');
    expect(groups[0]?.containers).toHaveLength(2);
    expect(groups[0]?.services).toEqual(['grafana', 'prometheus']);
  });

  it('does not group containers whose names merely look related', () => {
    // No Compose labels: these remain two standalone containers, not one inferred group.
    const groups = groupContainers([
      container('monitoring-a'),
      container('monitoring-b'),
    ]);
    expect(groups).toHaveLength(2);
    expect(groups.every((group) => group.origin === 'standalone')).toBe(true);
  });

  it('keeps a container without a project as an individual presentation row', () => {
    const groups = groupContainers([container('portainer')]);
    expect(groups[0]?.origin).toBe('standalone');
    expect(groups[0]?.name).toBe('portainer');
    expect(groups[0]?.configFile).toBeNull();
  });

  it('carries the declaration the compose project came from', () => {
    const groups = groupContainers([
      container('grafana', {
        [LABEL_PROJECT]: 'monitoring',
        [LABEL_CONFIG_FILES]: '/srv/compose/monitoring/docker-compose.yml',
      }),
    ]);
    expect(groups[0]?.configFile).toBe('/srv/compose/monitoring/docker-compose.yml');
  });

  it('counts only running containers as running', () => {
    const groups = groupContainers([
      container('a', { [LABEL_PROJECT]: 'p' }, 'running'),
      container('b', { [LABEL_PROJECT]: 'p' }, 'exited'),
    ]);
    expect(groups[0]?.running).toBe(1);
    expect(groups[0]?.containers).toHaveLength(2);
  });

  it('lists Compose groups before standalone containers', () => {
    const groups = groupContainers([
      container('zzz-standalone'),
      container('aaa', { [LABEL_PROJECT]: 'project' }),
    ]);
    expect(groups[0]?.origin).toBe('compose');
    expect(groups[1]?.origin).toBe('standalone');
  });
});

describe('containerGroupOf', () => {
  it('names the project and the service a container plays in it', () => {
    const result = containerGroupOf(
      container('grafana', { [LABEL_PROJECT]: 'monitoring', [LABEL_SERVICE]: 'grafana' }),
    );
    expect(result).toEqual({ name: 'monitoring', origin: 'compose', service: 'grafana' });
  });

  it('reports an unlabelled container with no project or service', () => {
    expect(containerGroupOf(container('portainer'))).toEqual({
      name: 'portainer',
      origin: 'standalone',
      service: null,
    });
  });
});

describe('engine facts', () => {
  const sweep = {
    provider: 'docker',
    provider_version: '29.1.3',
    completed_at: '2026-08-28T08:00:00Z',
  } as unknown as Sweep;

  it('reports only what the sweep and container list actually supply', () => {
    const facts = engineFacts(sweep, [container('a'), container('b', {}, 'exited')]);
    expect(facts.provider).toBe('docker');
    expect(facts.version).toBe('29.1.3');
    expect(facts.containersRunning).toBe(1);
    expect(facts.containersTotal).toBe(2);
  });

  it('reports nulls rather than zeroes when nothing was read', () => {
    const facts = engineFacts(null, null);
    expect(facts.version).toBeNull();
    expect(facts.containersTotal).toBeNull();
    expect(facts.containersRunning).toBeNull();
  });
});

describe('images in use', () => {
  it('lists each image once with the containers using it', () => {
    const images = imagesInUse([
      container('a', {}, 'running', 'repo/one:1'),
      container('b', {}, 'running', 'repo/one:1'),
      container('c', {}, 'running', 'repo/two:2'),
    ]);
    expect(images).toHaveLength(2);
    expect(images[0]?.reference).toBe('repo/one:1');
    expect(images[0]?.usedBy).toEqual(['a', 'b']);
  });
});

describe('runtimes', () => {
  const inventory = (status: string, issues: Sweep['issues'] = []) =>
    ({ scope: 'inventory', status, issues } as Sweep);

  it('reports compose and standalone docker from real evidence', () => {
    const groups = groupContainers([
      container('a', { [LABEL_PROJECT]: 'p' }),
      container('b'),
    ]);
    const rows = runtimeRows(groups, inventory('partial'));
    expect(rows.find((r) => r.name === 'docker compose')?.detection).toBe('observed');
    expect(rows.find((r) => r.name === 'docker')?.detection).toBe('observed');
  });

  it('never claims an undetectable runtime is absent', () => {
    const rows = runtimeRows(groupContainers([container('a')]), inventory('ok'));
    for (const name of ['podman', 'systemd-supervised', 'kubelet']) {
      const row = rows.find((r) => r.name === name);
      expect(row?.detection).toBe('unsupported');
      expect(row?.observedGroups).toBeNull();
      // "not detected by this build" is a statement about the build, not about the host.
      expect(row?.note).toMatch(/this build|would live here/i);
    }
  });

  it.each([
    ['no sweep', null],
    ['failed sweep', inventory('failed')],
    ['partial sweep', inventory('partial')],
  ] as const)('keeps empty Docker groups unknown with %s', (_label, sweep) => {
    const rows = runtimeRows([], sweep);
    for (const name of ['docker compose', 'docker']) {
      const row = rows.find((candidate) => candidate.name === name);
      expect(row?.detection).toBe('unknown');
      expect(row?.observedGroups).toBe(0);
      expect(row?.note).toMatch(/completeness is unknown/i);
    }
    for (const name of ['podman', 'systemd-supervised', 'kubelet']) {
      expect(rows.find((candidate) => candidate.name === name)?.detection).toBe('unsupported');
    }
  });

  it('only describes empty Docker groups as none observed for a complete inventory', () => {
    const rows = runtimeRows([], inventory('ok'));
    for (const name of ['docker compose', 'docker']) {
      const row = rows.find((candidate) => candidate.name === name);
      expect(row?.detection).toBe('absent');
      expect(row?.note).toMatch(/complete inventory/i);
      expect(row?.note).not.toMatch(/installed|installation/i);
    }
  });

  it('preserves positive label evidence even when the inventory is partial', () => {
    const rows = runtimeRows(
      groupContainers([container('observed-compose', { [LABEL_PROJECT]: 'project' })]),
      inventory('partial'),
    );
    expect(rows.find((row) => row.name === 'docker compose')?.detection).toBe('observed');
    expect(rows.find((row) => row.name === 'docker compose')?.observedGroups).toBe(1);
    expect(rows.find((row) => row.name === 'docker')?.detection).toBe('unknown');
  });

  it('keeps Docker detector status unknown when group data is unavailable', () => {
    const rows = runtimeRows(null, null);
    for (const name of ['docker compose', 'docker']) {
      expect(rows.find((row) => row.name === name)?.detection).toBe('unknown');
    }
    for (const name of ['podman', 'systemd-supervised', 'kubelet']) {
      expect(rows.find((row) => row.name === name)?.detection).toBe('unsupported');
    }
  });
});
