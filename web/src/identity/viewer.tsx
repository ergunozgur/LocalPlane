/** The current authenticated request has no named person, role, or user id. */
import { createContext, useContext, useMemo, type ReactNode } from 'react';

export interface Viewer {
  /**
   * The viewer's stable id, or `null` when the build has no user model.
   *
   * `null` means "this build cannot attribute actions to a person", which is exactly what
   * the backend says of itself: new authenticated confirmations use
   * `authenticated_request`, while historical `unauthenticated_request` rows remain possible.
   * Neither value identifies a person.
   */
  readonly id: string | null;
  readonly displayName: string;
  /** How an action taken now would be attributed in the record. */
  readonly attribution: 'authenticated_request';
}

const AUTHENTICATED_VIEWER: Viewer = {
  id: null,
  displayName: 'Authenticated operator',
  attribution: 'authenticated_request',
};

const ViewerContext = createContext<Viewer>(AUTHENTICATED_VIEWER);

export function ViewerProvider({
  children,
  viewer = AUTHENTICATED_VIEWER,
}: {
  children: ReactNode;
  viewer?: Viewer;
}): JSX.Element {
  const value = useMemo(() => viewer, [viewer]);
  return <ViewerContext.Provider value={value}>{children}</ViewerContext.Provider>;
}

export function useViewer(): Viewer {
  return useContext(ViewerContext);
}
