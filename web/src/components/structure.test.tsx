/**
 * The pieces of LocalPlane's product language, tested as behaviour.
 *
 * These are the structures that carry meaning rather than decoration — the comparator's
 * three relations, the symbol language's three shapes, an assembled tab strip, a chart shell
 * that refuses to draw, and a meter that will not render "not read" as zero. A visual
 * regression in any of them is a semantic regression.
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { Comparator } from './semantic/Comparator';
import { ChartShell, Meter } from './semantic/Metric';
import { Timeline } from './semantic/Timeline';
import {
  ConfidenceLadder,
  HealthMark,
  ManagementChip,
  ReconciliationChip,
} from './semantic/SemanticGlyph';
import { ObjectTabs, type ObjectTab } from './object/ObjectTabs';
import { FilterChips } from './primitives/FilterChips';
import { DataTable } from './primitives/DataTable';
import * as v from '@/domain/vocabulary';

function inRouter(node: JSX.Element): JSX.Element {
  return <MemoryRouter>{node}</MemoryRouter>;
}

describe('the comparator', () => {
  it('renders the relation as its own cell, with an operator per relation', () => {
    render(
      <Comparator
        source="v3"
        rows={[
          { field: 'mtu', observed: '1500', intended: '1500', relation: 'eq' },
          { field: 'state', observed: 'down', intended: 'up', relation: 'ne', drift: true },
        ]}
      />,
    );
    expect(screen.getByText('=')).toBeInTheDocument();
    expect(screen.getByText('≠')).toBeInTheDocument();
    expect(screen.getByText('v3')).toBeInTheDocument();
  });

  it('draws no operator at all for a comparison nobody made', () => {
    // An empty operator is the point. A dash or a question mark in the same weight as `=`
    // and `≠` reads as a verdict, and "not compared" is not a verdict.
    render(
      <Comparator rows={[{ field: 'mtu', observed: 'not read', intended: '1500', relation: 'na' }]} />,
    );
    const cell = screen.getByLabelText('not compared');
    expect(cell).toHaveTextContent('');
    expect(screen.queryByText('=')).not.toBeInTheDocument();
    expect(screen.queryByText('≠')).not.toBeInTheDocument();
  });
});

describe('the symbol language', () => {
  it('gives each axis its own mark, so two states are never the same glyph', () => {
    // Health is a circle, management a square, reconciliation a mono operator, and the
    // square is deliberately unlike the health dot. jsdom has no stylesheet, so the shapes
    // are asserted through the structures that carry them.
    const { container: health } = render(<HealthMark semantic={v.health('healthy')} />);
    const { container: management } = render(<ManagementChip state="managed" />);
    const { container: reconciliation } = render(<ReconciliationChip state="in_sync" />);

    // The health mark is a bare marked element that names its state — no glyph inside it.
    const mark = health.querySelector('[role="img"]');
    expect(mark).toBeInTheDocument();
    expect(mark?.textContent).toBe('');
    expect(mark?.getAttribute('aria-label')).toMatch(/healthy/);

    // Management carries an empty `<i>` — the square itself.
    expect(management.querySelector('i')?.textContent).toBe('');

    // Reconciliation carries an `<i>` holding the operator.
    expect(within(reconciliation).getByText('=').tagName).toBe('I');
  });

  it('spells each state out in words as well as in shape', () => {
    render(<ManagementChip state="observe_only" />);
    expect(screen.getByText(v.management('observe_only').label)).toBeInTheDocument();
  });

  it('renders an unrecognised reconciliation token as unknown, never as in sync', () => {
    render(<ReconciliationChip state="something_new" />);
    expect(screen.getByText('?')).toBeInTheDocument();
    expect(screen.queryByText('=')).not.toBeInTheDocument();
  });

  it('lights no confidence bars when confidence was never stated', () => {
    const stated = render(<ConfidenceLadder level="confirmed" />).container;
    const unstated = render(<ConfidenceLadder level={null} />).container;
    const classOf = (root: HTMLElement): string => root.firstElementChild?.className ?? '';
    expect(classOf(stated)).not.toBe(classOf(unstated));
  });
});

describe('object tabs', () => {
  const tabs: ObjectTab[] = [
    { id: 'a', label: 'Overview', render: () => 'A' },
    { id: 'b', label: 'Traffic', hidden: true, render: () => 'B' },
    { id: 'c', label: 'Evidence', count: 3, render: () => 'C' },
  ];

  it('assembles a tab away rather than rendering it disabled', () => {
    render(<ObjectTabs tabs={tabs} activeId="a" onSelect={() => {}} />);
    expect(screen.queryByRole('tab', { name: /Traffic/ })).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Evidence/ })).toBeInTheDocument();
  });

  it('moves between tabs with the arrow keys, skipping the assembled-away one', () => {
    const onSelect = vi.fn();
    render(<ObjectTabs tabs={tabs} activeId="a" onSelect={onSelect} />);
    fireEvent.keyDown(screen.getByRole('tab', { name: /Overview/ }), { key: 'ArrowRight' });
    expect(onSelect).toHaveBeenCalledWith('c');
  });

  it('keeps one tab stop for the strip, as a tablist should', () => {
    render(<ObjectTabs tabs={tabs} activeId="a" onSelect={() => {}} />);
    const stops = screen.getAllByRole('tab').filter((tab) => tab.getAttribute('tabindex') === '0');
    expect(stops).toHaveLength(1);
  });
});

describe('meters and chart shells', () => {
  it('will not render an unread proportion as an empty bar', () => {
    // An empty track and a zero-length fill look identical, and they are opposite claims.
    render(<Meter percent={null} label="CPU" />);
    expect(screen.getByLabelText('CPU: not read')).toBeInTheDocument();
    expect(screen.queryByLabelText(/0.0 percent/)).not.toBeInTheDocument();
  });

  it('states a percentage in the accessible name, not only as a width', () => {
    render(<Meter percent={42.5} label="Memory" />);
    expect(screen.getByLabelText('Memory: 42.5 percent')).toBeInTheDocument();
  });

  it('renders a chart frame that says what is missing rather than drawing nothing', () => {
    render(
      <ChartShell
        title="Load"
        series={null}
        absence="Nothing reads this host's load."
        wouldFill="would need: host.metrics.observe"
      />,
    );
    expect(screen.getByText(/Nothing reads this host's load/)).toBeInTheDocument();
    expect(screen.getByText('would need: host.metrics.observe')).toBeInTheDocument();
  });

  it('draws a series once one exists, without the caller changing anything else', () => {
    const { container } = render(
      <ChartShell
        title="Load"
        absence="unused"
        series={[
          {
            id: 'l1',
            label: '1m',
            tone: 'neutral',
            points: [
              { at: 'a', value: 1 },
              { at: 'b', value: 2 },
            ],
          },
        ]}
      />,
    );
    expect(container.querySelector('polyline')).toBeInTheDocument();
    expect(screen.queryByText('unused')).not.toBeInTheDocument();
  });
});

describe('the tick timeline', () => {
  it('gives a change a different marker class from an observation', () => {
    const { container } = render(
      <Timeline
        entries={[
          { id: '1', kind: 'change', at: '10:00', what: 'wrote mtu' },
          { id: '2', kind: 'observe', at: '09:00', what: 'read the link' },
        ]}
      />,
    );
    const items = container.querySelectorAll('li');
    expect(items).toHaveLength(2);
    expect(items[0]?.className).not.toBe(items[1]?.className);
  });
});

describe('filter chips', () => {
  it('shows the available values and their counts without a click', () => {
    render(
      <FilterChips
        legend="Result"
        value=""
        onChange={() => {}}
        options={[
          { value: '', label: 'All', count: 7 },
          { value: 'failed', label: 'failed', count: 2, tone: 'bad' },
        ]}
      />,
    );
    expect(screen.getByRole('button', { name: /All/ })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('2')).toBeInTheDocument();
  });

  it('omits a count rather than guessing one', () => {
    render(
      <FilterChips
        legend="Result"
        value="failed"
        onChange={() => {}}
        options={[
          { value: '', label: 'All' },
          { value: 'failed', label: 'failed', count: 2 },
        ]}
      />,
    );
    const all = screen.getByRole('button', { name: 'All' });
    expect(all).toHaveTextContent(/^All$/);
  });

  it('clears the filter when the pressed chip is pressed again', () => {
    const onChange = vi.fn();
    render(
      <FilterChips
        legend="Result"
        value="failed"
        onChange={onChange}
        options={[
          { value: '', label: 'All' },
          { value: 'failed', label: 'failed' },
        ]}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'failed' }));
    expect(onChange).toHaveBeenCalledWith('');
  });
});

describe('table rows', () => {
  const rows = [
    { id: '1', name: 'eth0', drifted: false },
    { id: '2', name: 'wlan0', drifted: true },
  ];

  it('activates on a click anywhere in the row', () => {
    const activate = vi.fn();
    render(
      inRouter(
        <DataTable
          rows={rows}
          rowKey={(row) => row.id}
          onRowActivate={activate}
          columns={[{ key: 'name', header: 'Name', render: (row) => row.name }]}
        />,
      ),
    );
    fireEvent.click(screen.getByText('wlan0'));
    expect(activate).toHaveBeenCalledWith(rows[1]);
  });

  it('does not activate twice when the click landed on the row’s own link', () => {
    const activate = vi.fn();
    render(
      inRouter(
        <DataTable
          rows={rows}
          rowKey={(row) => row.id}
          onRowActivate={activate}
          columns={[{ key: 'name', header: 'Name', render: (row) => <a href={`/x/${row.id}`}>{row.name}</a> }]}
        />,
      ),
    );
    fireEvent.click(screen.getByRole('link', { name: 'wlan0' }));
    expect(activate).not.toHaveBeenCalled();
  });

  it('marks a drifted row on the row itself, not only in a column', () => {
    const { container } = render(
      inRouter(
        <DataTable
          rows={rows}
          rowKey={(row) => row.id}
          rowTone={(row) => (row.drifted ? 'attention' : undefined)}
          columns={[{ key: 'name', header: 'Name', render: (row) => row.name }]}
        />,
      ),
    );
    const bodyRows = container.querySelectorAll('tbody tr');
    expect(bodyRows[0]?.className).not.toBe(bodyRows[1]?.className);
    expect(bodyRows[1]?.className).not.toBe('');
  });
});
