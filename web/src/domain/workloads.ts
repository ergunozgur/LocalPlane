/**
 * Applications, and the containers they are made of.
 *
 * The Workloads surface is built on a claim this product makes deliberately: **a workload
 * is a thing being run, and the runtime is only how it is being run.** An Application is the
 * first-class object — a named thing an operator operates — and its containers are the parts
 * it is made of. Reducing that to a flat container list loses the domain and keeps only the
 * mechanism.
 *
 * The grouping is evidence-backed rather than inferred from names. The backend keeps a
 * deliberate allowlist of labels, and it happens to be exactly what this model needs:
 *
 *   com.docker.compose.project               the application's name
 *   com.docker.compose.service               the part this container plays in it
 *   com.docker.compose.container-number      which replica of that part
 *   com.docker.compose.project.config_files  the declaration it came from
 *   com.docker.compose.project.working_dir   where that declaration lives
 *   com.docker.compose.config-hash           the digest of the declaration applied
 *
 * A container with no project label is not forced into one: it becomes a standalone
 * application of exactly itself.
 */
import type { DockerContainer, Sweep } from '@/api/types';

export const LABEL_PROJECT = 'com.docker.compose.project';
export const LABEL_SERVICE = 'com.docker.compose.service';
export const LABEL_CONTAINER_NUMBER = 'com.docker.compose.container-number';
export const LABEL_CONFIG_FILES = 'com.docker.compose.project.config_files';
export const LABEL_WORKING_DIR = 'com.docker.compose.project.working_dir';
export const LABEL_CONFIG_HASH = 'com.docker.compose.config-hash';

export type ApplicationOrigin = 'compose' | 'standalone';

export interface Application {
  /** Stable id: the compose project, or the container's own object id when standalone. */
  readonly id: string;
  readonly name: string;
  readonly origin: ApplicationOrigin;
  readonly containers: readonly DockerContainer[];
  readonly running: number;
  /** The declaration this application came from, when compose named one. */
  readonly configFile: string | null;
  readonly workingDir: string | null;
  readonly configHash: string | null;
  /** Distinct compose services within the application. Empty for a standalone container. */
  readonly services: readonly string[];
  /** Distinct docker network names the containers are attached to. */
  readonly networks: readonly string[];
}

function first(containers: readonly DockerContainer[], label: string): string | null {
  for (const container of containers) {
    const value = container.labels[label];
    if (value) return value;
  }
  return null;
}

/**
 * Group containers into applications.
 *
 * Order is stable and useful rather than alphabetical: compose projects first, then
 * standalone containers, each by name — which keeps the things an operator declared above
 * the things that merely exist.
 */
export function groupIntoApplications(
  containers: readonly DockerContainer[],
): readonly Application[] {
  const byProject = new Map<string, DockerContainer[]>();
  const standalone: DockerContainer[] = [];

  for (const container of containers) {
    const project = container.labels[LABEL_PROJECT];
    if (project) {
      const existing = byProject.get(project);
      if (existing) existing.push(container);
      else byProject.set(project, [container]);
    } else {
      standalone.push(container);
    }
  }

  const applications: Application[] = [];

  for (const [project, members] of byProject) {
    applications.push(buildApplication(project, project, 'compose', members));
  }
  for (const container of standalone) {
    applications.push(
      buildApplication(container.object_id, container.name, 'standalone', [container]),
    );
  }

  return applications.sort((a, b) => {
    if (a.origin !== b.origin) return a.origin === 'compose' ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
}

function buildApplication(
  id: string,
  name: string,
  origin: ApplicationOrigin,
  containers: readonly DockerContainer[],
): Application {
  const ordered = [...containers].sort((a, b) => {
    const sa = a.labels[LABEL_SERVICE] ?? a.name;
    const sb = b.labels[LABEL_SERVICE] ?? b.name;
    return sa.localeCompare(sb) || a.name.localeCompare(b.name);
  });

  const services = [
    ...new Set(ordered.map((c) => c.labels[LABEL_SERVICE]).filter((v): v is string => Boolean(v))),
  ];
  const networks = [
    ...new Set(ordered.flatMap((c) => c.networks.map((n) => n.name))),
  ];

  return {
    id,
    name,
    origin,
    containers: ordered,
    running: ordered.filter((c) => c.runtime.state === 'running').length,
    configFile: first(ordered, LABEL_CONFIG_FILES),
    workingDir: first(ordered, LABEL_WORKING_DIR),
    configHash: first(ordered, LABEL_CONFIG_HASH),
    services,
    networks,
  };
}

/** The application a container belongs to, for breadcrumbs on a detail page. */
export function applicationOf(container: DockerContainer): {
  name: string;
  origin: ApplicationOrigin;
  service: string | null;
} {
  const project = container.labels[LABEL_PROJECT];
  return {
    name: project ?? container.name,
    origin: project ? 'compose' : 'standalone',
    service: container.labels[LABEL_SERVICE] ?? null,
  };
}

/* ------------------------------------------------------------------- engine and images */

export interface EngineFacts {
  /** Reported by the observing provider, e.g. `docker 29.1.3`. */
  readonly provider: string | null;
  readonly version: string | null;
  readonly containersTotal: number | null;
  readonly containersRunning: number | null;
  readonly observedAt: string | null;
}

/**
 * What is actually known about the engine.
 *
 * The Engine plate lists storage driver, cgroup version, image and volume counts and
 * live-restore. None of those has a contract here — the agent reads containers and networks
 * and does not call `docker system info` — so they are absent from this type rather than
 * present and empty. The plate states them as unavailable; it does not receive nulls to
 * render as though they had been looked for.
 */
export function engineFacts(sweep: Sweep | null, containers: readonly DockerContainer[] | null): EngineFacts {
  return {
    provider: sweep?.provider ?? null,
    version: sweep?.provider_version ?? null,
    containersTotal: containers?.length ?? null,
    containersRunning: containers?.filter((c) => c.runtime.state === 'running').length ?? null,
    observedAt: sweep?.completed_at ?? null,
  };
}

export interface ImageInUse {
  readonly reference: string;
  readonly imageId: string | null;
  readonly usedBy: readonly string[];
}

/**
 * The images these containers run from.
 *
 * Derived from the containers themselves, because there is no image endpoint. That means it
 * is honestly "images in use", not the daemon's image list: an image pulled but not running
 * cannot appear here, and neither can a size, since nothing reports one.
 */
export function imagesInUse(containers: readonly DockerContainer[]): readonly ImageInUse[] {
  const byReference = new Map<string, { imageId: string | null; usedBy: string[] }>();
  for (const container of containers) {
    const reference = container.image.reference;
    if (!reference) continue;
    const existing = byReference.get(reference);
    if (existing) existing.usedBy.push(container.name);
    else byReference.set(reference, { imageId: container.image.image_id, usedBy: [container.name] });
  }
  return [...byReference.entries()]
    .map(([reference, value]) => ({ reference, imageId: value.imageId, usedBy: value.usedBy }))
    .sort((a, b) => a.reference.localeCompare(b.reference));
}

export interface RuntimeRow {
  readonly name: string;
  /** `observed` and `absent` are answers; `unsupported` means this build cannot tell. */
  readonly detection: 'observed' | 'absent' | 'unsupported';
  readonly workloads: number | null;
  readonly note: string;
}

/**
 * Which runtimes are actually behind the workloads on this host.
 *
 * Compose-backed and standalone Docker are distinguishable from evidence the backend already
 * publishes. The others are the point of the Engine plate — that Workloads is the domain and
 * Docker is only what is present today — but this build has no detector for them, so they
 * are `unsupported`: not "absent", which would be a claim that they are not installed.
 */
export function runtimeRows(applications: readonly Application[] | null): readonly RuntimeRow[] {
  const compose = applications?.filter((a) => a.origin === 'compose') ?? null;
  const standalone = applications?.filter((a) => a.origin === 'standalone') ?? null;

  return [
    {
      name: 'docker compose',
      detection: compose === null ? 'unsupported' : compose.length > 0 ? 'observed' : 'absent',
      workloads: compose?.length ?? null,
      note:
        compose && compose.length > 0
          ? `${compose.length} project${compose.length === 1 ? '' : 's'}, from compose labels on the containers`
          : 'no container carries a compose project label',
    },
    {
      name: 'docker',
      detection:
        standalone === null ? 'unsupported' : standalone.length > 0 ? 'observed' : 'absent',
      workloads: standalone?.length ?? null,
      note:
        standalone && standalone.length > 0
          ? 'standalone containers, observed only'
          : 'every container belongs to a compose project',
    },
    {
      name: 'podman',
      detection: 'unsupported',
      workloads: null,
      note: 'no provider reads podman in this build, so its absence here is not evidence it is absent',
    },
    {
      name: 'systemd-supervised',
      detection: 'unsupported',
      workloads: null,
      note: 'native workloads adopted from units would live here rather than under System',
    },
    {
      name: 'kubelet',
      detection: 'unsupported',
      workloads: null,
      note: 'no provider reads a kubelet in this build',
    },
  ];
}
