/**
 * Information density from the *container's* width, not the viewport's.
 *
 * A dashboard widget's width is set by the layout, not by the window: a widget four columns
 * wide is narrow on a 4K display. Reacting to the viewport would give that widget the
 * expanded treatment and let it overflow. Reacting to its own box gives the right answer at
 * every window size, and keeps working when dashboard customisation later lets an operator
 * resize it.
 *
 * Density is about *which facts are shown*, never about type scale. Nothing that reads this
 * hook shrinks text; it drops secondary fields into a disclosure and keeps the primary ones.
 */
import { useEffect, useRef, useState } from 'react';

export type DensityMode = 'expanded' | 'compact' | 'minimal';

export const DENSITY_BREAKPOINTS = { expanded: 900, compact: 560 } as const;

export function densityForWidth(width: number): DensityMode {
  if (width >= DENSITY_BREAKPOINTS.expanded) return 'expanded';
  if (width >= DENSITY_BREAKPOINTS.compact) return 'compact';
  return 'minimal';
}

/**
 * Observe an element and report its density mode.
 *
 * Starts at `expanded` rather than `minimal` so that a server-rendered or
 * pre-measurement paint shows the full content and then reduces, instead of flashing a
 * stripped-down card and expanding. `ResizeObserver` is absent in some test environments;
 * the hook degrades to its initial mode rather than throwing.
 */
export function useContainerDensity<T extends HTMLElement>(
  initial: DensityMode = 'expanded',
): [React.RefObject<T>, DensityMode, number] {
  const ref = useRef<T>(null);
  const [mode, setMode] = useState<DensityMode>(initial);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const element = ref.current;
    if (!element || typeof ResizeObserver === 'undefined') return;

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const observed = entry.contentRect.width;
      if (observed > 0) {
        setMode(densityForWidth(observed));
        setWidth(observed);
      }
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  return [ref, mode, width];
}
