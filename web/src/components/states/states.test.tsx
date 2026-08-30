/**
 * Degraded states, and the difference between kinds of nothing.
 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Degraded, Empty, Failed, NotAssessed } from './SurfaceState';
import { Gaps, RawEvidence } from '@/components/semantic/Evidence';
import { ApiError } from '@/api/client';

describe('Failed', () => {
  it('says the backend is unreachable rather than making a claim about the host', () => {
    render(
      <Failed error={new ApiError({ kind: 'unreachable', message: 'refused', path: '/host' })} />,
    );
    expect(screen.getByText(/backend could not be reached/i)).toBeInTheDocument();
    expect(screen.getByText(/not about the host itself/i)).toBeInTheDocument();
  });

  it('shows the stable code so an operator can search for it', () => {
    render(
      <Failed
        error={
          new ApiError({
            kind: 'backend',
            message: 'no executor',
            path: '/runs/x/apply',
            code: 'execution_not_implemented',
            status: 409,
          })
        }
      />,
    );
    expect(screen.getByText('execution_not_implemented')).toBeInTheDocument();
    expect(screen.getByText(/HTTP 409/)).toBeInTheDocument();
  });

  it('offers a retry only when retrying could plausibly work', () => {
    const { rerender } = render(
      <Failed
        error={new ApiError({ kind: 'unreachable', message: '', path: '/host' })}
        onRetry={() => {}}
      />,
    );
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();

    rerender(
      <Failed
        error={new ApiError({ kind: 'backend', message: '', path: '/x', code: 'preview_stale', status: 409 })}
        onRetry={() => {}}
      />,
    );
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument();
  });

  it('announces itself as an alert', () => {
    render(<Failed error={new ApiError({ kind: 'timeout', message: '', path: '/x' })} />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
  });
});

describe('Empty', () => {
  it('carries the explanation that distinguishes "none" from "nobody looked"', () => {
    render(
      <Empty
        title="No containers"
        explanation="No sweep has recorded containers, so this is not evidence that there are none."
      />,
    );
    expect(screen.getByText(/not evidence that there are none/i)).toBeInTheDocument();
  });
});

describe('NotAssessed', () => {
  it('is distinct from empty and from failed', () => {
    render(<NotAssessed title="Execution has not been assessed" />);
    expect(screen.getByText(/has not been assessed/i)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('Gaps', () => {
  it('shows missing evidence rather than hiding it behind a disclosure', () => {
    render(<Gaps items={['session.peer', 'route.observe']} />);
    expect(screen.getByText('session.peer')).toBeInTheDocument();
    expect(screen.getByText('route.observe')).toBeInTheDocument();
    expect(screen.getByText(/Missing evidence · 2/)).toBeInTheDocument();
  });

  it('renders nothing when there is nothing missing', () => {
    const { container } = render(<Gaps items={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('states the remainder honestly when the list is capped', () => {
    render(<Gaps items={Array.from({ length: 30 }, (_, i) => `gap_${i}`)} limit={5} />);
    expect(screen.getByText('and 25 more')).toBeInTheDocument();
    expect(screen.getByText(/· 30/)).toBeInTheDocument();
  });
});

describe('Degraded', () => {
  it('reports a partial observation as a caveat on real data', () => {
    render(
      <Degraded title="Observation was not complete">
        What is shown is what could be read.
      </Degraded>,
    );
    expect(screen.getByText(/what could be read/i)).toBeInTheDocument();
  });
});

describe('RawEvidence', () => {
  it('bounds a large evidence object rather than filling the page', () => {
    render(<RawEvidence value={{ blob: 'x'.repeat(5000) }} maxChars={200} />);
    expect(screen.getByText(/truncated at 200 characters/i)).toBeInTheDocument();
  });

  it('does not throw on a value that cannot be serialised', () => {
    const circular: Record<string, unknown> = {};
    circular['self'] = circular;
    expect(() => render(<RawEvidence value={circular} />)).not.toThrow();
  });
});
