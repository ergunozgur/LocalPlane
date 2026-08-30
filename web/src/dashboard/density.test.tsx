/**
 * Container-aware density, asserted on what is actually rendered.
 *
 * The requirement is not that the widget resizes — it is that it presents *different
 * information* as its own container narrows, while never reducing away the safety state. So
 * these tests drive a fake `ResizeObserver` and check which facts survive each mode.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { DeviceOverviewWidget } from './widgets/DeviceOverviewWidget';
import { ViewerProvider } from '@/identity/viewer';
import { stubBackend } from '@/test/backend';

/** A `ResizeObserver` that reports one width to every observer. */
function installResizeObserver(width: number): void {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe(_target: Element): void {
        this.callback(
          [{ contentRect: { width } } as unknown as ResizeObserverEntry],
          this,
        );
      }
      unobserve(): void {}
      disconnect(): void {}
    },
  );
}

/** The lead plate's head, so assertions about the meta line do not also match the plane. */
function plateHead(): HTMLElement {
  const heading = screen.getByRole('heading', { name: 'demo-host' });
  const head = heading.parentElement;
  if (!head) throw new Error('plate head not found');
  return head;
}

function renderAt(width: number): void {
  installResizeObserver(width);
  stubBackend();
  render(
    <ViewerProvider>
      <MemoryRouter>
        <DeviceOverviewWidget />
      </MemoryRouter>
    </ViewerProvider>,
  );
}

beforeEach(() => vi.unstubAllGlobals());
afterEach(() => vi.unstubAllGlobals());

describe('expanded (wide container)', () => {
  it('draws the full four-column relationship plane', async () => {
    renderAt(1400);
    await screen.findAllByText('demo-host');
    for (const column of ['Upstream', 'Path', 'Host', 'Attached']) {
      expect(screen.getByText(column)).toBeInTheDocument();
    }
  });

  it('carries the kernel in the meta line when there is room for it', async () => {
    renderAt(1400);
    await screen.findAllByText('demo-host');
    expect(plateHead().textContent).toContain('6.8.0-1060-raspi');
  });

  it('shows the estate counts on the host node', async () => {
    renderAt(1400);
    expect(await screen.findByText(/1 interfaces/)).toBeInTheDocument();
  });
});

describe('compact (medium container)', () => {
  it('narrows the plane to Host and Attached rather than shrinking four columns', async () => {
    renderAt(700);
    await screen.findAllByText('demo-host');
    expect(screen.getByText('Host')).toBeInTheDocument();
    expect(screen.getByText('Attached')).toBeInTheDocument();
    expect(screen.queryByText('Upstream')).not.toBeInTheDocument();
  });

  it('keeps the host node and its interface list at this width', async () => {
    // The column count follows the measured width, not the density mode, because a
    // truncated identifier is worth less than a column.
    renderAt(700);
    await screen.findAllByText('demo-host');
    // Named twice by design: once in the host node's list, once as the subnet's route in
    // Attached. Both are true statements about the same interface.
    expect(screen.getByText('reachable through')).toBeInTheDocument();
    expect(screen.getAllByText('enx020000000012').length).toBeGreaterThan(0);
  });

  it('drops the kernel from the meta line, keeping OS and architecture', async () => {
    renderAt(700);
    await screen.findAllByText('demo-host');
    const head = plateHead().textContent ?? '';
    expect(head).toContain('Ubuntu 24.04.4 LTS');
    expect(head).toContain('aarch64');
    expect(head).not.toContain('6.8.0-1060-raspi');
  });
});

describe('minimal (narrow container)', () => {
  it('drops the plane entirely rather than rendering a one-column diagram', async () => {
    renderAt(420);
    await screen.findAllByText('demo-host');
    for (const column of ['Upstream', 'Path', 'Host', 'Attached']) {
      expect(screen.queryByText(column)).not.toBeInTheDocument();
    }
  });

  it('drops the OS line and the estate counts', async () => {
    renderAt(420);
    await screen.findAllByText('demo-host');
    expect(screen.queryByText(/Ubuntu 24.04.4 LTS · aarch64/)).not.toBeInTheDocument();
    expect(screen.queryByText(/interfaces$/)).not.toBeInTheDocument();
  });
});

describe('what density must never remove', () => {
  it.each([1400, 700, 420])('keeps safety state as text at %ipx', async (width) => {
    renderAt(width);
    await screen.findAllByText('demo-host');

    // Agent reachability and the management-path verdict, in every mode, as words.
    expect(screen.getAllByText('agent reachable').length).toBeGreaterThan(0);
    expect(screen.getAllByText('unresolved').length).toBeGreaterThan(0);
    expect(
      screen.getByText(/cannot tell which interface carries this connection/i),
    ).toBeInTheDocument();
  });

  it.each([1400, 700, 420])('keeps missing evidence visible at %ipx', async (width) => {
    renderAt(width);
    await waitFor(() => expect(screen.getByText('session.peer')).toBeInTheDocument());
    expect(screen.getByText('route.observe')).toBeInTheDocument();
  });

  it.each([1400, 700, 420])('never hides safety state behind hover only at %ipx', async (width) => {
    renderAt(width);
    await screen.findAllByText('demo-host');
    // The path explanation is body text, not a title attribute on something.
    const note = screen.getByText(/cannot tell which interface carries this connection/i);
    expect(note.closest('details')).toBeNull();
  });
});
