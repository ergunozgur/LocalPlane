/**
 * Protection, per reason.
 *
 * The headline conclusion is the backend's, and so is every reason under it. The panel's one
 * job is to keep `unknown` from reading like `clear`: an unsettled reason is shown with the
 * unknown tone, its own dashed treatment and the evidence that would have settled it, rather
 * than being folded into a green summary because "nothing was proven against it".
 *
 * `clear` carries its own caveat, because the backend is explicit that the word is scoped to
 * the reasons this build implements and is not a synonym for safe.
 */
import type { ObjectProtection } from '@/api/types';
import type { Resource } from '@/hooks/useResource';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { ResourceView } from '@/components/states/ResourceView';
import { StatusPill } from './StatusPill';
import { Conclusion, Gaps } from './Evidence';
import { Value } from './UnknownValue';
import { managementPathRelation, protection as protectionOf } from '@/domain/vocabulary';
import { formatTimestamp } from '@/domain/format';
import styles from './ProtectionPanel.module.css';

export function ProtectionPanel({
  resource,
}: {
  resource: Resource<ObjectProtection>;
}): JSX.Element {
  return (
    <Plate
      tone={
        resource.status === 'success' && resource.data.status === 'protected'
          ? 'attention'
          : undefined
      }
    >
      <PlateHead title="Protection" level={3} meta="what changing this would put at risk" />
      <PlateBody>
        <ResourceView resource={resource} what="protection assessment">
          {(data) => (
            <>
              <Conclusion
                semantic={protectionOf(data.status)}
                token={data.status}
                why={data.reason}
              />

              <div className={styles.pathRow}>
                <span className="label">Management path</span>
                <StatusPill
                  semantic={managementPathRelation(data.management_path)}
                  token={data.management_path}
                />
              </div>

              <div className={styles.reasons}>
                <div className={`${styles.reasonsHead} label`}>
                  Reasons evaluated · {data.implemented_reasons.length}
                </div>
                <KeyValueList>
                  {data.assessed.map((reason) => (
                    <KeyValue
                      key={reason.reason}
                      label={<span className="mono">{reason.reason}</span>}
                      hint={
                        reason.observed_at
                          ? formatTimestamp(reason.observed_at) ?? undefined
                          : undefined
                      }
                    >
                      <span className={styles.reasonRow}>
                        <StatusPill semantic={protectionOf(reason.status)} size="sm" />
                        <Value value={reason.detail} mono />
                      </span>
                    </KeyValue>
                  ))}
                </KeyValueList>
              </div>

              <Gaps items={data.missing_evidence} label="Evidence that would settle this" />

              <p className={styles.note}>{data.note}</p>
            </>
          )}
        </ResourceView>
      </PlateBody>
    </Plate>
  );
}
