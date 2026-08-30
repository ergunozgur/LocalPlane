/**
 * A container's recent log lines.
 *
 * Fetched on demand — the panel asks only when an operator opens it, because logs are a
 * request against the daemon and a detail page should not make one before it is wanted.
 *
 * The backend's own bounds are surfaced rather than hidden: it caps lines and bytes, and
 * says when a read was truncated. A log view that quietly drops the cap would leave an
 * operator believing they had seen the end of something.
 *
 * This is a **read, not a stream.** The design direction offers a "Live logs" tab; nothing
 * here tails, so that is not offered.
 */
import { useCallback, useState } from 'react';
import { endpoints } from '@/api/endpoints';
import { useResource } from '@/hooks/useResource';
import { ResourceView } from '@/components/states/ResourceView';
import { Empty } from '@/components/states/SurfaceState';
import { formatTimestamp } from '@/domain/format';
import styles from './ContainerLogPanel.module.css';

const CHOICES = [50, 200, 500] as const;

export function ContainerLogPanel({ objectId }: { objectId: string }): JSX.Element {
  const [open, setOpen] = useState(false);
  const [tail, setTail] = useState<number>(CHOICES[0]);

  if (!open) {
    return (
      <div className={styles.closed}>
        <button type="button" className={styles.load} onClick={() => setOpen(true)}>
          Read recent logs
        </button>
        <span className={styles.hint}>
          Asks the daemon for this container's most recent output. Nothing is read until you
          ask.
        </span>
      </div>
    );
  }

  return <Loaded objectId={objectId} tail={tail} onTail={setTail} />;
}

function Loaded({
  objectId,
  tail,
  onTail,
}: {
  objectId: string;
  tail: number;
  onTail: (value: number) => void;
}): JSX.Element {
  const { resource, refresh } = useResource(
    `container-logs:${objectId}:${tail}`,
    useCallback((signal) => endpoints.containerLogs(objectId, tail, { signal }), [objectId, tail]),
  );

  return (
    <>
      <div className={styles.controls}>
        <label className={styles.tail}>
          <span className="visually-hidden">Lines to read</span>
          <select
            className={styles.select}
            value={tail}
            onChange={(event) => onTail(Number(event.target.value))}
          >
            {CHOICES.map((choice) => (
              <option key={choice} value={choice}>
                last {choice} lines
              </option>
            ))}
          </select>
        </label>
        <button type="button" className={styles.load} onClick={refresh}>
          Read again
        </button>
      </div>

      <ResourceView resource={resource} what="log" loadingLabel="Reading logs…">
        {(logs) => (
          <>
            {logs.lines.length === 0 ? (
              <Empty
                title="No output"
                explanation="The daemon returned no lines for this container."
              />
            ) : (
              <pre className={styles.log}>
                {logs.lines.map((line, index) => (
                  <span key={`${line.timestamp}-${index}`} className={styles.line}>
                    <span className={styles.time}>
                      {formatTimestamp(line.timestamp) ?? '—'}
                    </span>
                    <span
                      className={line.stream === 'stderr' ? styles.stderr : styles.stdout}
                    >
                      {line.stream}
                    </span>
                    <span className={styles.message}>{line.message}</span>
                  </span>
                ))}
              </pre>
            )}

            <p className={styles.meta}>
              {logs.line_count} line{logs.line_count === 1 ? '' : 's'} · requested{' '}
              {logs.requested_lines} · read {formatTimestamp(logs.read_at)}
              {logs.truncated
                ? ` · truncated by the backend at ${logs.line_limit} lines / ${logs.byte_limit} bytes`
                : ''}
            </p>
          </>
        )}
      </ResourceView>
    </>
  );
}
