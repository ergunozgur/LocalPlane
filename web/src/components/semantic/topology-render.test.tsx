/**
 * The topology as an interaction, not only as a derivation.
 *
 * `topology.test.ts` proves every node is traceable to published evidence. This proves the
 * behaviour built around those nodes: selecting one explains it, the relationship is
 * written out in words so nothing is lost when a label will not fit, and the series slot
 * stays empty because there is no series.
 *
 * jsdom reports every box as zero-sized, so no edge label can fit — which is convenient: it
 * puts the component permanently in the state where labels are dropped, and lets the dropped
 * count be asserted.
 */
import { describe, expect, it, vi, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import { fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { Topology } from './Topology';
import type { TopologyNode } from './topology-model';

const NODES: TopologyNode[] = [
  {
    id: 'path',
    column: 1,
    name: 'eth0',
    kind: 'management path',
    mark: { tone: 'good', label: 'confirmed', description: 'Proven.' },
    evidence: 'This request arrives over eth0.',
    sources: ['management_path.evidence'],
  },
  {
    id: 'host',
    column: 2,
    name: 'demo-host',
    kind: 'this host',
    mark: { tone: 'good', label: 'agent reachable', description: 'The agent answered.' },
    facts: ['15 interfaces'],
    ports: [
      { name: 'eth0', detail: '192.0.2.24/24', tone: 'good', objectId: 'obj_eth0' },
      { name: 'wlan0', detail: '198.51.100.5/24', tone: 'warn', objectId: 'obj_wlan0', drifted: true },
    ],
    portsLabel: 'reachable through',
    edges: [{ from: 'path', kind: 'active', label: '192.0.2.1', why: 'The connection terminates on eth0.' }],
  },
  {
    id: 'net:1',
    column: 3,
    name: 'monitoring_default',
    kind: 'container net',
    mark: { tone: 'neutral', label: 'observed', description: 'Read.' },
    to: '/network/obj_br',
    drifted: true,
    evidence: 'Docker attributes this network to br-dd9c.',
    sources: ['docker_ipam_gateway_on_link'],
    edges: [{ from: 'host', kind: 'drift', label: '172.19.0.0/16', why: 'br-dd9c carries this network’s gateway.' }],
  },
];

afterEach(() => {
  vi.restoreAllMocks();
});

function draw(): void {
  render(
    <MemoryRouter>
      <Topology nodes={NODES} columns={4} />
    </MemoryRouter>,
  );
}

function withGeometry(): void {
  const origin: DOMRect = {
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    right: 1200,
    bottom: 800,
    width: 1200,
    height: 800,
    toJSON: () => origin,
  };
  const nodeBoxes: Record<string, DOMRect> = {
    path: {
      x: 250, y: 100, top: 100, left: 250, right: 450, bottom: 180, width: 200, height: 80,
      toJSON: () => nodeBoxes.path,
    },
    host: {
      x: 650, y: 100, top: 100, left: 650, right: 850, bottom: 180, width: 200, height: 80,
      toJSON: () => nodeBoxes.host,
    },
    'net:1': {
      x: 1050, y: 100, top: 100, left: 1050, right: 1250, bottom: 180, width: 200, height: 80,
      toJSON: () => nodeBoxes['net:1'],
    },
  };
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
    const id = this.getAttribute('data-node');
    return id && id in nodeBoxes ? nodeBoxes[id]! : origin;
  });
}

describe('selection', () => {
  it('starts with nothing selected and says what selection is for', () => {
    draw();
    expect(screen.getByText(/Select anything in the plane/)).toBeInTheDocument();
    for (const node of screen.getAllByRole('button')) {
      expect(node).toHaveAttribute('aria-pressed', 'false');
    }
  });

  it('explains a node in the evidence strip rather than navigating away from it', () => {
    draw();
    fireEvent.click(screen.getByRole('button', { name: /monitoring_default/ }));

    expect(screen.getByText('Docker attributes this network to br-dd9c.')).toBeInTheDocument();
    expect(screen.getByText('docker_ipam_gateway_on_link')).toBeInTheDocument();
    // The strip carries the link; the node itself does not navigate.
    expect(screen.getByRole('link', { name: /Open/ })).toHaveAttribute('href', '/network/obj_br');
  });

  it('writes every relationship touching the selection out in full', () => {
    draw();
    fireEvent.click(screen.getByRole('button', { name: /monitoring_default/ }));
    expect(screen.getByText('br-dd9c carries this network’s gateway.')).toBeInTheDocument();
    expect(screen.queryByText('The connection terminates on eth0.')).not.toBeInTheDocument();
  });

  it('deselects when the same node is pressed again', () => {
    draw();
    const node = screen.getByRole('button', { name: /monitoring_default/ });
    fireEvent.click(node);
    expect(node).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(node);
    expect(node).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByText(/Select anything in the plane/)).toBeInTheDocument();
  });
});

describe('edge labels', () => {
  it('states how many labels did not fit rather than losing them silently', () => {
    draw();
    // Both labels are dropped at zero width, and the strip says so.
    expect(screen.getByText(/edge labels? did not fit/)).toBeInTheDocument();
  });
});

describe("the host's ports", () => {
  it('renders each attachment as its own link out of the diagram', () => {
    draw();
    expect(screen.getByRole('link', { name: /eth0/ })).toHaveAttribute('href', '/network/obj_eth0');
    expect(screen.getByRole('link', { name: /wlan0/ })).toHaveAttribute('href', '/network/obj_wlan0');
  });

  it('marks a drifted attachment with the reconciliation operator', () => {
    draw();
    const row = screen.getByRole('link', { name: /wlan0/ });
    expect(within(row).getByTitle('drifted from its intent')).toHaveTextContent('≠');
  });

  it('draws nothing in the series slot, because there is no series', () => {
    const { container } = render(
      <MemoryRouter>
        <Topology nodes={NODES} columns={4} />
      </MemoryRouter>,
    );
    // No animated flow overlay, and the reserved slot holds no marks of any kind.
    expect(container.querySelector('[class*="flow"]')).toBeNull();
    const slot = container.querySelector('[title^="No traffic history"]');
    expect(slot).toBeInTheDocument();
    expect(slot?.textContent).toBe('');
  });
});

describe('selection survival', () => {
  it('clears the selection when a column resize hides the selected node', async () => {
    const { container, rerender } = render(
      <MemoryRouter>
        <Topology nodes={NODES} columns={4} />
      </MemoryRouter>,
    );
    const path = screen.getByRole('button', { name: /eth0/ });
    fireEvent.click(path);
    expect(path).toHaveAttribute('aria-pressed', 'true');
    expect(container.querySelector('[class*="dimmed"]')).toBeInTheDocument();
    expect(container.querySelector('[class*="nodeDimmed"]')).toBeInTheDocument();

    // Narrowing to two columns hides column 1 (the path node).
    rerender(
      <MemoryRouter>
        <Topology nodes={NODES} columns={2} />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText(/Select anything in the plane/)).toBeInTheDocument());
    expect(container.querySelector('[class*="dimmed"]')).toBeNull();
    expect(container.querySelector('[class*="nodeDimmed"]')).toBeNull();
    expect(screen.getAllByRole('button').every((button) => button.getAttribute('aria-pressed') === 'false')).toBe(true);

    // A remaining node can be selected again.
    fireEvent.click(screen.getByRole('button', { name: /demo-host/ }));
    expect(screen.getByRole('button', { name: /demo-host/ })).toHaveAttribute('aria-pressed', 'true');
  });

  it('clears the selection when the selected node is removed from the data', async () => {
    const { container, rerender } = render(
      <MemoryRouter>
        <Topology nodes={NODES} columns={4} />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole('button', { name: /demo-host/ }));
    expect(screen.getByRole('button', { name: /demo-host/ })).toHaveAttribute('aria-pressed', 'true');
    expect(container.querySelector('[class*="nodeDimmed"]')).toBeInTheDocument();

    rerender(
      <MemoryRouter>
        <Topology nodes={NODES.filter((node) => node.id !== 'host')} columns={4} />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText(/Select anything in the plane/)).toBeInTheDocument());
    expect(container.querySelector('[class*="dimmed"]')).toBeNull();
    expect(container.querySelector('[class*="nodeDimmed"]')).toBeNull();
    expect(screen.queryByText('15 interfaces')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /monitoring_default/ }));
    expect(screen.getByRole('button', { name: /monitoring_default/ })).toHaveAttribute('aria-pressed', 'true');
  });

  it('does not resurrect a stored selection when a hidden node reappears', async () => {
    const { rerender } = render(
      <MemoryRouter>
        <Topology nodes={NODES} columns={4} />
      </MemoryRouter>,
    );
    const path = screen.getByRole('button', { name: /eth0/ });
    fireEvent.click(path);
    expect(path).toHaveAttribute('aria-pressed', 'true');

    rerender(
      <MemoryRouter>
        <Topology nodes={NODES} columns={2} />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText(/Select anything in the plane/)).toBeInTheDocument());

    // Widening again must not revive the old selection.
    rerender(
      <MemoryRouter>
        <Topology nodes={NODES} columns={4} />
      </MemoryRouter>,
    );
    expect(screen.getByRole('button', { name: /eth0/ })).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByText(/Select anything in the plane/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /monitoring_default/ }));
    expect(screen.getByRole('button', { name: /monitoring_default/ })).toHaveAttribute('aria-pressed', 'true');
  });

  it('drops relationship rows when the selected node is hidden', async () => {
    const { rerender } = render(
      <MemoryRouter>
        <Topology nodes={NODES} columns={4} />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole('button', { name: /demo-host/ }));
    expect(screen.getByText('The connection terminates on eth0.')).toBeInTheDocument();
    expect(screen.getByText('br-dd9c carries this network’s gateway.')).toBeInTheDocument();

    // Hide the host (and path) by showing only the last column.
    rerender(
      <MemoryRouter>
        <Topology nodes={NODES} columns={1} />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.queryByText('The connection terminates on eth0.')).not.toBeInTheDocument());
    expect(screen.queryByText('br-dd9c carries this network’s gateway.')).not.toBeInTheDocument();
  });
});

describe('selection appearance', () => {
  it('marks the selected node and dims the others', () => {
    withGeometry();
    const { container } = render(
      <MemoryRouter>
        <Topology nodes={NODES} columns={4} />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole('button', { name: /demo-host/ }));

    const selected = screen.getByRole('button', { name: /demo-host/ });
    expect(selected.className).toContain('selected');
    expect(container.querySelectorAll('[class*="nodeDimmed"]')).toHaveLength(2);
    for (const dimmed of container.querySelectorAll('[class*="nodeDimmed"]')) {
      expect(dimmed.contains(selected)).toBe(false);
    }
  });

  it('lights edges touching the selection and dims the rest', () => {
    withGeometry();
    const { container } = render(
      <MemoryRouter>
        <Topology nodes={NODES} columns={4} />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole('button', { name: /demo-host/ }));
    let paths = container.querySelectorAll('svg path');
    expect(paths).toHaveLength(2);
    const hasClass = (p: Element, token: string) => p.getAttribute('class')?.includes(token) ?? false;
    expect([...paths].filter((p) => hasClass(p, 'lit'))).toHaveLength(2);
    expect([...paths].filter((p) => hasClass(p, 'dimmed'))).toHaveLength(0);

    fireEvent.click(screen.getByRole('button', { name: /eth0/ }));
    paths = container.querySelectorAll('svg path');
    expect([...paths].filter((p) => hasClass(p, 'lit'))).toHaveLength(1);
    expect([...paths].filter((p) => hasClass(p, 'dimmed'))).toHaveLength(1);
  });
});

describe('nonzero geometry', () => {
  it('places SVG labels and backgrounds when nodes have real size', () => {
    withGeometry();
    const { container } = render(
      <MemoryRouter>
        <Topology nodes={NODES} columns={4} />
      </MemoryRouter>,
    );
    const texts = container.querySelectorAll('svg text');
    expect(texts).toHaveLength(2);
    expect([...texts].map((text) => text.textContent)).toEqual(
      expect.arrayContaining(['192.0.2.1', '172.19.0.0/16']),
    );
    const backgrounds = container.querySelectorAll('svg rect');
    expect(backgrounds).toHaveLength(2);
    for (const rect of backgrounds) {
      expect(rect).toHaveAttribute('width', expect.stringMatching(/^[1-9]/));
      expect(rect).toHaveAttribute('height', '13');
    }
  });

  it('places labels at positive coordinates inside the measured plane', () => {
    withGeometry();
    const { container } = render(
      <MemoryRouter>
        <Topology nodes={NODES} columns={4} />
      </MemoryRouter>,
    );
    for (const text of container.querySelectorAll('svg text')) {
      const x = Number(text.getAttribute('x'));
      const y = Number(text.getAttribute('y'));
      expect(x).toBeGreaterThan(0);
      expect(y).toBeGreaterThan(0);
    }
    for (const rect of container.querySelectorAll('svg rect')) {
      const x = Number(rect.getAttribute('x'));
      const y = Number(rect.getAttribute('y'));
      expect(x).toBeGreaterThan(0);
      expect(y).toBeGreaterThan(0);
    }
  });
});
