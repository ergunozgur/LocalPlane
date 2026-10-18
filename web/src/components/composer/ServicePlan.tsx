import { useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { endpoints } from '@/api/endpoints';
import type { Run, SystemdUnit, SystemdServiceOperation } from '@/api/types';
import { runRequestError } from '@/hooks/useRun';
import { OPERATIONS_WITH_UI_CONTROLS } from '@/domain/execution';
import styles from './Composer.module.css';

export function ServicePlan({ unit }: { unit: SystemdUnit }): JSX.Element {
  const lock = useRef(false);
  const [pending, setPending] = useState(false);
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const plan = async (type: SystemdServiceOperation['type']): Promise<void> => {
    if (lock.current) return;
    lock.current = true;
    setPending(true);
    try { setRun(await endpoints.createServiceRun({ type, object_id: unit.object_id })); }
    catch (cause) { setError(cause instanceof Error ? cause : new Error('Planning response unavailable.')); }
    finally { setPending(false); }
  };
  if (unit.unit_type !== 'service' || unit.load_state !== 'loaded') {
    return <p className={styles.note}>Planning is offered for loaded service objects only. This unit is not applicable.</p>;
  }
  return <div>
    <p className={styles.note}>Plan a service action to inspect its risk, protection and required confirmation. Planning does not write to the host.</p>
    <div className={styles.actions} aria-label="Plan service action">
      {OPERATIONS_WITH_UI_CONTROLS.map((type) => <button key={type} type="button" className={styles.button} disabled={pending || !!run || !!error} onClick={() => void plan(type)}>Plan {type.split('.').at(-1)}</button>)}
    </div>
    {pending ? <p role="status">Planning… You can find the retained Run in Operations if you leave this page.</p> : null}
    {run ? <p role="status"><Link to={`/operations/runs/${encodeURIComponent(run.run_id)}`}>Open Composer · {run.object_name} · {run.operation}</Link></p> : null}
    {error ? <div role="alert" className={styles.error}><p>{runRequestError(error)}</p><p>Inspect <Link to="/operations?tab=runs">Runs in Operations</Link> before planning again; a Run may already have been created.</p></div> : null}
  </div>;
}
