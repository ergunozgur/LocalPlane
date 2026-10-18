import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { WorkloadRuntime } from './WorkloadRuntime';
import { BACKEND, stubBackend } from '@/test/backend';
import type { DockerContainer, DockerContainerList, Sweep } from '@/api/types';

const baseList = BACKEND['/api/v1/docker/containers'] as DockerContainerList;
const baseContainer = BACKEND['/api/v1/docker/containers/obj_ct'] as DockerContainer;
const baseSweep = baseList.last_sweep as Sweep;

afterEach(() => {
  vi.unstubAllGlobals();
});

function makeSweep(
  status: Sweep['status'],
  issues: Sweep['issues'] = [],
): Sweep {
  return { ...baseSweep, scope: 'inventory', status, issues };
}

function renderRuntime(sweep: Sweep | null, containers: DockerContainer[] = []): void {
  const list: DockerContainerList = {
    ...baseList,
    last_sweep: sweep,
    count: containers.length,
    containers,
  };
  stubBackend({ '/api/v1/docker/containers': list });
  render(
    <MemoryRouter initialEntries={['/workloads/runtime']}>
      <Routes>
        <Route path="/workloads/runtime" element={<WorkloadRuntime />} />
      </Routes>
    </MemoryRouter>,
  );
}

function runtimeRow(name: string): HTMLElement {
  return within(screen.getByRole('table', { name: 'Runtimes' }))
    .getByText(name, { selector: 'span' })
    .closest('tr') as HTMLElement;
}

describe('WorkloadRuntime inventory evidence', () => {
  it('keeps empty groups unknown when there is no inventory sweep', async () => {
    renderRuntime(null);

    await screen.findByText('docker compose');
    expect(screen.getByText('No completed container inventory is recorded')).toBeInTheDocument();
    for (const name of ['docker compose', 'docker']) {
      expect(within(runtimeRow(name)).getByText('unknown')).toBeInTheDocument();
      expect(within(runtimeRow(name)).getByText(/inventory completeness is unknown/i)).toBeInTheDocument();
    }
    expect(within(runtimeRow('podman')).getByText(/not detected by this build/i)).toBeInTheDocument();
  });

  it('does not describe the no-sweep state as empty when containers were returned', async () => {
    renderRuntime(null, [baseContainer]);

    await screen.findByText('docker compose');
    expect(screen.getByText('No completed container inventory is recorded')).toBeInTheDocument();
    expect(screen.getByText('Runtime-group assessment is limited to the containers returned in this read.')).toBeInTheDocument();
    expect(within(runtimeRow('docker compose')).getByText('yes')).toBeInTheDocument();
    expect(within(runtimeRow('docker compose')).getByText(/1 project, from compose labels on the containers/i)).toBeInTheDocument();
    expect(screen.queryByText('No runtime groups were returned.')).not.toBeInTheDocument();
  });

  it.each([
    ['failed', makeSweep('failed')],
    ['partial', makeSweep('partial', [{
      source: 'docker.containers',
      code: 'partial_read',
      message: 'some containers unreadable',
      detail: {},
    }])],
  ] as const)('shows sweep caveat and unknown empty-group status for a %s inventory', async (_status, sweep) => {
    renderRuntime(sweep);

    await screen.findByText('docker compose');
    expect(screen.getByText('Observation was not complete')).toBeInTheDocument();
    expect(within(runtimeRow('docker compose')).getByText('unknown')).toBeInTheDocument();
    expect(within(runtimeRow('docker')).getByText('unknown')).toBeInTheDocument();
  });

  it('describes zero groups only as none observed in a complete inventory', async () => {
    renderRuntime(makeSweep('ok'));

    await screen.findByText('docker compose');
    expect(within(runtimeRow('docker compose')).getByText('none observed')).toBeInTheDocument();
    expect(within(runtimeRow('docker')).getByText('none observed')).toBeInTheDocument();
    expect(screen.queryByText(/^no$/)).not.toBeInTheDocument();
  });

  it('preserves positive provider evidence in a partial inventory', async () => {
    const composeContainer: DockerContainer = {
      ...baseContainer,
      labels: { ...baseContainer.labels, 'com.docker.compose.project': 'observed-project' },
    };
    renderRuntime(makeSweep('partial'), [composeContainer]);

    await screen.findByText('docker compose');
    expect(screen.getByText('Observation was not complete')).toBeInTheDocument();
    expect(within(runtimeRow('docker compose')).getByText('yes')).toBeInTheDocument();
    expect(within(runtimeRow('docker compose')).getByText('1 project, from compose labels on the containers')).toBeInTheDocument();
    expect(within(runtimeRow('docker')).getByText('unknown')).toBeInTheDocument();
    expect(screen.getByText('grafana/grafana-oss:latest')).toBeInTheDocument();
  });
});
