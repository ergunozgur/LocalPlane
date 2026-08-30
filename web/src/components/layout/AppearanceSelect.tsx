/**
 * Appearance and session, in the account menu.
 *
 * Appearance sits behind an avatar button, as theme buttons carrying a swatch of the
 * palette they select — which reads far better than a list of names, because the
 * choice being made is a visual one.
 *
 * The account card above it says what this session actually is: "operator · local
 * session" — and this build means it literally, because there is no authentication and
 * the backend records every confirmation as
 * `unauthenticated_request`. When a real user model arrives, this card gains values rather
 * than the menu gaining a concept.
 *
 * The options are exactly the themes that exist. No "system", no "auto", no greyed-out
 * coming-soon entry.
 */
import { Link } from 'react-router-dom';
import { THEMES, usePreferences, type ThemeId } from '@/preferences/preferences';
import { useViewer } from '@/identity/viewer';
import { useEstateCounts } from '@/hooks/useEstateCounts';
import { Menu, MenuLabel, MenuSeparator } from './Menu';
import styles from './AppearanceSelect.module.css';

/** The three swatch colours per theme: ground, accent, and the "good" channel. */
const SWATCHES: Record<ThemeId, readonly [string, string, string]> = {
  localplane: ['#0e0d12', '#9d7cf4', '#63b78f'],
  graphite: ['#e7e8e3', '#2f5c7a', '#3a6b52'],
};

export function AppearanceSelect(): JSX.Element {
  const { theme, setTheme } = usePreferences();
  const viewer = useViewer();
  const { changes: changeCount } = useEstateCounts();

  return (
    <Menu label="Account and appearance" trigger={<span className={styles.avatar} aria-hidden="true" />}>
      <div className={styles.account}>
        <span className={styles.avatarLarge} aria-hidden="true" />
        <div className={styles.accountText}>
          <div className={styles.accountName}>{viewer.displayName}</div>
          <div className={styles.accountSub}>
            {viewer.id === null ? 'local session · no user model' : viewer.id}
          </div>
        </div>
      </div>

      <MenuSeparator />

      <MenuLabel>Appearance</MenuLabel>
      <div className={styles.themes}>
        {THEMES.map((option) => (
          <button
            key={option.id}
            type="button"
            className={styles.theme}
            aria-pressed={theme === option.id}
            onClick={() => setTheme(option.id)}
          >
            <span className={styles.swatch} aria-hidden="true">
              {SWATCHES[option.id].map((colour) => (
                <i key={colour} style={{ background: colour }} />
              ))}
            </span>
            {option.name}
            {option.id === 'localplane' ? <span className={styles.default}>default</span> : null}
          </button>
        ))}
      </div>

      <p className={styles.note}>
        The alternative changes the atmosphere, never the structure — or what a colour means.
      </p>

      <MenuSeparator />

      <Link to="/operations" className={styles.entry}>
        <span className={styles.entryTitle}>Change ledger</span>
        <span className={styles.entryDescription}>
          {changeCount === null
            ? 'the record of every write boundary crossed'
            : `${changeCount.toLocaleString()} recorded change${changeCount === 1 ? '' : 's'}`}
        </span>
      </Link>

      <Link to="/settings" className={styles.entry}>
        <span className={styles.entryTitle}>Console settings</span>
        <span className={styles.entryDescription}>
          Appearance, and what this build does not yet let you configure
        </span>
      </Link>

      {/* The typed dashboard layout model exists; the editor does not. The entry marks where
          it lands rather than offering a control that would do nothing. */}
      <div className={styles.deferred}>
        <span className={styles.entryTitle}>
          Customize dashboard
          <span className={styles.deferredTag}>not in this build</span>
        </span>
        <span className={styles.entryDescription}>
          Moving, resizing and hiding the panels on Overview. The layout is already typed
          configuration; the editor for it is not built.
        </span>
      </div>

      {/* The menu ends with Sign out. There is no session to end: the backend records
          every confirmation as `unauthenticated_request`, and a sign-out control would be
          the one thing in this console that does nothing at all. */}
      <div className={styles.deferred}>
        <span className={styles.entryTitle}>
          Sign out
          <span className={styles.deferredTag}>no session</span>
        </span>
        <span className={styles.entryDescription}>
          This build has no authentication, so there is nothing to sign out of.
        </span>
      </div>
    </Menu>
  );
}
