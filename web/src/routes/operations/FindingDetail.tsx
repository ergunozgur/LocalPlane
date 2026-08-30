/**
 * One finding, and the evidence it rests on.
 *
 * The backend publishes two shapes of evidence, discriminated by `finding_type`: a drift
 * claim carries the intent, the field, the intended and observed values and whether they are
 * even comparable; an ownership-conflict claim carries the owner, the confidence and the
 * source that argued for it. Both are rendered as published.
 *
 * Nothing here re-derives a verdict. In particular a `comparison` of `unknown` is shown as
 * *not comparable* — one side could not be read — and never as agreement.
 */
import { useCallback } from 'react';
import { useParams } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { Plate, PlateBody, PlateFoot, PlateHead } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { Tag } from '@/components/primitives/Chip';
import { StatusPill } from '@/components/semantic/StatusPill';
import { Value } from '@/components/semantic/UnknownValue';
import { Conclusion } from '@/components/semantic/Evidence';
import { ResourceView } from '@/components/states/ResourceView';
import { PageHeader } from '../PageHeader';
import { ScopeBar } from '@/components/layout/ScopeBar';
import { findingComparison, findingStatus, humanise } from '@/domain/vocabulary';
import { formatTimestamp, formatTypedValue } from '@/domain/format';
import styles from './FindingDetail.module.css';

/**
 * Read a string out of the evidence object.
 *
 * `Finding.evidence` is a union of two shapes, so it arrives as an untyped record. Pulling a
 * field through this keeps a non-string from being stringified into `[object Object]` on
 * screen — an evidence view that garbles its own evidence is worse than one that says the
 * field is absent.
 */
function text(value: unknown): string | null {
  if (typeof value === 'string') return value || null;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return null;
}

/** The backend models a typed value as `{type, value}`; render the value, keep the type. */
function typed(value: unknown): { text: string | null; kind: string | null } {
  if (typeof value !== 'object' || value === null) return { text: null, kind: null };
  const record = value as { value?: unknown; type?: unknown };
  const raw = record.value;
  return {
    text:
      raw === null || raw === undefined
        ? null
        : formatTypedValue(raw as boolean | number | string),
    kind: typeof record.type === 'string' ? record.type : null,
  };
}

export function FindingDetail(): JSX.Element {
  const { findingId = '' } = useParams();
  const { resource } = useResource(
    `finding:${findingId}`,
    useCallback((signal) => endpoints.finding(findingId, { signal }), [findingId]),
  );

  return (
    <ResourceView resource={resource} what="finding" loadingLabel="Reading finding…">
      {(finding, meta) => {
        const evidence = finding.evidence as Record<string, unknown>;
        const isDrift = 'field' in evidence;
        const intended = typed(evidence['intended']);
        const observed = typed(evidence['observed']);

        return (
          <>
            <ScopeBar
              crumbs={[
                { label: 'operations', to: '/operations' },
                { label: 'findings', to: '/operations/findings' },
                { label: finding.object_name, mono: true },
              ]}
              observedAt={meta.fetchedAt}
            />

            <PageHeader
              title={finding.summary}
              back={{ to: '/operations/findings', label: 'Findings' }}
              annotation="A finding is an interpretation, not a fact. It records that LocalPlane noticed, when it first noticed, and how it ended."
            >
              <StatusPill semantic={findingStatus(finding.status)} token={finding.status} />
            </PageHeader>

            <div className={styles.columns}>
              <div className={styles.column}>
                <Plate>
                  <PlateHead title="The claim" level={3} />
                  <PlateBody>
                    <Conclusion
                      semantic={findingStatus(finding.status)}
                      token={finding.finding_type}
                      {...(finding.resolution ? { why: finding.resolution } : {})}
                    />
                    <KeyValueList columns="auto">
                      <KeyValue label="Object">
                        <Value value={finding.object_name} mono />
                      </KeyValue>
                      <KeyValue label="Subject" hint="what within the object">
                        <Value value={finding.subject} mono />
                      </KeyValue>
                      <KeyValue label="Type">
                        <Value value={finding.finding_type} mono />
                      </KeyValue>
                      <KeyValue label="Finding id">
                        <Tag title={finding.finding_id}>{finding.finding_id}</Tag>
                      </KeyValue>
                      <KeyValue label="Key" hint="the stable logical identity">
                        <Tag title={finding.finding_key}>{finding.finding_key}</Tag>
                      </KeyValue>
                    </KeyValueList>
                  </PlateBody>
                </Plate>

                <Plate>
                  <PlateHead
                    title="Evidence"
                    level={3}
                    meta={isDrift ? 'the values this claim rests on' : 'who claims this object'}
                  />
                  <PlateBody>
                    {isDrift ? (
                      <>
                        <div className={styles.comparison}>
                          <div className={styles.side}>
                            <span className="label">Intended</span>
                            <span className={styles.sideValue}>
                              <Value value={intended.text} mono />
                            </span>
                            {intended.kind ? (
                              <span className={styles.sideKind}>{intended.kind}</span>
                            ) : null}
                          </div>
                          <div className={styles.operator}>
                            <StatusPill
                              semantic={findingComparison(text(evidence['comparison']))}
                              size="sm"
                            />
                          </div>
                          <div className={styles.side}>
                            <span className="label">Observed</span>
                            <span className={styles.sideValue}>
                              <Value
                                value={observed.text}
                                mono
                                reason="the last evaluation could not read this value"
                              />
                            </span>
                            {observed.kind ? (
                              <span className={styles.sideKind}>{observed.kind}</span>
                            ) : null}
                          </div>
                        </div>

                        <KeyValueList columns="auto">
                          <KeyValue label="Field">
                            <Value value={text(evidence['field'])} mono />
                          </KeyValue>
                          <KeyValue label="Reason">
                            <Value value={text(evidence['reason'])} mono />
                          </KeyValue>
                          <KeyValue label="Intent">
                            <Value value={text(evidence['intent_id'])} mono />
                          </KeyValue>
                          <KeyValue label="Observation">
                            <Value
                              value={(evidence['observation'] as string | null) ?? null}
                              mono
                              reason="no observation was recorded for this evaluation"
                            />
                          </KeyValue>
                        </KeyValueList>
                      </>
                    ) : (
                      <KeyValueList columns="auto">
                        <KeyValue label="Relation">
                          <Value value={text(evidence['relation'])} mono />
                        </KeyValue>
                        <KeyValue label="Owner">
                          <Value
                            value={
                              (evidence['owner'] as { provider?: string } | undefined)?.provider ??
                              null
                            }
                            mono
                          />
                        </KeyValue>
                        <KeyValue label="Confidence">
                          <Value value={text(evidence['confidence'])} mono />
                        </KeyValue>
                        <KeyValue label="Source">
                          <Value value={text(evidence['evidence_source'])} mono />
                        </KeyValue>
                        <KeyValue label="Reason">
                          <Value value={text(evidence['reason'])} mono />
                        </KeyValue>
                        <KeyValue label="Provider observation">
                          <Value
                            value={(evidence['provider_observation'] as string | null) ?? null}
                            mono
                          />
                        </KeyValue>
                      </KeyValueList>
                    )}
                  </PlateBody>
                  <PlateFoot source="published by the backend; nothing here re-derives it" />
                </Plate>
              </div>

              <div className={styles.column}>
                <Plate>
                  <PlateHead title="Timeline" level={3} />
                  <PlateBody>
                    <KeyValueList>
                      <KeyValue label="First seen" hint="when this episode opened">
                        <Value value={formatTimestamp(finding.first_seen_at)} />
                      </KeyValue>
                      <KeyValue label="Last seen" hint="the last observation that proved it">
                        <Value value={formatTimestamp(finding.last_seen_at)} />
                      </KeyValue>
                      <KeyValue label="Updated" hint="the last evaluation that touched it">
                        <Value value={formatTimestamp(finding.updated_at)} />
                      </KeyValue>
                      <KeyValue label="Resolved">
                        <Value
                          value={formatTimestamp(finding.resolved_at)}
                          reason="this finding is still open"
                        />
                      </KeyValue>
                    </KeyValueList>

                    {finding.resolution ? (
                      <p className={styles.resolution}>
                        Ended as <code>{finding.resolution}</code> — {humanise(finding.resolution)}.
                        A resolution says how the claim ended, not that the host was put right.
                      </p>
                    ) : null}
                  </PlateBody>
                </Plate>
              </div>
            </div>
          </>
        );
      }}
    </ResourceView>
  );
}
