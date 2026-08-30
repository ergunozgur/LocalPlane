/**
 * Who is looking.
 *
 * This build has no authentication, no users and no roles, and this file does not pretend
 * otherwise. It exists so that the component tree asks *a viewer* for its identity instead
 * of assuming there is only ever one anonymous operator — the assumption that is expensive
 * to remove later, and cheap to avoid now.
 *
 * What is deliberately **not** here: no permission checks, no role model, no LDAP, no
 * session, no token, and no client-side authorization of any kind. There is no backend
 * contract for any of it, and a permission invented in the browser would be a lie with a
 * padlock drawn on it. When a real model arrives, it arrives as values on this type and as
 * a provider that fetches them.
 *
 * The rule that survives that change: **frontend visibility is never authorization.** The
 * backend refuses what must be refused whether or not a control was drawn.
 */
import { createContext, useContext, useMemo, type ReactNode } from 'react';

export interface Viewer {
  /**
   * The viewer's stable id, or `null` when the build has no user model.
   *
   * `null` means "this build cannot attribute actions to a person", which is exactly what
   * the backend says of itself: `RunConfirmation.source` has the single value
   * `unauthenticated_request`, and the schema notes that recording a user would be a fiction.
   */
  readonly id: string | null;
  readonly displayName: string;
  /** How an action taken now would be attributed in the record. */
  readonly attribution: 'unauthenticated_request';
}

const ANONYMOUS: Viewer = {
  id: null,
  displayName: 'Operator',
  attribution: 'unauthenticated_request',
};

const ViewerContext = createContext<Viewer>(ANONYMOUS);

export function ViewerProvider({
  children,
  viewer = ANONYMOUS,
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
