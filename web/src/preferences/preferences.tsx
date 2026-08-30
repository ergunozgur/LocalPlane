/**
 * Operator preferences — how this browser presents LocalPlane.
 *
 * Kept deliberately apart from domain state. Nothing here is a fact about the host, nothing
 * here is consulted by any safety or domain rendering, and nothing here is authority for
 * anything. It is the appearance of the console and the shape of the dashboard, and that is
 * all it will ever be.
 *
 * The storage key already carries a scope segment (`localplane.prefs.<scope>`). Today the
 * scope is `local`, because this build has no user model. When one arrives the scope becomes
 * the user's id and per-user preferences follow without a migration of concept — see
 * `identity/viewer.ts`.
 */
import {
  createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode,
} from 'react';

export const THEMES = [
  { id: 'localplane', name: 'LocalPlane', scheme: 'dark' },
  { id: 'graphite', name: 'Graphite', scheme: 'light' },
] as const;

export type ThemeId = (typeof THEMES)[number]['id'];

const DEFAULT_THEME: ThemeId = 'localplane';

function isTheme(value: unknown): value is ThemeId {
  return typeof value === 'string' && THEMES.some((t) => t.id === value);
}

export interface Preferences {
  theme: ThemeId;
}

export interface PreferencesApi extends Preferences {
  setTheme: (theme: ThemeId) => void;
}

const PreferencesContext = createContext<PreferencesApi | null>(null);

function storageKey(scope: string): string {
  return `localplane.prefs.${scope}`;
}

function read(scope: string): Preferences {
  try {
    const raw = window.localStorage.getItem(storageKey(scope));
    if (!raw) return { theme: DEFAULT_THEME };
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== 'object' || parsed === null) return { theme: DEFAULT_THEME };
    const theme = (parsed as { theme?: unknown }).theme;
    return { theme: isTheme(theme) ? theme : DEFAULT_THEME };
  } catch {
    // A browser with storage disabled is a browser that gets defaults, not an error screen.
    return { theme: DEFAULT_THEME };
  }
}

export function PreferencesProvider({
  children,
  scope = 'local',
}: {
  children: ReactNode;
  scope?: string;
}): JSX.Element {
  const [preferences, setPreferences] = useState<Preferences>(() => read(scope));

  useEffect(() => {
    try {
      window.localStorage.setItem(storageKey(scope), JSON.stringify(preferences));
    } catch {
      /* preferences are a convenience; failing to persist them is not worth an error */
    }
  }, [preferences, scope]);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', preferences.theme);
  }, [preferences.theme]);

  const setTheme = useCallback((theme: ThemeId) => setPreferences((p) => ({ ...p, theme })), []);

  const value = useMemo<PreferencesApi>(
    () => ({ ...preferences, setTheme }),
    [preferences, setTheme],
  );

  return <PreferencesContext.Provider value={value}>{children}</PreferencesContext.Provider>;
}

export function usePreferences(): PreferencesApi {
  const context = useContext(PreferencesContext);
  if (!context) throw new Error('usePreferences used outside PreferencesProvider');
  return context;
}
