// @ts-check
/**
 * Lint configuration.
 *
 * Deliberately small. TypeScript's strict mode already carries most of the weight here, so
 * these rules cover what the compiler cannot see: hook correctness, and a handful of
 * patterns that would quietly undo the safety properties this frontend is built around.
 */
import js from '@eslint/js';
import globals from 'globals';
import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';

export default tseslint.config(
  { ignores: ['dist', 'node_modules', 'src/api/schema.d.ts', 'coverage'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommendedTypeChecked],
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      globals: { ...globals.browser, ...globals.node },
      parserOptions: {
        project: ['./tsconfig.app.json', './tsconfig.node.json'],
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],

      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      // Domain values arrive as `unknown` from JSON and are narrowed deliberately; the
      // blanket ban is noise here, but an accidental `any` is not.
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/no-unnecessary-condition': 'off',

      // `value ?? fallback` on a domain field is how a missing fact silently becomes a
      // present one. It is not banned outright — it is legitimate for display strings — but
      // `||` on a possibly-null number would turn a real 0 into a fallback, so that is.
      'no-restricted-syntax': [
        'error',
        {
          selector: 'BinaryExpression[operator="=="]',
          message: 'Use === so that null and undefined stay distinguishable.',
        },
      ],
    },
  },
  {
    // A context provider and its hook belong together; splitting them to satisfy fast
    // refresh would scatter the seam this architecture depends on being obvious.
    files: [
      'src/preferences/preferences.tsx',
      'src/identity/viewer.tsx',
      'src/dashboard/registry.tsx',
    ],
    rules: { 'react-refresh/only-export-components': 'off' },
  },
  {
    // Tests deliberately construct partial fixtures and assert on loose shapes.
    files: ['**/*.test.{ts,tsx}', 'src/test/**'],
    rules: {
      '@typescript-eslint/no-unsafe-assignment': 'off',
      '@typescript-eslint/no-unsafe-member-access': 'off',
      '@typescript-eslint/no-unsafe-argument': 'off',
      '@typescript-eslint/no-non-null-assertion': 'off',
      // A fetch double must return a promise; it has nothing to await.
      '@typescript-eslint/require-await': 'off',
    },
  },
);
