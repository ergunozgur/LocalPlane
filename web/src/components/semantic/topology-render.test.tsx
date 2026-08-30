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
import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
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

function draw(): void {
  render(
    <MemoryRouter>
      <Topology nodes={NODES} columns={4} />
    </MemoryRouter>,
  );
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

describe('the host’s ports', () => {
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
