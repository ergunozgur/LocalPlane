// `defineConfig` comes from vitest/config so that the `test` block below is typed. It is a
// re-export of Vite's own, so this changes nothing about the build.
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';

// The backend registers no CORS middleware, so a cross-origin dev server cannot reach it.
// Every /api request is proxied to the backend's own origin instead. LOCALPLANE_API_ORIGIN
// overrides the target; nothing else about the request is rewritten.
const apiOrigin = process.env.LOCALPLANE_API_ORIGIN ?? 'http://127.0.0.1:8080';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5178,
    proxy: {
      '/api': { target: apiOrigin, changeOrigin: false },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    // Fonts are inlined by default under 4 KB; these are 30-100 KB and must stay as files
    // so the browser can cache them across builds.
    assetsInlineLimit: 4096,
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
