/**
 * One run, and the whole anatomy of its plan.
 *
 * This is where the execution gate meets real backend evidence. A plan carries what would
 * change, why, how it would execute, what protects the target, what it risks, what
 * confirmation it demands, how it would be verified, whether a guard is available and what
 * recovery exists — and this page renders all of it rather than reducing it to a verdict.
 *
 * The `how` block is the authority on whether anything may be executed. A legacy plan can
 * still read `not_implemented` with `capability_declared_by_agent: true`, and both are shown
 * together because the pair keeps the lesson: a probed mechanism and an executable plan are
 * different facts, and this console renders controls for neither.
 */
import { useCallback } from 'react';
import { Link, useParams } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { Plate, PlateBody, PlateHead } from '@/components/primitives/Plate';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { Tag } from '@/components/primitives/Chip';
import { StatusPill } from '@/components/semantic/StatusPill';
import { Value } from '@/components/semantic/UnknownValue';
import { Conclusion, Disclosure, Gaps, RawEvidence } from '@/components/semantic/Evidence';
import { ResourceView } from '@/components/states/ResourceView';
import { Empty } from '@/components/states/SurfaceState';
import { DataTable } from '@/components/primitives/DataTable';
import { PageHeader } from '../PageHeader';
import { executionGate } from '@/domain/execution';
import {
  executionAvailability,
  executionEligibility,
  hostEffect,
  protection as protectionOf,
  managementPathRelation,
  runState,
  humanise,
} from '@/domain/vocabulary';
import { formatTimestamp, formatTypedValue } from '@/domain/format';
import styles from './RunDetail.module.css';

export function RunDetail(): JSX.Element {
  const { runId = '' } = useParams();
  const { resource } = useResource(
    `run:${runId}`,
    useCallback((signal) => endpoints.run(runId, { signal }), [runId]),
  );

  return (
    <ResourceView resource={resource} what="run" loadingLabel="Reading run…">
      {(run) => {
        const preview = run.preview;
        const gate = executionGate(preview.how, preview.confirmation);

        return (
          <>
            <PageHeader
              title={<span className="mono">{run.operation}</span>}
              back={{ to: '/operations', label: 'Operations' }}
              annotation={preview.operation.summary}
            >
              <StatusPill semantic={runState(run.state)} token={run.state} />
              <StatusPill semantic={hostEffect(run.host_effect)} />
            </PageHeader>

            <div className={styles.columns}>
              <div className={styles.column}>
                {/* HOW — the execution gate. First, because it governs everything else. */}
                <Plate tone={gate.state === 'not_implemented' ? undefined : 'attention'}>
                  <PlateHead title="Execution" level={3} meta="whether this could run at all" />
                  <PlateBody>
                    <Conclusion
                      semantic={executionAvailability(preview.how.availability)}
                      token={preview.how.availability}
                    />
                    <div className={styles.gateRow}>
                      <span className="label">Eligibility</span>
                      <StatusPill
                        semantic={executionEligibility(preview.how.eligibility)}
                        token={preview.how.eligibility}
                      />
                    </div>

                    <KeyValueList>
                      <KeyValue label="Required capability">
                        <Value value={preview.how.required_capability} mono />
                      </KeyValue>
                      <KeyValue
                        label="Declared by agent"
                        hint={
                          preview.how.capability_declared_by_agent &&
                          preview.how.availability === 'not_implemented'
                            ? 'the mechanism exists; this stored plan predates the executor this build has'
                            : undefined
                        }
                      >
                        {preview.how.capability_declared_by_agent ? 'yes' : 'no'}
                      </KeyValue>
                      <KeyValue label="Provider">
                        <Value
                          value={preview.how.provider}
                          mono
                          reason="LocalPlane does not truthfully know which provider would perform this"
                        />
                      </KeyValue>
                      <KeyValue label="Control offered here">
                        <span className={styles.noControl}>
                          no — this build renders the write boundary and produces nothing across it
                        </span>
                      </KeyValue>
                    </KeyValueList>

                    <Gaps items={gate.blockers} label="Blockers" />

                    {preview.how.note ? (
                      <p className={styles.note}>{preview.how.note}</p>
                    ) : null}
                  </PlateBody>
                </Plate>

                {/* WHAT */}
                <Plate>
                  <PlateHead title="What would change" level={3} />
                  <PlateBody>
                    <KeyValueList columns="auto">
                      <KeyValue label="Object">
                        <Value value={preview.what.object_name} mono />
                      </KeyValue>
                      <KeyValue label="Kind"><Value value={preview.what.kind} mono /></KeyValue>
                      {preview.what.kind === 'field' ? (
                        <>
                          <KeyValue label="Field"><Value value={preview.what.field} mono /></KeyValue>
                          <KeyValue label="Current">
                            <Value value={formatTypedValue(preview.what.current)} mono />
                          </KeyValue>
                          <KeyValue label="Desired">
                            <Value value={formatTypedValue(preview.what.desired)} mono />
                          </KeyValue>
                        </>
                      ) : (
                        <>
                          <KeyValue label="Action"><Value value={preview.what.action} mono /></KeyValue>
                          <KeyValue label="Observed state">
                            <Value value={preview.what.observed_state} mono />
                          </KeyValue>
                          <KeyValue label="Expected state">
                            <Value value={preview.what.expected_state} mono />
                          </KeyValue>
                        </>
                      )}
                      <KeyValue label="Expected after" hint="what the host would read back">
                        <Value value={formatTypedValue(preview.what.expected_after)} mono />
                      </KeyValue>
                    </KeyValueList>
                  </PlateBody>
                </Plate>

                {/* PROTECTION */}
                <Plate tone={preview.protection.status === 'protected' ? 'attention' : undefined}>
                  <PlateHead title="Protection" level={3} />
                  <PlateBody>
                    <Conclusion
                      semantic={protectionOf(preview.protection.status)}
                      token={preview.protection.status}
                      why={preview.protection.reason}
                    />
                    <div className={styles.gateRow}>
                      <span className="label">Management path</span>
                      <StatusPill
                        semantic={managementPathRelation(preview.protection.management_path)}
                        token={preview.protection.management_path}
                      />
                    </div>
                    {preview.protection.assessed && preview.protection.assessed.length > 0 ? (
                      <KeyValueList>
                        {preview.protection.assessed.map((reason) => (
                          <KeyValue key={reason.reason} label={<span className="mono">{reason.reason}</span>}>
                            <span className={styles.inline}>
                              <StatusPill semantic={protectionOf(reason.status)} size="sm" />
                              <Value value={reason.detail} mono />
                            </span>
                          </KeyValue>
                        ))}
                      </KeyValueList>
                    ) : null}
                    <Gaps items={preview.protection.unresolved} label="Reasons that could not be settled" />
                    <Gaps items={preview.protection.missing_evidence} label="Missing evidence" />
                  </PlateBody>
                </Plate>

                {preview.systemd_lifecycle_context ? (
                  <SystemdContext context={preview.systemd_lifecycle_context} />
                ) : null}

                {/* EVENTS */}
                <Plate>
                  <PlateHead title="Transcript" level={3} meta={`${run.events.length} events`} />
                  <DataTable
                    caption="Run events"
                    rows={run.events}
                    rowKey={(row) => String(row.sequence)}
                    emptyState={<Empty title="No events" explanation="Nothing has been recorded for this run." />}
                    columns={[
                      { key: 'seq', header: '#', align: 'right', render: (row) => <span className="mono">{row.sequence}</span> },
                      { key: 'event', header: 'Event', render: (row) => <span className="mono">{row.event}</span> },
                      {
                        key: 'transition',
                        header: 'Transition',
                        render: (row) =>
                          row.state_from || row.state_to ? (
                            <span className="mono">
                              {row.state_from ?? '—'} → {row.state_to ?? '—'}
                            </span>
                          ) : (
                            <Value value={null} reason="this event is not a state transition" />
                          ),
                      },
                      { key: 'at', header: 'At', render: (row) => <Value value={formatTimestamp(row.occurred_at)} /> },
                    ]}
                  />
                </Plate>
              </div>

              <div className={styles.column}>
                {/* RISK */}
                <Plate>
                  <PlateHead title="Risk" level={3} />
                  <PlateBody>
                    <div className={styles.riskTier}>
                      <StatusPill
                        semantic={{
                          tone:
                            preview.risk.tier === 'high'
                              ? 'bad'
                              : preview.risk.tier === 'medium'
                                ? 'warn'
                                : 'good',
                          label: `${preview.risk.tier} risk`,
                          description: 'The tier the backend assigned this plan.',
                        }}
                      />
                    </div>
                    <KeyValueList>
                      {preview.risk.factors.map((factor) => (
                        <KeyValue key={factor.code} label={<span className="mono">{factor.code}</span>} hint={factor.floor}>
                          {factor.detail}
                        </KeyValue>
                      ))}
                    </KeyValueList>
                  </PlateBody>
                </Plate>

                {/* CONFIRMATION */}
                <Plate>
                  <PlateHead title="Confirmation" level={3} />
                  <PlateBody>
                    <KeyValueList>
                      <KeyValue label="Required">{preview.confirmation.required ? 'yes' : 'no'}</KeyValue>
                      <KeyValue label="Method"><Value value={preview.confirmation.method} mono /></KeyValue>
                      <KeyValue label="Satisfied">{preview.confirmation.satisfied ? 'yes' : 'no'}</KeyValue>
                      <KeyValue
                        label="Satisfiable"
                        hint={preview.confirmation.unsatisfiable_reason ?? undefined}
                      >
                        {preview.confirmation.satisfiable ? 'yes' : 'no'}
                      </KeyValue>
                      <KeyValue label="Attribution" hint="persisted confirmation source; no person is identified">
                        <span className="mono">{run.confirmation?.source ?? 'not recorded'}</span>
                      </KeyValue>
                    </KeyValueList>
                    <p className={styles.policy}>{preview.confirmation.policy}</p>
                    <Gaps items={preview.confirmation.reasons} label="Why confirmation is required" />
                  </PlateBody>
                </Plate>

                {/* VERIFICATION / GUARD / RECOVERY */}
                <Plate>
                  <PlateHead title="Verification" level={3} />
                  <PlateBody>
                    <KeyValueList>
                      <KeyValue label="Executed">{preview.verification.executed ? 'yes' : 'no'}</KeyValue>
                      <KeyValue label="Capability"><Value value={preview.verification.capability} mono /></KeyValue>
                      <KeyValue label="Provider"><Value value={preview.verification.provider} mono /></KeyValue>
                      <KeyValue label="Condition"><Value value={preview.verification.condition} mono /></KeyValue>
                      <KeyValue label="Expect">
                        <Value value={formatTypedValue(preview.verification.expect)} mono />
                      </KeyValue>
                    </KeyValueList>
                  </PlateBody>
                </Plate>

                <Plate>
                  <PlateHead title="Connection guard" level={3} />
                  <PlateBody>
                    <Conclusion
                      semantic={
                        preview.guard.availability === 'available'
                          ? { tone: 'good', label: 'available', description: 'A guarded path exists for this plan.' }
                          : { tone: 'neutral', label: 'unavailable', description: 'No connection guard is offered for this plan.' }
                      }
                      token={preview.guard.availability}
                      why={preview.guard.reason}
                    />
                    <p className={styles.policy}>{preview.guard.guarantee}</p>
                    <Gaps items={preview.guard.unmet} label="Unmet prerequisites" />
                  </PlateBody>
                </Plate>

                <Plate>
                  <PlateHead title="Recovery" level={3} />
                  <PlateBody>
                    <KeyValueList>
                      <KeyValue label="Mode"><Value value={preview.recovery.mode} mono /></KeyValue>
                      <KeyValue label="Rollback possible">
                        {preview.recovery.rollback_possible ? 'yes' : 'no'}
                      </KeyValue>
                      <KeyValue label="Restores field">
                        <Value value={preview.recovery.restores_field} mono reason="this operation has no inverse" />
                      </KeyValue>
                      <KeyValue label="Guarantee"><Value value={preview.recovery.guarantee} mono /></KeyValue>
                    </KeyValueList>
                    <p className={styles.policy}>{humanise(preview.recovery.reason)}</p>
                  </PlateBody>
                </Plate>

                <Plate>
                  <PlateHead title="Plan identity" level={3} />
                  <PlateBody>
                    <KeyValueList>
                      <KeyValue label="Preview id"><Tag title={preview.preview_id}>{preview.preview_id}</Tag></KeyValue>
                      <KeyValue label="Digest"><Tag title={preview.preview_digest}>{preview.preview_digest}</Tag></KeyValue>
                      <KeyValue label="Published"><Value value={formatTimestamp(preview.published_at)} /></KeyValue>
                      <KeyValue label="Validity" hint={preview.validity.state}>
                        <Value value={formatTimestamp(preview.validity.as_of)} />
                      </KeyValue>
                    </KeyValueList>
                    {preview.validity.reasons.length > 0 ? (
                      <Disclosure summary="Why this plan is stale" count={preview.validity.reasons.length}>
                        <KeyValueList>
                          {preview.validity.reasons.map((reason) => (
                            <KeyValue key={reason.code} label={<span className="mono">{reason.code}</span>}>
                              {reason.detail && Object.keys(reason.detail).length > 0 ? (
                                <RawEvidence value={reason.detail} maxChars={800} />
                              ) : (
                                <Value value={humanise(reason.code)} />
                              )}
                            </KeyValue>
                          ))}
                        </KeyValueList>
                      </Disclosure>
                    ) : null}
                  </PlateBody>
                </Plate>

                {run.change_id ? (
                  <Plate>
                    <PlateBody>
                      <Link to={`/operations/changes/${run.change_id}`}>
                        This run crossed the write boundary — see its change ›
                      </Link>
                    </PlateBody>
                  </Plate>
                ) : null}
              </div>
            </div>
          </>
        );
      }}
    </ResourceView>
  );
}

/**
 * The systemd effect graph.
 *
 * Only reachable inside a preview — there is no read-only endpoint for it — and worth
 * surfacing because it is the answer to "what else moves if this unit restarts".
 */
function SystemdContext({
  context,
}: {
  context: import('@/api/types').PlanSystemdLifecycleContext;
}): JSX.Element {
  return (
    <Plate>
      <PlateHead title="systemd effect graph" level={3} meta="what else this would move" />
      <PlateBody>
        <KeyValueList columns="auto">
          <KeyValue label="Target unit"><Value value={context.target_unit} mono /></KeyValue>
          <KeyValue label="Action"><Value value={context.action} mono /></KeyValue>
          <KeyValue label="Effect units">{context.effect_units.length}</KeyValue>
          <KeyValue
            label="Effect complete"
            hint={context.effect_complete ? undefined : 'the graph was truncated'}
          >
            {context.effect_complete ? 'yes' : 'no'}
          </KeyValue>
          <KeyValue label="Agent unit">
            <Value value={context.agent_unit} mono reason="the agent could not be correlated to a unit" />
          </KeyValue>
          <KeyValue label="Management units">{context.management_units.length}</KeyValue>
        </KeyValueList>

        {context.effect_units.length > 0 ? (
          <Disclosure summary="Units in the effect graph" count={context.effect_units.length}>
            <ul className={styles.unitList}>
              {context.effect_units.map((unit) => (
                <li key={unit}><Tag title={unit}>{unit}</Tag></li>
              ))}
            </ul>
          </Disclosure>
        ) : null}

        <Gaps items={context.gaps} label="Gaps in the effect graph" />
      </PlateBody>
    </Plate>
  );
}
