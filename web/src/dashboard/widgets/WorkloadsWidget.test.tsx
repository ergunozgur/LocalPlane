import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { WorkloadsWidget } from './WorkloadsWidget';
import { BACKEND, stubBackend } from '@/test/backend';
import type { DockerContainerList } from '@/api/types';

afterEach(() => vi.unstubAllGlobals());

const before = BACKEND['/api/v1/docker/containers'] as DockerContainerList;

function renderWidget(list: DockerContainerList): void {
  stubBackend({ '/api/v1/docker/containers': list });
  render(<MemoryRouter><WorkloadsWidget /></MemoryRouter>);
}

describe('WorkloadsWidget empty evidence', () => {
  it.each([
    ['complete', { status: 'ok', issues: [] }, 'A complete stored Docker inventory recorded no containers. This does not describe a live daemon read.'],
    ['partial', { status: 'partial', issues: [{ source: 'docker.containers', code: 'inspect_failed', message: 'one inspection failed', detail: {} }] }, 'The most recent Docker inventory was incomplete or has an unrecognised status, so an empty stored list does not establish that no containers exist.'],
    ['failed', { status: 'failed', issues: [] }, 'The most recent Docker inventory failed, so an empty stored list does not establish that no containers exist.'],
  ])('does not infer an empty daemon for a %s sweep', async (_kind, patch, expected) => {
    renderWidget({
      ...before,
      count: 0,
      containers: [],
      last_sweep: { ...before.last_sweep!, ...patch },
    });

    expect(await screen.findByText(expected)).toBeInTheDocument();
    expect(screen.queryByText('The Docker daemon answered and reported none.')).not.toBeInTheDocument();
  });
});
