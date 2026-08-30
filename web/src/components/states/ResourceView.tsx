/**
 * Render a resource through its states.
 *
 * Every surface routes its data through this so that loading, failure and success are
 * handled the same way everywhere, and so that no screen can accidentally render `undefined`
 * as though it were a value.
 *
 * A refresh keeps the previous body on screen with a quiet marker rather than replacing it
 * with a spinner: an operator watching a value should not lose it because the page asked
 * again.
 */
import type { ReactNode } from 'react';
import type { Resource } from '@/hooks/useResource';
import { Failed, Loading } from './SurfaceState';
import styles from './ResourceView.module.css';

export function ResourceView<T>({
  resource,
  children,
  what,
  loadingLabel,
  onRetry,
}: {
  resource: Resource<T>;
  children: (data: T, meta: { refreshing: boolean; fetchedAt: Date }) => ReactNode;
  what?: string;
  loadingLabel?: string;
  onRetry?: () => void;
}): JSX.Element {
  if (resource.status === 'loading') {
    return <Loading {...(loadingLabel ? { label: loadingLabel } : {})} />;
  }
  if (resource.status === 'failed') {
    return (
      <Failed
        error={resource.error}
        onRetry={onRetry ?? resource.retry}
        {...(what ? { what } : {})}
      />
    );
  }
  return (
    <div className={resource.refreshing ? styles.refreshing : undefined}>
      {children(resource.data, { refreshing: resource.refreshing, fetchedAt: resource.fetchedAt })}
    </div>
  );
}
