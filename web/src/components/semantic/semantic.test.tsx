/**
 * What actually reaches the screen.
 *
 * The vocabulary tests prove the mapping; these prove the rendering, which is where a
 * colour-only status or a `??  '—'` at a call site would undo it.
 */
import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { StatusPill, StatusMark } from './StatusPill';
import { UnknownValue, Value } from './UnknownValue';
import { LifecyclePanel } from './LifecyclePanel';
import * as v from '@/domain/vocabulary';
import type { Capability, SystemdUnit } from '@/api/types';

describe('StatusPill', () => {
  it('renders a word, not only a colour', () => {
    render(<StatusPill semantic={v.protection('unknown')} />);
    expect(screen.getByText('unknown')).toBeInTheDocument();
  });

  it('gives unknown and clear different glyphs, so greyscale still distinguishes them', () => {
    const { container: unknown } = render(<StatusPill semantic={v.protection('unknown')} />);
    const { container: clear } = render(<StatusPill semantic={v.protection('clear')} />);
    const glyphOf = (root: HTMLElement): string =>
      root.querySelector('[aria-hidden="true"]')?.textContent ?? '';
    expect(glyphOf(unknown)).toBe('?');
    expect(glyphOf(clear)).toBe('✓');
    expect(glyphOf(unknown)).not.toBe(glyphOf(clear));
  });

  it('speaks the tone to assistive technology rather than relying on colour', () => {
    render(<StatusPill semantic={v.mutationOutcome('write_unknown')} />);
    expect(screen.getByText(/needs attention:/i)).toBeInTheDocument();
  });

  it('shows the backend token beside the word so it can be searched for', () => {
    render(<StatusPill semantic={v.protection('unknown')} token="transport_peer_local" />);
    expect(screen.getByText('transport_peer_local')).toBeInTheDocument();
  });

  it('carries the explanation as the accessible title', () => {
    const { container } = render(<StatusPill semantic={v.protection('clear')} />);
    expect(container.firstElementChild?.getAttribute('title')).toMatch(/not a word for/i);
  });
});

describe('StatusMark', () => {
  it('names the state for screen readers even when only a glyph is drawn', () => {
    render(<StatusMark semantic={v.health('failed')} />);
    expect(screen.getByRole('img', { name: /problem: failed/i })).toBeInTheDocument();
  });
});

describe('absent values', () => {
  it('renders null as an explicit unknown rather than a blank', () => {
    render(<Value value={null} />);
    expect(screen.getByRole('img', { name: 'not known' })).toBeInTheDocument();
  });

  it('renders zero as zero, not as unknown', () => {
    render(<Value value={0} />);
    expect(screen.getByText('0')).toBeInTheDocument();
    expect(screen.queryByRole('img', { name: /not known/ })).not.toBeInTheDocument();
  });

  it('carries the backend’s reason for the absence', () => {
    render(<UnknownValue reason="the kernel refuses this read while the link is down" />);
    expect(
      screen.getByRole('img', { name: /kernel refuses this read/i }),
    ).toBeInTheDocument();
  });

  it('never renders a null boolean as "no"', () => {
    const { container } = render(<Value value={null} reason="not reported" />);
    expect(container.textContent).not.toMatch(/\bno\b/i);
    expect(container.textContent).not.toMatch(/\bfalse\b/i);
  });
});

/** A real unit from this host, with the properties the panel reads. */
function unit(overrides: Partial<SystemdUnit> = {}): SystemdUnit {
  return {
    object_id: 'obj_b230f43d7107c57429c228e366e618a9',
    kind: 'systemd.unit',
    canonical_id: 'ModemManager.service',
    names: ['ModemManager.service'],
    description: 'Modem Manager',
    unit_type: 'service',
    identity: { basis: 'unit_id', value: 'ModemManager.service', confidence: 'high' },
    management: { state: 'observed', reason: 'observe_only' },
    health: { state: 'healthy', reason: 'active_running' },
    observation: null,
    observed_in_latest_sweep: true,
    load_state: 'loaded',
    active_state: 'active',
    sub_state: 'running',
    unit_file_state: 'enabled',
    unit_file_preset: 'enabled',
    can_start: true,
    can_stop: true,
    can_reload: false,
    refuse_manual_start: false,
    refuse_manual_stop: false,
    need_daemon_reload: false,
    fragment_path: '/usr/lib/systemd/system/ModemManager.service',
    source_path: null,
    drop_in_paths: null,
    transient: false,
    template: null,
    current_job: null,
    invocation_id: 'abc',
    timestamps: {},
    relationships: [],
    first_seen_at: '2026-08-27T21:46:55Z',
    last_seen_at: '2026-08-27T21:47:11Z',
    ...overrides,
  };
}

/** The capability exactly as this host's agent reports it: available, and mutating. */
const LIFECYCLE_CAPABILITY: Capability = {
  capability: 'systemd.service.lifecycle',
  version: 1,
  status: 'available',
  mutating: true,
  summary: 'Declare whether the system manager exposes the closed service job contract.',
  reason: null,
  detail: {},
  discovered_at: '2026-08-27T21:46:40Z',
};

describe('systemd lifecycle presentation', () => {
  it('offers no start, stop or restart control even when the capability reads available', () => {
    render(<LifecyclePanel unit={unit()} capability={LIFECYCLE_CAPABILITY} />);

    // Not enabled, and not disabled either: a disabled button still claims the feature.
    for (const verb of [/^start$/i, /^stop$/i, /^restart$/i]) {
      expect(screen.queryByRole('button', { name: verb })).not.toBeInTheDocument();
    }
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });

  it('states plainly that no lifecycle control is offered here', () => {
    render(<LifecyclePanel unit={unit()} capability={LIFECYCLE_CAPABILITY} />);
    expect(screen.getByText(/no lifecycle control/i)).toBeInTheDocument();
  });

  it('shows the capability without letting it read as permission', () => {
    render(<LifecyclePanel unit={unit()} capability={LIFECYCLE_CAPABILITY} />);
    expect(screen.getByText('systemd.service.lifecycle')).toBeInTheDocument();
    expect(screen.getByText(/not permission to act/i)).toBeInTheDocument();
  });

  it("reports systemd's own can_start as a property of the unit, not of LocalPlane", () => {
    render(<LifecyclePanel unit={unit({ can_start: true })} capability={LIFECYCLE_CAPABILITY} />);
    const row = screen.getByText('Can start').closest('div');
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText('yes')).toBeInTheDocument();
    expect(screen.getByText(/not what LocalPlane can ask it to do/i)).toBeInTheDocument();
  });

  it('renders an unreported systemd property as unknown rather than as no', () => {
    render(<LifecyclePanel unit={unit({ can_start: null })} capability={LIFECYCLE_CAPABILITY} />);
    const row = screen.getByText('Can start').closest('div') as HTMLElement;
    expect(within(row).queryByText('no')).not.toBeInTheDocument();
    expect(
      within(row).getByRole('img', { name: /did not report this property/i }),
    ).toBeInTheDocument();
  });

  it('reports execution as not assessed when no plan has been published', () => {
    render(<LifecyclePanel unit={unit()} capability={LIFECYCLE_CAPABILITY} />);
    expect(screen.getByText(/not assessed/i)).toBeInTheDocument();
  });
});
