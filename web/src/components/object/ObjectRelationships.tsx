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

function Target({ target }: { target: ObjectRelationship['target'] }): JSX.Element {
  if (target.href) {
    return (
      <Link to={target.href} className={styles.target}>
        {target.name}
      </Link>
    );
  }
  return (
    <span className={target.unresolved ? styles.unresolved : styles.target}>
      {target.name}
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
