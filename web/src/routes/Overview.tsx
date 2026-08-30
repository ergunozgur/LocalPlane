/**
 * Overview.
 *
 * The home view reads top to bottom: **attention → the machine → supporting depth.** The
 * rail comes first because what needs looking at is read before the machine is; the widget
 * grid follows.
 *
 * The page holds no widget markup. It resolves a layout and hands it to the grid, so adding,
 * reordering, resizing or hiding a widget is a change to configuration rather than to this
 * file — which is what makes a future dashboard editor, user templates and per-user layouts
 * additive instead of a rewrite.
 */
import { useCallback } from 'react';
import { endpoints } from '@/api/endpoints';
import { combine, useResource } from '@/hooks/useResource';
import { DashboardGrid } from '@/dashboard/DashboardGrid';
import { DEFAULT_TEMPLATE } from '@/dashboard/registry';
import { AttentionRail, type AttentionItem } from '@/components/semantic/AttentionRail';

export function Overview(): JSX.Element {
  const layout = DEFAULT_TEMPLATE.layout;

  const { resource: interfaces } = useResource(
    'interfaces',
    useCallback((signal) => endpoints.interfaces({ signal }), []),
  );
  const { resource: findings } = useResource(
    'findings:open',
    useCallback((signal) => endpoints.findings({ status: 'open', limit: 50 }, { signal }), []),
  );
  const { resource: containers } = useResource(
    'containers',
    useCallback((signal) => endpoints.containers({ signal }), []),
  );
  const { resource: units } = useResource(
    'systemd-units',
    useCallback((signal) => endpoints.systemdUnits({ signal }), []),
  );

  const attention = combine(combine(interfaces, findings), combine(containers, units));

  return (
    <>
      <h1 className="visually-hidden">Overview</h1>

      {/* Only a *failed* read is unresolved. While it is still in flight nothing is claimed
          either way, so the rail stays out of the way rather than announcing a problem that
          may not exist. */}
      {attention.status === 'failed' ? (
        <AttentionRail drifted={[]} findings={[]} quietSummary={null} unresolved />
      ) : null}

      {attention.status === 'success'
        ? (() => {
            const [[interfaceList, findingList], [containerList, unitList]] = attention.data;

            const drifted: AttentionItem[] = interfaceList.interfaces
              .filter((item) => item.reconciliation === 'drifted')
              .map((item) => ({
                id: item.object_id,
                name: item.name,
                detail: 'drifted',
                to: `/network/${item.object_id}`,
              }));

            const findingItems: AttentionItem[] = findingList.findings.map((finding) => ({
              id: finding.finding_id,
              name: finding.object_name,
              detail: finding.finding_type,
            }));

            const observed =
              interfaceList.count + containerList.count + unitList.count;
            const managed = interfaceList.interfaces.filter(
              (item) => item.management.state === 'managed',
            ).length;

            return (
              <AttentionRail
                drifted={drifted}
                findings={findingItems}
                quietSummary={
                  <>
                    {observed.toLocaleString()} objects observed ·{' '}
                    {managed === 0
                      ? 'none managed, so nothing can drift'
                      : `${managed} managed, all in sync`}{' '}
                    · no open findings
                  </>
                }
              />
            );
          })()
        : null}

      <DashboardGrid layout={layout} />
    </>
  );
}
