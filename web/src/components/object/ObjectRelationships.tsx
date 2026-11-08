import { Link } from 'react-router-dom';
import type {
  DockerContainer,
  DockerContainerList,
  ManagementPath,
  NetworkInterface,
  NetworkInterfaceList,
} from '@/api/types';
import type { Resource } from '@/hooks/useResource';
import { DataTable } from '@/components/primitives/DataTable';
import { Plate, PlateFoot, PlateHead } from '@/components/primitives/Plate';
import { StatusPill } from '@/components/semantic/StatusPill';
import { Degraded, Empty } from '@/components/states/SurfaceState';
import { deriveRelationships, type ObjectRelationship } from '@/domain/relationships';
import { inventoryMembership } from '@/domain/inventory';
import { freshness } from '@/domain/vocabulary';
import { formatRelative } from '@/domain/format';
import styles from './ObjectRelationships.module.css';

type Subject = NetworkInterface | DockerContainer;

export function ObjectRelationships({
  subject,
  interfaces,
  containers,
  managementPath,
}: {
  subject: Subject;
  interfaces: Resource<NetworkInterfaceList>;
  containers: Resource<DockerContainerList>;
  managementPath: Resource<ManagementPath>;
}): JSX.Element {
  const interfaceData = interfaces.status === 'success' ? interfaces.data.interfaces : [];
  const containerData = containers.status === 'success' ? containers.data.containers : [];
  const pathData = managementPath.status === 'success' ? managementPath.data : null;
  const relationships = deriveRelationships({
    subject,
    interfaces: interfaceData,
    containers: containerData,
    managementPath: pathData,
    interfaceEnvelope: interfaces.status === 'success' ? interfaces.data : null,
    containerEnvelope: containers.status === 'success' ? containers.data : null,
  });
  const notices = [
    ...streamState('interfaces', interfaces),
    ...streamState('containers', containers),
    ...streamState('management path', managementPath),
  ];

  return (
    <Plate>
      <PlateHead
        title="Relationships"
        level={3}
        meta="every one of these is backed by something read from the host"
      />
      {notices.length > 0 ? (
        <div className={styles.notices} aria-live="polite">
          {notices.map((notice) => (
            <Degraded
              key={notice.label}
              tone={notice.kind === 'loading' ? 'unknown' : 'warn'}
              title={
                notice.kind === 'loading'
                  ? `loading: ${notice.label}`
                  : `unread: ${notice.label}${notice.detail ? ` — ${notice.detail}` : ''}`
              }
            >
              {notice.kind === 'loading'
                ? 'Relationships derived from this stream are not shown yet.'
                : 'What is shown below is real; what this stream would add is unknown.'}
            </Degraded>
          ))}
        </div>
      ) : null}
      {relationships.length > 0 ? (
        <DataTable
          caption="Object relationships"
          rows={relationships}
          rowKey={(row) => `${row.type}-${row.target.objectId ?? row.target.name}-${row.evidence}`}
          columns={[
            {
              key: 'type',
              header: 'Type',
              width: '110px',
              render: (row) => <span className={styles.type}>{row.target.kind}</span>,
            },
            {
              key: 'object',
              header: 'Object',
              width: '220px',
              render: (row) => <Target target={row.target} />,
            },
            {
              key: 'relationship',
              header: 'Relationship',
              width: '170px',
              render: (row) => (
                <span>
                  {row.type}
                  {row.guarded ? (
                    <StatusPill
                      className={styles.guarded}
                      size="sm"
                      semantic={{
                        tone: 'warn',
                        label: 'guarded',
                        description:
                          'This request arrives over this interface, so a change to it is guarded.',
                      }}
                    />
                  ) : null}
                </span>
              ),
            },
            {
              key: 'coverage',
              header: 'Coverage',
              width: '200px',
              render: (row) => (
                <EndpointCoverage
                  subject={{
                    observedInLatestSweep: row.subjectObservedInLatestSweep ?? null,
                    freshness: row.subjectFreshness ?? null,
                    sweepStatus: row.subjectSweepStatus ?? null,
                    sweepFreshness: row.subjectSweepFreshness ?? null,
                  }}
                  counterpart={row.target}
                />
              ),
            },
            {
              key: 'evidence',
              header: 'Evidence',
              render: (row) => <span className={styles.evidence}>{row.evidence}</span>,
            },
            {
              key: 'open',
              header: '',
              width: '78px',
              align: 'right',
              render: (row) =>
                row.target.href ? (
                  <Link to={row.target.href} className={styles.open}>
                    open ›
                  </Link>
                ) : (
                  <span className={styles.noOpen}>{row.target.unresolved ? 'unresolved' : '—'}</span>
                ),
            },
          ]}
        />
      ) : notices.length === 0 ? (
        <Empty
          title="No relationship is established"
          explanation="No published identifier joins this object to another object in the streams that were read."
        />
      ) : null}
      <PlateFoot>
        <span className={styles.footerNote}>
          LocalPlane does not draw a relationship it cannot show you the evidence for.
        </span>
      </PlateFoot>
    </Plate>
  );
}

interface EndpointCoverageProps {
  subject: {
    observedInLatestSweep?: boolean | null;
    freshness?: string | null;
    sweepStatus?: string | null;
    sweepFreshness?: string | null;
  };
  counterpart: ObjectRelationship['target'];
}

function EndpointCoverage({ subject, counterpart }: EndpointCoverageProps): JSX.Element {
  return (
    <span className={styles.coverage}>
      <span className={styles.coverageItem} data-testid="subject-coverage">
        <span className={styles.coverageLabel}>subject</span>
        <StatusPill
          semantic={inventoryMembership(subject.observedInLatestSweep)}
          size="sm"
        />
        {subject.freshness ? (
          <StatusPill semantic={freshness(subject.freshness)} size="sm" />
        ) : (
          <span className={styles.notObserved}>not observed</span>
        )}
        <span className={styles.sweepStatus}>{sweepLabel(subject.sweepStatus, subject.sweepFreshness)}</span>
      </span>
      <span className={styles.coverageItem} data-testid="counterpart-coverage">
        <span className={styles.coverageLabel}>counterpart</span>
        {counterpart.freshness ? (
          <StatusPill semantic={freshness(counterpart.freshness)} size="sm" />
        ) : (
          <span className={styles.notObserved}>{counterpart.kind === 'Docker network' ? 'not independently observed' : 'not observed'}</span>
        )}
        <span className={styles.sweepStatus}>
          {counterpart.kind === 'Docker network' ? 'source inventory ' : ''}
          {sweepLabel(counterpart.sweepStatus, counterpart.sweepFreshness)}
        </span>
      </span>
    </span>
  );
}

function sweepLabel(status: string | null | undefined, sweepFreshness: string | null | undefined): string {
  if (status === null || status === undefined) return 'inventory not read';
  const currency = sweepFreshness === null || sweepFreshness === undefined ? 'unknown currency' : sweepFreshness;
  return `sweep ${status} · ${currency}`;
}

function Target({ target }: { target: ObjectRelationship['target'] }): JSX.Element {
  const membership =
    target.observedInLatestSweep === undefined ? null : (
      <StatusPill semantic={inventoryMembership(target.observedInLatestSweep)} size="sm" />
    );
  const age = target.observedAt ? formatRelative(target.observedAt) : null;
  const agePill = age ? <span className={styles.targetAge}>{age}</span> : null;
  const retainedNote =
    target.observedInLatestSweep === false ? (
      <span className={styles.retainedNote} title="Retained from an earlier sweep, not sighted in the latest one.">
        retained
      </span>
    ) : null;
  if (target.href) {
    return (
      <span className={styles.targetRow}>
        <Link to={target.href} className={styles.target}>
          {target.name}
        </Link>
        {membership}
        {agePill}
        {retainedNote}
      </span>
    );
  }
  return (
    <span className={styles.targetRow}>
      <span className={target.unresolved ? styles.unresolved : styles.target}>
        {target.name}
      </span>
      {membership}
      {agePill}
      {retainedNote}
    </span>
  );
}

type Notice = { label: string; kind: 'loading' | 'unread'; detail?: string };

function streamState<T>(label: string, resource: Resource<T>): Notice[] {
  if (resource.status === 'loading') return [{ label, kind: 'loading' }];
  if (resource.status === 'failed') {
    return [{ label, kind: 'unread', detail: resource.error.code ?? resource.error.kind }];
  }
  return [];
}
