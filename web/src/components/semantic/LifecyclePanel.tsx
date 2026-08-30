/**
 * Lifecycle, presented without implying a capability this surface does not offer.
 *
 * This is the surface the whole execution-gate design exists for. The agent on the host this
 * was written against reports `systemd.service.lifecycle` as `available` and `mutating: true`
 * — truthfully, because systemd does expose the job contract — and the backend now has an
 * executor behind it: an eligible systemd plan can be applied through the API. What this
 * console does not have is authentication, so it renders none of that capability as a
 * control.
 *
 * So this panel does three things and refuses a fourth:
 *
 *  1. It shows what systemd itself says about whether the unit *can* be started or stopped
 *     (`can_start`, `refuse_manual_start`, …). Those are read facts and belong to the unit.
 *  2. It shows the agent capability as what it is — a probed mechanism — with the caveat
 *     attached, so a reader cannot mistake it for permission.
 *  3. It states plainly that this console offers no control, and why.
 *
 *  4. It never renders a Start, Stop or Restart control — not enabled, and not disabled
 *     either. A greyed-out button is still a claim that this surface has the feature and is
 *     withholding it, and this surface deliberately does not.
 *
 * The write path exists on the backend and is gated on `executionGate(preview.how)`; this
 * console stays read-only until authentication exists, without any of the above changing.
 */
import type { Capability, SystemdUnit } from '@/api/types';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { StatusPill } from './StatusPill';
import { Value } from './UnknownValue';
import { NotAssessed } from '@/components/states/SurfaceState';
import { capabilityStatus } from '@/domain/vocabulary';
import { executionGate } from '@/domain/execution';
import styles from './LifecyclePanel.module.css';

export function LifecyclePanel({
  unit,
  capability,
}: {
  unit: SystemdUnit;
  capability: Capability | null;
}): JSX.Element {
  // No plan has been published for this unit from here, so there is no `PlanExecution` to
  // read — and the gate says `not_assessed` rather than guessing from the capability.
  const gate = executionGate(null);

  return (
    <Plate>
      <PlateHead title="Lifecycle" level={3} meta="starting, stopping and restarting" />
      <PlateBody>
        <NotAssessed title="This console offers no lifecycle control for systemd units">
          The backend can start, stop and restart units — <span className="mono">systemd.service.start</span>,{' '}
          <span className="mono">.stop</span> and <span className="mono">.restart</span> have an
          executor, and an eligible plan is applied through the API. This console does not expose
          that path — it stays read-only until authentication exists — and no control is shown
          here, not even a disabled one, because a control an operator can see is a claim about
          what this surface does.
        </NotAssessed>

        <div className={styles.gate}>
          <span className="label">Execution</span>
          <span className={styles.gateValue}>
            {gate.state.replace(/_/g, ' ')} — no plan has been published from this page
          </span>
        </div>

        <div className={styles.section}>
          <div className={`${styles.sectionHead} label`}>What systemd says about this unit</div>
          <KeyValueList columns="auto">
            <KeyValue label="Can start">
              <Value
                value={unit.can_start === null ? null : unit.can_start ? 'yes' : 'no'}
                mono
                reason="the manager did not report this property"
              />
            </KeyValue>
            <KeyValue label="Can stop">
              <Value
                value={unit.can_stop === null ? null : unit.can_stop ? 'yes' : 'no'}
                mono
                reason="the manager did not report this property"
              />
            </KeyValue>
            <KeyValue label="Can reload">
              <Value
                value={unit.can_reload === null ? null : unit.can_reload ? 'yes' : 'no'}
                mono
                reason="the manager did not report this property"
              />
            </KeyValue>
            <KeyValue label="Refuses manual start">
              <Value
                value={
                  unit.refuse_manual_start === null ? null : unit.refuse_manual_start ? 'yes' : 'no'
                }
                mono
              />
            </KeyValue>
            <KeyValue label="Refuses manual stop">
              <Value
                value={
                  unit.refuse_manual_stop === null ? null : unit.refuse_manual_stop ? 'yes' : 'no'
                }
                mono
              />
            </KeyValue>
            <KeyValue label="Needs daemon reload">
              <Value
                value={
                  unit.need_daemon_reload === null ? null : unit.need_daemon_reload ? 'yes' : 'no'
                }
                mono
              />
            </KeyValue>
          </KeyValueList>
          <p className={styles.caveat}>
            These are systemd's own properties about the unit. They describe what the manager
            would accept — not what LocalPlane can ask it to do.
          </p>
        </div>

        {capability ? (
          <div className={styles.section}>
            <div className={`${styles.sectionHead} label`}>Agent capability</div>
            <KeyValueList>
              <KeyValue label={<span className="mono">{capability.capability}</span>}>
                <span className={styles.capabilityRow}>
                  <StatusPill semantic={capabilityStatus(capability.status)} size="sm" />
                  {capability.mutating ? <span className={styles.mutating}>mutating</span> : null}
                </span>
              </KeyValue>
            </KeyValueList>
            <p className={styles.caveat}>
              A capability records that the agent <em>probed a mechanism and found it</em>. It is
              not permission to act, and it is not what an execution decision rests on: that is
              the published plan, read through the execution gate. This console renders no control
              from either, and offers no way to write from this page.
            </p>
          </div>
        ) : null}
      </PlateBody>
    </Plate>
  );
}
