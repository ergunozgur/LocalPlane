/**
 * The dashboard model.
 *
 * The property that matters is negative: a layout must be incapable of influencing what any
 * fact means. It can say which widgets appear and where, and nothing else.
 */
import { describe, expect, it } from 'vitest';
import { DEFAULT_TEMPLATE, WIDGETS } from './registry';
import { visibleWidgets, type DashboardLayout } from './model';

describe('the default template', () => {
  it('names only widgets the registry defines', () => {
    for (const section of DEFAULT_TEMPLATE.layout.sections) {
      for (const entry of section.widgets) {
        expect(WIDGETS.has(entry.widget)).toBe(true);
      }
    }
  });

  it('places every widget inside the twelve-column grid', () => {
    for (const section of DEFAULT_TEMPLATE.layout.sections) {
      for (const entry of section.widgets) {
        expect(entry.placement.w).toBeGreaterThan(0);
        expect(entry.placement.w).toBeLessThanOrEqual(12);
        expect(entry.placement.x + entry.placement.w).toBeLessThanOrEqual(12);
      }
    }
  });

  it('never places a widget below its declared minimum', () => {
    for (const section of DEFAULT_TEMPLATE.layout.sections) {
      for (const entry of section.widgets) {
        const definition = WIDGETS.get(entry.widget);
        expect(definition).toBeDefined();
        expect(entry.placement.w).toBeGreaterThanOrEqual(definition!.minimum.w);
      }
    }
  });

  it('shows each widget at most once', () => {
    const seen = DEFAULT_TEMPLATE.layout.sections.flatMap((s) => s.widgets.map((w) => w.widget));
    expect(new Set(seen).size).toBe(seen.length);
  });
});

describe('layout configuration cannot carry domain meaning', () => {
  it('exposes only placement and identity on a widget entry', () => {
    const entry = DEFAULT_TEMPLATE.layout.sections[0]?.widgets[0];
    expect(entry).toBeDefined();
    expect(Object.keys(entry!).sort()).toEqual(['placement', 'widget']);
    expect(Object.keys(entry!.placement).sort()).toEqual(['h', 'w', 'x', 'y']);
  });

  it('hides a widget without altering any other widget', () => {
    const hidden: DashboardLayout = { ...DEFAULT_TEMPLATE.layout, hidden: ['services'] };
    for (const section of hidden.sections) {
      const shown = visibleWidgets(section, hidden, WIDGETS);
      expect(shown.some((w) => w.widget === 'services')).toBe(false);
      // Everything else is untouched: hiding removes a panel, it does not reinterpret one.
      const expected = section.widgets.filter((w) => w.widget !== 'services');
      expect(shown).toEqual(expected);
    }
  });

  it('drops a widget the registry does not define rather than rendering a blank', () => {
    const layout: DashboardLayout = {
      id: 'x',
      name: 'x',
      hidden: [],
      sections: [
        {
          id: 's',
          title: null,
          widgets: [
            { widget: 'device-overview', placement: { x: 0, y: 0, w: 12, h: 8 } },
            // A layout written by a newer build, naming a widget this one lacks.
            { widget: 'not-a-real-widget' as never, placement: { x: 0, y: 8, w: 12, h: 8 } },
          ],
        },
      ],
    };
    const shown = visibleWidgets(layout.sections[0]!, layout, WIDGETS);
    expect(shown).toHaveLength(1);
    expect(shown[0]?.widget).toBe('device-overview');
  });
});

describe('the registry', () => {
  it('gives every widget a name and a description for a future picker', () => {
    for (const [id, definition] of WIDGETS) {
      expect(definition.id).toBe(id);
      expect(definition.name.length).toBeGreaterThan(0);
      expect(definition.description.length).toBeGreaterThan(0);
    }
  });

  it('marks exactly one widget as focal', () => {
    expect([...WIDGETS.values()].filter((d) => d.focal)).toHaveLength(1);
  });
});
