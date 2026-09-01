/**
 * Settings.
 *
 * One real section. Appearance is implemented, so it is here; nothing else is, so nothing
 * else is. Account, Users and Administration are recorded in the implementation plan as
 * future areas and are deliberately absent from the interface — a settings page with three
 * greyed-out headings is a promise the build cannot keep, and this product's whole argument
 * is that it does not make those.
 *
 * The information architecture is what makes them additive: each area is a section on this
 * page or a sibling route, and per-user preferences already have a scope in
 * `PreferencesProvider`.
 */
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { AppearanceSelect } from '@/components/layout/AppearanceSelect';
import { PageHeader } from './PageHeader';
import styles from './Settings.module.css';
import { useViewer } from '@/identity/viewer';

export function Settings(): JSX.Element {
  const viewer = useViewer();

  return (
    <>
      <PageHeader
        title="Settings"
        annotation="Preferences here affect how this browser presents LocalPlane. None of them is consulted by any safety or domain judgement, and none of them is authority for anything."
      />

      <Plate>
        <PlateHead title="Appearance" level={3} meta="how this console looks" />
        <PlateBody>
          <KeyValueList>
            <KeyValue label="Theme" hint="stored in this browser">
              <AppearanceSelect />
            </KeyValue>
          </KeyValueList>
        </PlateBody>
      </Plate>

      {/* Console settings. These are real LocalPlane concepts — the confirmation
          policy is published in every plan, the guard window is a policy constant, and the
          observation interval governs freshness — but no settings endpoint exists, so each
          states what is in force and that it cannot be changed here. */}
      <Plate style={{ marginTop: 14 }}>
        <PlateHead
          title="Console settings"
          level={3}
          meta="what governs observation and writes on this host"
        />
        <PlateBody>
          <KeyValueList>
            <KeyValue label="Observation interval" hint="not configurable in this build">
              <span className={styles.deferred}>
                Observation is taken on request and at startup. Nothing polls, and there is no
                interval to set.
              </span>
            </KeyValue>
            <KeyValue label="Connection guard" hint="not configurable in this build">
              <span className={styles.deferred}>
                The guard window is a policy constant published in each plan, not a setting.
                There is no request field for it anywhere in the API.
              </span>
            </KeyValue>
            <KeyValue label="Confirmations" hint="not configurable in this build">
              <span className={styles.deferred}>
                Required for medium and high risk, for anything that can remove the management
                path, and for anything that cannot be undone. The policy in force is recorded
                on every plan.
              </span>
            </KeyValue>
          </KeyValueList>
          <p className="note" style={{ marginTop: 11, maxWidth: '80ch' }}>
            No settings endpoint exists in this build. These are stated so the policy in force
            is visible, not so it can be edited here.
          </p>
        </PlateBody>
      </Plate>

      <Plate style={{ marginTop: 14 }}>
        <PlateHead title="Identity" level={3} meta="who this session is attributed to" />
        <PlateBody>
          <KeyValueList>
            <KeyValue label="Viewer">{viewer.displayName}</KeyValue>
            <KeyValue label="User id" hint="this build has no user model">
              <span className="mono">{viewer.id ?? 'none'}</span>
            </KeyValue>
            <KeyValue label="Attribution">
              <span className="mono">{viewer.attribution}</span>
            </KeyValue>
          </KeyValueList>
          <p className="note" style={{ marginTop: 11, maxWidth: '80ch' }}>
            This browser crossed the authentication boundary, but LocalPlane has no user model.
            New confirmations record <span className="mono">authenticated_request</span> without
            inventing a person; historical records retain their original source. Nothing here grants or withholds permission: the backend
            refuses what must be refused whether or not this console draws a control.
          </p>
        </PlateBody>
      </Plate>
    </>
  );
}
