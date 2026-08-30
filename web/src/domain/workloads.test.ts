/**
 * Application grouping.
 *
 * The claim under test is the product's: an application is the first-class object and a
 * container is a part of it. The grouping must come from the labels the backend publishes,
 * never from names — two containers that merely look related are not an application.
 */
import { describe, expect, it } from 'vitest';
import {
  engineFacts,
  groupIntoApplications,
  imagesInUse,
  runtimeRows,
  applicationOf,
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
  it('groups containers into one application by their compose project', () => {
    const apps = groupIntoApplications([
      container('grafana', { [LABEL_PROJECT]: 'monitoring', [LABEL_SERVICE]: 'grafana' }),
      container('prometheus', { [LABEL_PROJECT]: 'monitoring', [LABEL_SERVICE]: 'prometheus' }),
    ]);
    expect(apps).toHaveLength(1);
    expect(apps[0]?.name).toBe('monitoring');
    expect(apps[0]?.origin).toBe('compose');
    expect(apps[0]?.containers).toHaveLength(2);
    expect(apps[0]?.services).toEqual(['grafana', 'prometheus']);
  });

  it('does not group containers whose names merely look related', () => {
    // No compose labels: these are two standalone applications, not one.
    const apps = groupIntoApplications([
      container('monitoring-a'),
      container('monitoring-b'),
    ]);
    expect(apps).toHaveLength(2);
    expect(apps.every((a) => a.origin === 'standalone')).toBe(true);
  });

  it('keeps a container without a project as an application of itself', () => {
    const apps = groupIntoApplications([container('portainer')]);
    expect(apps[0]?.origin).toBe('standalone');
    expect(apps[0]?.name).toBe('portainer');
    expect(apps[0]?.configFile).toBeNull();
  });

  it('carries the declaration the compose project came from', () => {
    const apps = groupIntoApplications([
      container('grafana', {
        [LABEL_PROJECT]: 'monitoring',
        [LABEL_CONFIG_FILES]: '/srv/compose/monitoring/docker-compose.yml',
      }),
    ]);
    expect(apps[0]?.configFile).toBe('/srv/compose/monitoring/docker-compose.yml');
  });

  it('counts only running containers as running', () => {
    const apps = groupIntoApplications([
      container('a', { [LABEL_PROJECT]: 'p' }, 'running'),
      container('b', { [LABEL_PROJECT]: 'p' }, 'exited'),
    ]);
    expect(apps[0]?.running).toBe(1);
    expect(apps[0]?.containers).toHaveLength(2);
  });

  it('lists compose applications before standalone ones', () => {
    const apps = groupIntoApplications([
      container('zzz-standalone'),
      container('aaa', { [LABEL_PROJECT]: 'project' }),
    ]);
    expect(apps[0]?.origin).toBe('compose');
    expect(apps[1]?.origin).toBe('standalone');
  });
});

describe('applicationOf', () => {
  it('names the project and the service a container plays in it', () => {
    const result = applicationOf(
      container('grafana', { [LABEL_PROJECT]: 'monitoring', [LABEL_SERVICE]: 'grafana' }),
    );
    expect(result).toEqual({ name: 'monitoring', origin: 'compose', service: 'grafana' });
  });

  it('reports a standalone container as its own application with no service', () => {
    expect(applicationOf(container('portainer'))).toEqual({
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
  it('reports compose and standalone docker from real evidence', () => {
    const apps = groupIntoApplications([
      container('a', { [LABEL_PROJECT]: 'p' }),
      container('b'),
    ]);
    const rows = runtimeRows(apps);
    expect(rows.find((r) => r.name === 'docker compose')?.detection).toBe('observed');
    expect(rows.find((r) => r.name === 'docker')?.detection).toBe('observed');
  });

  it('never claims an undetectable runtime is absent', () => {
    const rows = runtimeRows(groupIntoApplications([container('a')]));
    for (const name of ['podman', 'systemd-supervised', 'kubelet']) {
      const row = rows.find((r) => r.name === name);
      expect(row?.detection).toBe('unsupported');
      expect(row?.workloads).toBeNull();
      // "not detected by this build" is a statement about the build, not about the host.
      expect(row?.note).toMatch(/this build|would live here/i);
    }
  });

  it('reports every runtime as undetectable when nothing was read', () => {
    const rows = runtimeRows(null);
    expect(rows.every((r) => r.detection === 'unsupported')).toBe(true);
  });
});
