/**
 * WorkloadList URL-driven Compose-project filtering.
 *
 * The existing WorkloadDetail breadcrumb links to `/workloads?group=<project>`. These tests
 * assert that the list now honours that parameter: exact case-sensitive provider label only,
 * standalone name collisions excluded, empty/unknown groups handled truthfully, history
 * restoration without a refetch, and clearing the filter while preserving unrelated params.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from 'react-router-dom';
import { WorkloadList } from './WorkloadList';
import { WorkloadDetail } from './WorkloadDetail';
import { BACKEND, stubBackend, urlOf } from '@/test/backend';
import {
  LABEL_PROJECT,
  LABEL_SERVICE,
} from '@/domain/workloads';
import type { DockerContainer, DockerContainerList, Sweep } from '@/api/types';

afterEach(() => vi.unstubAllGlobals());

function makeContainer(
  name: string,
  labels: Record<string, string> = {},
  state = 'running',
): DockerContainer {
  return {
    object_id: `obj_${name}`,
    name,
    runtime: { state },
    image: { reference: 'example/image:1', image_id: `sha256:${name}` },
    labels,
    networks: [],
    ports: [],
    container_health: { checked: false, status: null, failing_streak: null },
    management: { state: 'observed', reason: 'observe_only' },
  } as unknown as DockerContainer;
}

const MIXED_LIST: DockerContainerList = {
  ...(BACKEND['/api/v1/docker/containers'] as DockerContainerList),
  count: 5,
  containers: [
    makeContainer('grafana', {
      [LABEL_PROJECT]: 'monitoring',
      [LABEL_SERVICE]: 'grafana',
    }),
    makeContainer('prometheus', {
      [LABEL_PROJECT]: 'monitoring',
      [LABEL_SERVICE]: 'prometheus',
    }),
    makeContainer('app-one', {
      [LABEL_PROJECT]: 'app',
      [LABEL_SERVICE]: 'app',
    }),
    // Same display name as a Compose project, but no project label: must stay standalone.
    makeContainer('monitoring'),
    makeContainer('nginx'),
  ],
};

function AddressProbe(): JSX.Element {
  const location = useLocation();
  return <span data-testid="address">{`${location.pathname}${location.search}`}</span>;
}

function renderAt(path: string, overrides: Record<string, unknown> = {}): ReturnType<typeof render> {
  stubBackend({ '/api/v1/docker/containers': MIXED_LIST, ...overrides });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="workloads" element={<WorkloadList />} />
      </Routes>
      <AddressProbe />
    </MemoryRouter>,
  );
}

function expectRow(name: string): void {
  expect(screen.getByRole('link', { name: new RegExp(name) })).toBeInTheDocument();
}

function expectNoRow(name: string): void {
  expect(screen.queryByRole('link', { name: new RegExp(name) })).not.toBeInTheDocument();
}

function clearFilterLink(): HTMLElement {
  return screen.getByRole('link', { name: /Show all groups/i });
}

describe('URL-driven group filter', () => {
  it('shows only the matching Compose project at /workloads?group=monitoring', async () => {
    renderAt('/workloads?group=monitoring');

    await waitFor(() => expectRow('grafana'));
    expectRow('prometheus');
    expectNoRow('app-one');
    expectNoRow('nginx');
    // The standalone container whose name coincides with the project must not appear.
    expectNoRow('monitoring');
    expect(screen.getByText(/Showing Compose project/i)).toBeInTheDocument();
    expect(clearFilterLink()).toHaveAttribute('href', '/workloads');
  });

  it('shows all groups at /workloads', async () => {
    renderAt('/workloads');

    await waitFor(() => expectRow('grafana'));
    expectRow('prometheus');
    expectRow('app-one');
    expectRow('nginx');
    expectRow('monitoring');
    expect(screen.queryByText(/Showing Compose project/i)).not.toBeInTheDocument();
  });

  it.each([
    ['missing group', '/workloads'],
    ['empty group', '/workloads?group='],
  ])('treats %s as an ordinary full list', async (_label, path) => {
    renderAt(path);

    await waitFor(() => expectRow('grafana'));
    expectRow('app-one');
    expectRow('nginx');
    expect(screen.queryByText(/No group matches/i)).not.toBeInTheDocument();
  });

  it('does not include a standalone container with the same name as the project', async () => {
    renderAt('/workloads?group=monitoring');

    await waitFor(() => expectRow('grafana'));
    expectRow('prometheus');
    expectNoRow('monitoring');
  });

  it('matches the exact case-sensitive project label only', async () => {
    renderAt('/workloads?group=Monitoring');

    await waitFor(() => expect(screen.getByText(/No group matches/i)).toBeInTheDocument());
    expectNoRow('grafana');
    expectNoRow('app-one');
  });

  it('decodes URL-encoded project labels exactly', async () => {
    const encodedList: DockerContainerList = {
      ...MIXED_LIST,
      count: 2,
      containers: [
        makeContainer('prod-a', { [LABEL_PROJECT]: 'monitoring/prod', [LABEL_SERVICE]: 'prod-a' }),
        makeContainer('prod-b', { [LABEL_PROJECT]: 'monitoring/prod', [LABEL_SERVICE]: 'prod-b' }),
      ],
    };
    stubBackend({ '/api/v1/docker/containers': encodedList });
    render(
      <MemoryRouter initialEntries={['/workloads?group=monitoring%2Fprod']}>
        <Routes>
          <Route path="workloads" element={<WorkloadList />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expectRow('prod-a'));
    expectRow('prod-b');
    expect(screen.getByText('monitoring/prod', { selector: 'strong' })).toBeInTheDocument();
  });

  it('uses the first value for repeated group parameters', async () => {
    renderAt('/workloads?group=monitoring&group=app');

    await waitFor(() => expectRow('grafana'));
    expectRow('prometheus');
    expectNoRow('app-one');
  });

  it('shows an explicit no-match for an unknown nonempty group', async () => {
    renderAt('/workloads?group=nosuch');

    await waitFor(() => expect(screen.getByText(/No group matches/i)).toBeInTheDocument());
    expect(screen.getByText(/No observed Compose project matches this filter/i)).toBeInTheDocument();
    expect(screen.getByText('nosuch', { selector: 'strong' })).toBeInTheDocument();
    expectNoRow('grafana');
  });

  it('shows a long special-character project label once as text with a clear-filter escape', async () => {
    const project = `${'a'.repeat(120)}<&+ö`;
    renderAt(`/workloads?group=${encodeURIComponent(project)}`);

    await screen.findByText('No group matches');
    expect(screen.getByText(project, { selector: 'strong' })).toBeInTheDocument();
    expect(screen.getAllByText(project, { exact: true })).toHaveLength(1);
    expect(screen.getByText('No observed Compose project matches this filter.')).toBeInTheDocument();
    expect(clearFilterLink()).toHaveAttribute('href', '/workloads');
  });
});

describe('clear filter and unrelated params', () => {
  it('clears only the group parameter while preserving unrelated params', async () => {
    const user = userEvent.setup();
    renderAt('/workloads?group=monitoring&keep=this');

    await waitFor(() => expectRow('grafana'));
    expectNoRow('app-one');

    await user.click(clearFilterLink());
    await waitFor(() =>
      expect(screen.getByTestId('address')).toHaveTextContent('/workloads?keep=this'),
    );
    expectRow('grafana');
    expectRow('app-one');
    expectRow('nginx');
  });
});

describe('history navigation', () => {
  it('restores scope via Back and Forward without fetching containers again', async () => {
    const user = userEvent.setup();

    function HistoryHarness(): JSX.Element {
      const navigate = useNavigate();
      const location = useLocation();
      return (
        <>
          <WorkloadList />
          <button type="button" onClick={() => navigate('/workloads?group=app')}>
            To app
          </button>
          <button type="button" onClick={() => navigate(-1)}>
            Back
          </button>
          <button type="button" onClick={() => navigate(1)}>
            Forward
          </button>
          <span data-testid="address">{`${location.pathname}${location.search}`}</span>
        </>
      );
    }

    stubBackend({ '/api/v1/docker/containers': MIXED_LIST });
    render(
      <MemoryRouter initialEntries={['/workloads']}>
        <Routes>
          <Route path="workloads" element={<HistoryHarness />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expectRow('grafana'));

    const before = vi.mocked(fetch).mock.calls.filter(
      ([input]) => urlOf(input).includes('/docker/containers'),
    ).length;

    await user.click(screen.getByRole('button', { name: 'To app' }));
    await waitFor(() =>
      expect(screen.getByTestId('address')).toHaveTextContent('/workloads?group=app'),
    );
    expectRow('app-one');
    expectNoRow('grafana');

    const afterFilter = vi.mocked(fetch).mock.calls.filter(
      ([input]) => urlOf(input).includes('/docker/containers'),
    ).length;
    expect(afterFilter).toBe(before);

    await user.click(screen.getByRole('button', { name: 'Back' }));
    await waitFor(() => expect(screen.getByTestId('address').textContent).toBe('/workloads'));
    expectRow('grafana');

    const afterBack = vi.mocked(fetch).mock.calls.filter(
      ([input]) => urlOf(input).includes('/docker/containers'),
    ).length;
    expect(afterBack).toBe(before);

    await user.click(screen.getByRole('button', { name: 'Forward' }));
    await waitFor(() =>
      expect(screen.getByTestId('address')).toHaveTextContent('/workloads?group=app'),
    );
    expectNoRow('grafana');
  });
});

describe('breadcrumb navigation from container detail', () => {
  it('follows the project pathline link to the filtered list', async () => {
    const user = userEvent.setup();
    const grafana = {
      ...(BACKEND['/api/v1/docker/containers/obj_ct'] as DockerContainer),
      object_id: 'obj_grafana',
      name: 'grafana',
      labels: {
        ...(BACKEND['/api/v1/docker/containers/obj_ct'] as DockerContainer).labels,
        [LABEL_PROJECT]: 'monitoring',
        [LABEL_SERVICE]: 'grafana',
      },
    };
    const listWithGrafana: DockerContainerList = {
      ...MIXED_LIST,
      containers: MIXED_LIST.containers.map((c) =>
        c.name === 'grafana' ? grafana : c,
      ),
    };

    stubBackend({
      '/api/v1/docker/containers': listWithGrafana,
      '/api/v1/docker/containers/obj_grafana': grafana,
    });

    render(
      <MemoryRouter initialEntries={['/workloads/obj_grafana']}>
        <Routes>
          <Route path="workloads" element={<WorkloadList />} />
          <Route path="workloads/:objectId" element={<WorkloadDetail />} />
        </Routes>
        <AddressProbe />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByText(/Compose labels place this container/i)).toBeInTheDocument(),
    );

    await user.click(screen.getByRole('link', { name: 'monitoring' }));
    await waitFor(() =>
      expect(screen.getByTestId('address')).toHaveTextContent('/workloads?group=monitoring'),
    );
    expectRow('grafana');
    expectNoRow('app-one');
  });
});

describe('degraded evidence', () => {
  it('still surfaces the sweep caveat while filtering by group', async () => {
    const baseSweep = (BACKEND['/api/v1/docker/containers'] as DockerContainerList).last_sweep as Sweep;
    const partial: DockerContainerList = {
      ...MIXED_LIST,
      last_sweep: {
        ...baseSweep,
        status: 'degraded',
        issues: [{ source: 'docker.containers', code: 'partial_read', message: 'some containers unreadable', detail: {} }],
      },
    };
    renderAt('/workloads?group=monitoring', { '/api/v1/docker/containers': partial });

    await waitFor(() => expect(screen.getByText(/Observation was not complete/i)).toBeInTheDocument());
    expect(screen.getByText(/1 issue/i)).toBeInTheDocument();
    expectRow('grafana');
  });
});
