/**
 * A container's resource usage, from the backend's own sample.
 *
 * The design direction shows CPU, memory, network and block I/O as meters. Everything here
 * comes from one `POST …/stats` call, taken on demand when a detail page is opened — never
 * once per row of a list, which would turn a page load into N requests against a backend
 * with a known concurrency defect.
 *
 * It is a **sample, not a feed**: `sampled_at` is shown so the number is read as a moment
 * rather than as a live value, and `gaps` names anything the daemon did not report. A metric
 * the sample could not supply renders as unknown, never as zero.
 */
import type { ContainerStats } from '@/api/types';
import { KeyValue, KeyValueList } from '@/components/primitives/KeyValue';
import { Meter } from './Metric';
import { Value } from './UnknownValue';
import { Gaps } from './Evidence';
import { formatBytes, formatTimestamp } from '@/domain/format';
import styles from './ResourceUsage.module.css';

export function ResourceUsage({ stats }: { stats: ContainerStats }): JSX.Element {
  return (
    <>
      <div className={styles.meters}>
        <div className={styles.row}>
          <span className={styles.label}>CPU</span>
          <Meter percent={stats.cpu_percent ?? null} tone="neutral" wide label="CPU" />
          <span className={styles.value}>
            <Value
              value={stats.cpu_percent === null || stats.cpu_percent === undefined ? null : `${stats.cpu_percent.toFixed(2)}%`}
              mono
            />
          </span>
          <span className={styles.qualifier}>
            {stats.online_cpus === null || stats.online_cpus === undefined
              ? ''
              : `of ${stats.online_cpus} cores`}
          </span>
        </div>

        <div className={styles.row}>
          <span className={styles.label}>Memory</span>
          <Meter percent={stats.memory_percent ?? null} tone="good" wide label="Memory" />
          <span className={styles.value}>
            <Value value={formatBytes(stats.memory_usage_bytes)} mono />
          </span>
          <span className={styles.qualifier}>
            {stats.memory_limit_bytes ? `limit ${formatBytes(stats.memory_limit_bytes)}` : ''}
            {stats.memory_percent === null || stats.memory_percent === undefined
              ? ''
              : ` · ${stats.memory_percent.toFixed(1)}%`}
          </span>
        </div>
      </div>

      <KeyValueList columns="auto">
        <KeyValue label="Network in">
          <Value value={formatBytes(stats.network_rx_bytes)} mono />
        </KeyValue>
        <KeyValue label="Network out">
          <Value value={formatBytes(stats.network_tx_bytes)} mono />
        </KeyValue>
        <KeyValue label="Block read">
          <Value value={formatBytes(stats.block_read_bytes)} mono />
        </KeyValue>
        <KeyValue label="Block written">
          <Value value={formatBytes(stats.block_write_bytes)} mono />
        </KeyValue>
        <KeyValue label="PIDs" hint={stats.pids_limit ? `limit ${stats.pids_limit}` : undefined}>
          <Value value={stats.pids} mono />
        </KeyValue>
        <KeyValue label="Sampled at">
          <Value value={formatTimestamp(stats.sampled_at)} />
        </KeyValue>
      </KeyValueList>

      <Gaps items={stats.gaps} label="Not reported in this sample" />

      <p className={styles.note}>
        One sample, taken when this page asked. Nothing here polls, so these are the values at
        that moment rather than a live reading.
      </p>
    </>
  );
}
