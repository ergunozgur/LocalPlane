/**
 * ScopeBar tab selection semantics.
 *
 * Ordinary callers rely on NavLink pathname matching. The System list is the one surface that
 * needs an explicit override because the same pathname carries two mutually-exclusive section
 * filters. This test guards both behaviors.
 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { ScopeBar, type ScopeTab } from './ScopeBar';

function Fixture({ tabs }: { tabs: readonly ScopeTab[] }): JSX.Element {
  return (
    <MemoryRouter initialEntries={['/a?x=1']}>
      <Routes>
        <Route path="a" element={<ScopeBar crumbs={[{ label: 'a' }]} tabs={tabs} />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('ScopeBar ordinary NavLink callers', () => {
  it('marks the matching tab current and leaves others unmarked', () => {
    render(
      <Fixture
        tabs={[
          { to: '/a', label: 'A', end: true },
          { to: '/b', label: 'B' },
        ]}
      />,
    );

    expect(screen.getByRole('link', { name: /^A$/ })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: /^B$/ })).not.toHaveAttribute('aria-current');
  });

  it('renders counts without affecting current matching', () => {
    render(
      <Fixture
        tabs={[
          { to: '/a', label: 'A', end: true, count: 7 },
          { to: '/b', label: 'B', count: 3 },
        ]}
      />,
    );

    // Counts are part of the accessible name, so match the label at the start.
    expect(screen.getByRole('link', { name: /^A/ })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByText('7')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
  });
});

describe('ScopeBar explicit current override', () => {
  it('uses current instead of pathname matching', () => {
    render(
      <Fixture
        tabs={[
          { to: '/a?x=1', label: 'A', current: false },
          { to: '/a?x=2', label: 'B', current: true },
        ]}
      />,
    );

    expect(screen.getByRole('link', { name: /^A$/ })).not.toHaveAttribute('aria-current');
    expect(screen.getByRole('link', { name: /^B$/ })).toHaveAttribute('aria-current', 'page');
  });

  it('keeps the tab a real link even when explicitly not current', () => {
    render(
      <Fixture
        tabs={[
          { to: '/a?x=1', label: 'A', current: false },
          { to: '/a?x=2', label: 'B', current: true },
        ]}
      />,
    );

    expect(screen.getByRole('link', { name: /^A$/ })).toHaveAttribute('href', '/a?x=1');
    expect(screen.getByRole('link', { name: /^B$/ })).toHaveAttribute('href', '/a?x=2');
  });
});
