/**
 * What LocalPlane intends for an object, and whether the host agrees.
 *
 * Four states have to stay distinguishable, and the backend keeps them apart deliberately:
 *
 *   no intent      the object is observed; nothing is retained, so it cannot drift
 *   in sync        every controlled field matches what is retained
 *   drifted        a controlled field no longer matches
 *   unknown        a controlled value could not be read, so no comparison was possible
 *
 * The fourth is the one a UI is most tempted to collapse. "Could not read it" is not
 * "in sync", and this panel renders it as its own answer.
 *
 * Intent is versioned: revising it replaces the active version and keeps the old one. A
 * drift that ends by revision is resolved `intent_revised`, which is not the same as the
 * host having been put right — so history is shown rather than only the current version.
 */
import type { Intent, IntentHistory, NetworkInterface, ReconciliationResult } from '@/api/types';
import type { Resource } from '@/hooks/useResource';
import { Plate, PlateBody, PlateFoot, PlateHead } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { StatusPill } from './StatusPill';
import { Comparator, type Relation } from './Comparator';
import { Value } from './UnknownValue';
import { Disclosure } from './Evidence';
import { Empty } from '@/components/states/SurfaceState';
import { reconciliation as reconciliationOf } from '@/domain/vocabulary';
import { formatTimestamp, formatTypedValue } from '@/domain/format';
import styles from './IntentPanel.module.css';

/**
 * The backend's comparison vocabulary, mapped onto the comparator's three relations.
 *
 * Total by construction: anything this build does not recognise becomes `na`, an empty
 * operator, never `eq`. A word LocalPlane cannot read must not be rendered as agreement.
 */
const RELATION: Readonly<Record<string, Relation>> = {
  matches: 'eq',
  differs: 'ne',
  unknown: 'na',
};

export function IntentPanel({
  object,
  intent,
  reconciliation,
  history,
}: {
  object: NetworkInterface;
  intent: Resource<Intent | null>;
  reconciliation: Resource<ReconciliationResult | null>;
  history: Resource<IntentHistory | null>;
}): JSX.Element {
  const managed = object.management.state === 'managed';

  return (
    <Plate tone={object.reconciliation === 'drifted' ? 'attention' : undefined}>
      <PlateHead
        title="Intent"
        level={3}
        meta={managed ? 'what LocalPlane retains for this object' : 'nothing is retained'}
        chips={
          <StatusPill
            semantic={reconciliationOf(object.reconciliation)}
            size="sm"
            token={reconciliation.status === 'success' ? reconciliation.data?.reconciliation?.reason : undefined}
          />
        }
      />
      <PlateBody>
        {!managed ? (
          <Empty
            title="No intent"
            explanation="This object is observed. LocalPlane retains no desired state for it, so there is nothing for it to drift from — which is not the same as being in sync."
          />
        ) : (
          <>
            {intent.status === 'success' && intent.data ? (
              <KeyValueList columns="auto">
                <KeyValue label="Intent id">
                  <Value value={intent.data.intent_id} mono />
                </KeyValue>
                <KeyValue label="Version" hint={intent.data.active ? 'in force' : 'superseded'}>
                  <Value value={intent.data.version} mono />
                </KeyValue>
                <KeyValue label="Origin" hint="how this version came to exist">
                  <Value value={intent.data.origin} mono />
                </KeyValue>
                <KeyValue label="Supersedes">
                  <Value
                    value={intent.data.supersedes}
                    mono
                    reason="this is the first version retained for this object"
                  />
                </KeyValue>
                <KeyValue label="Created">
                  <Value value={formatTimestamp(intent.data.created_at)} />
                </KeyValue>
                <KeyValue label="Captured from" hint="the observation it was written against">
                  <Value value={intent.data.captured_from.observation_id} mono />
                </KeyValue>
              </KeyValueList>
            ) : null}

            {reconciliation.status === 'success' && reconciliation.data?.reconciliation ? (
              <div className={styles.fields}>
                <div className={`${styles.head} label`}>Controlled fields</div>
                {reconciliation.data.reconciliation.fields.length === 0 ? (
                  <Empty
                    title="No controlled fields"
                    explanation="The retained intent controls nothing on this object."
                  />
                ) : (
                  <Comparator
                    source={intent.status === 'success' && intent.data ? `v${intent.data.version}` : undefined}
                    rows={reconciliation.data.reconciliation.fields.map((row) => ({
                      field: row.field,
                      relation: RELATION[row.comparison] ?? 'na',
                      drift: row.comparison === 'differs',
                      observed:
                        row.comparison === 'unknown' ? (
                          <Value
                            value={formatTypedValue(row.observed)}
                            mono
                            reason="this value could not be read — unknown, never drift"
                          />
                        ) : (
                          formatTypedValue(row.observed)
                        ),
                      intended: formatTypedValue(row.intended),
                    }))}
                  />
                )}
              </div>
            ) : null}

            {history.status === 'success' && history.data && history.data.intents.length > 0 ? (
              <Disclosure summary="Intent history" count={history.data.count}>
                <p className={styles.note}>
                  Revising intent replaces the active version and keeps the one it replaced.
                  Nothing here was written to the host: a drift that ends this way is resolved{' '}
                  <code>intent_revised</code>, never as though anything had been put right.
                </p>
                <KeyValueList>
                  {history.data.intents.map((version) => (
                    <KeyValue
                      key={version.intent_id}
                      label={
                        <span className="mono">
                          v{version.version}
                          {version.active ? ' ·' : ''}
                        </span>
                      }
                      hint={formatTimestamp(version.created_at) ?? undefined}
                    >
                      <span className={styles.revision}>
                        <Value value={version.origin} mono />
                        {version.active ? <span className={styles.active}>in force</span> : null}
                      </span>
                    </KeyValue>
                  ))}
                </KeyValueList>
              </Disclosure>
            ) : null}
          </>
        )}
      </PlateBody>
      <PlateFoot source="reconciliation is recomputed on every read; it is never stored" />
    </Plate>
  );
}
