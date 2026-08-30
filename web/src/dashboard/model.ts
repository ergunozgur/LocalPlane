/**
 * The dashboard as typed, ordered configuration.
 *
 * This model is carried over from the design direction rather than reinvented. The
 * dashboard runs a twelve-column grid on a 28 px row with a 14 px gap, gives every
 * widget a default placement and a minimum size, marks section headings as `band` and the
 * device overview as `focal`, keeps a hidden list, and carries the arrangement as typed
 * configuration. Those are the concepts below.
 *
 * **What this build does:** the types, the registry and the renderer, so the Overview page
 * contains no widget-specific markup and a widget can be added, reordered, resized or hidden
 * as data.
 *
 * **What this build does not do:** drag, resize, the widget picker and layout
 * persistence. The design direction has all four, and they are the editor, not the
 * foundation. They attach to this model without changing it.
 *
 * **The safety constraint is structural, not a convention.** A `DashboardLayout` can express
 * which widgets appear, in what order and at what size — and there is deliberately no field
 * on any type here through which it could express anything else. Every widget reads the same
 * typed endpoints and maps them through the same `domain/vocabulary`, so a layout can remove
 * a fact from a page and can never change what a fact means. No client-side configuration is
 * capable of turning an `unknown` into a `clear`.
 */
import type { ReactNode } from 'react';

/** The dashboard's grid constants. */
export const GRID_COLUMNS = 12;
export const GRID_ROW_HEIGHT = 28;
export const GRID_GAP = 14;

export interface WidgetPlacement {
  /** Column origin, 0-based, in a twelve-column grid. */
  x: number;
  /** Row origin, in `GRID_ROW_HEIGHT` units. */
  y: number;
  w: number;
  h: number;
}

export interface WidgetSize {
  w: number;
  h: number;
}

export type WidgetId =
  | 'device-overview'
  | 'workloads'
  | 'services'
  | 'network'
  | 'activity'
  | 'changes'
  | 'observation';

export interface WidgetDefinition {
  readonly id: WidgetId;
  readonly name: string;
  /** Shown in a future widget picker, and as the accessible description here. */
  readonly description: string;
  readonly defaultPlacement: WidgetPlacement;
  readonly minimum: WidgetSize;
  /** A full-width section heading rather than a plate. */
  readonly band?: boolean;
  /** The widget a layout is built around. The device overview is marked this way. */
  readonly focal?: boolean;
  readonly render: () => ReactNode;
}

export interface DashboardWidget {
  readonly widget: WidgetId;
  readonly placement: WidgetPlacement;
}

export interface DashboardSection {
  readonly id: string;
  /** Rendered as a band heading. `null` for a section that needs no title. */
  readonly title: string | null;
  readonly widgets: readonly DashboardWidget[];
}

export interface DashboardLayout {
  readonly id: string;
  readonly name: string;
  readonly sections: readonly DashboardSection[];
  readonly hidden: readonly WidgetId[];
}

export interface DashboardTemplate {
  readonly id: string;
  readonly name: string;
  readonly description: string;
  readonly layout: DashboardLayout;
}

/** Widgets a layout names but the registry does not define are dropped, not rendered blank. */
export function visibleWidgets(
  section: DashboardSection,
  layout: DashboardLayout,
  registry: ReadonlyMap<WidgetId, WidgetDefinition>,
): readonly DashboardWidget[] {
  return section.widgets.filter(
    (entry) => !layout.hidden.includes(entry.widget) && registry.has(entry.widget),
  );
}
