/// <reference types="vite/client" />

/**
 * CSS modules.
 *
 * Typed as a permissive record rather than generated per-file: generating exact class names
 * would add a build step and a watcher for a guarantee that a missing class renders
 * unstyled, which is visible immediately. Not worth the machinery at this size.
 */
declare module '*.module.css' {
  const classes: Readonly<Record<string, string>>;
  export default classes;
}
