import type { Capability, SystemdUnit } from '@/api/types';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { StatusPill } from './StatusPill';
import { Value } from './UnknownValue';
import { ServicePlan } from '@/components/composer/ServicePlan';
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
        <ServicePlan key={unit.object_id} unit={unit} />

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
              the published plan, read through the execution gate. Only the published preview can enable an ordinary apply in Composer.
            </p>
          </div>
        ) : null}
      </PlateBody>
    </Plate>
  );
}
