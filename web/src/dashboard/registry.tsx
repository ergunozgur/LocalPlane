/**
 * The widget registry, and the default template.
 *
 * A widget is listed here only when a real backend endpoint backs it. The design direction
 * also carries `sessions`, `resources` (load/memory/disk/temperature history), `storage`
 * (filesystems) and `iface-traffic` (throughput over the last hour); this build has no
 * sessions API, no metrics time-series of any kind, no filesystem API, and `Statistics`
 * exposes cumulative counters with no history. They are omitted rather than stubbed — a
 * fabricated counter is exactly what the product must not do.
 */
import type { DashboardTemplate, WidgetDefinition, WidgetId } from './model';
import { DeviceOverviewWidget } from './widgets/DeviceOverviewWidget';
import { WorkloadsWidget } from './widgets/WorkloadsWidget';
import { ServicesWidget } from './widgets/ServicesWidget';
import { NetworkWidget } from './widgets/NetworkWidget';
import { ActivityWidget } from './widgets/ActivityWidget';
import { ChangesWidget } from './widgets/ChangesWidget';
import { ObservationWidget } from './widgets/ObservationWidget';

export const WIDGETS: ReadonlyMap<WidgetId, WidgetDefinition> = new Map(
  (
    [
      {
        id: 'device-overview',
        name: 'Device overview',
        description: 'the machine, and what is proven about it',
        defaultPlacement: { x: 0, y: 0, w: 8, h: 15 },
        minimum: { w: 4, h: 8 },
        focal: true,
        render: () => <DeviceOverviewWidget />,
      },
      {
        id: 'observation',
        name: 'Summary',
        description: 'identity, agent and observation state for this host',
        defaultPlacement: { x: 8, y: 0, w: 4, h: 15 },
        minimum: { w: 3, h: 6 },
        render: () => <ObservationWidget />,
      },
      {
        id: 'workloads',
        name: 'Workloads',
        description: 'the containers this host is running',
        defaultPlacement: { x: 0, y: 21, w: 7, h: 10 },
        minimum: { w: 4, h: 5 },
        render: () => <WorkloadsWidget />,
      },
      {
        id: 'network',
        name: 'Network',
        description: 'interfaces, their state and who configures them',
        defaultPlacement: { x: 7, y: 21, w: 5, h: 10 },
        minimum: { w: 3, h: 5 },
        render: () => <NetworkWidget />,
      },
      {
        id: 'services',
        name: 'Services',
        description: 'systemd units, as the manager reports them',
        defaultPlacement: { x: 0, y: 31, w: 7, h: 10 },
        minimum: { w: 4, h: 5 },
        render: () => <ServicesWidget />,
      },
      {
        id: 'activity',
        name: 'Recent runs',
        description: 'what has been planned from here, and how it ended',
        defaultPlacement: { x: 7, y: 31, w: 5, h: 5 },
        minimum: { w: 3, h: 4 },
        render: () => <ActivityWidget />,
      },
      {
        id: 'changes',
        name: 'Recent changes',
        description: 'entries in the record of crossing the write boundary',
        defaultPlacement: { x: 7, y: 36, w: 5, h: 5 },
        minimum: { w: 3, h: 4 },
        render: () => <ChangesWidget />,
      },
    ] satisfies WidgetDefinition[]
  ).map((definition) => [definition.id, definition]),
);

/**
 * The default arrangement.
 *
 * Sections are ordered as an operator reads a host: what it is, what wants attention, what
 * it is running, and what has been done to it. A future administrator-provided default or a
 * user's own template replaces this constant with a fetched one.
 */
export const DEFAULT_TEMPLATE: DashboardTemplate = {
  id: 'default',
  name: 'Default',
  description: 'The arrangement LocalPlane ships with.',
  layout: {
    id: 'default',
    name: 'Default',
    hidden: [],
    sections: [
      {
        id: 'machine',
        title: null,
        widgets: [
          { widget: 'device-overview', placement: { x: 0, y: 0, w: 8, h: 15 } },
          { widget: 'observation', placement: { x: 8, y: 0, w: 4, h: 15 } },
        ],
      },
      {
        id: 'estate',
        title: 'Estate',
        widgets: [
          { widget: 'workloads', placement: { x: 0, y: 0, w: 7, h: 10 } },
          { widget: 'network', placement: { x: 7, y: 0, w: 5, h: 10 } },
          // 304 units on this host: the widest table in the estate gets the full row.
          { widget: 'services', placement: { x: 0, y: 10, w: 12, h: 10 } },
        ],
      },
      {
        // The two operational ledgers read against each other: what was planned, and what
        // crossed the write boundary. Separating them across sections loses that.
        id: 'record',
        title: 'Record',
        widgets: [
          { widget: 'activity', placement: { x: 0, y: 0, w: 5, h: 8 } },
          { widget: 'changes', placement: { x: 5, y: 0, w: 7, h: 8 } },
        ],
      },
    ],
  },
};
