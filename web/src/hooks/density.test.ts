/**
 * Density modes.
 *
 * Thresholds are asserted because the component contract depends on them: safety state is
 * rendered in every mode, and only secondary fields move as the container narrows.
 */
import { describe, expect, it } from 'vitest';
import { densityForWidth, DENSITY_BREAKPOINTS } from './useContainerDensity';

describe('densityForWidth', () => {
  it('is expanded at and above the wide threshold', () => {
    expect(densityForWidth(DENSITY_BREAKPOINTS.expanded)).toBe('expanded');
    expect(densityForWidth(1600)).toBe('expanded');
  });

  it('is compact between the thresholds', () => {
    expect(densityForWidth(DENSITY_BREAKPOINTS.compact)).toBe('compact');
    expect(densityForWidth(DENSITY_BREAKPOINTS.expanded - 1)).toBe('compact');
  });

  it('is minimal below the narrow threshold', () => {
    expect(densityForWidth(DENSITY_BREAKPOINTS.compact - 1)).toBe('minimal');
    expect(densityForWidth(0)).toBe('minimal');
  });

  it('reacts to the container, not the viewport', () => {
    // A four-column widget on a 4K display is narrow. The mode follows the box it is in.
    expect(densityForWidth(420)).toBe('minimal');
  });
});
