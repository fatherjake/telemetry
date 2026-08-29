import js from '@eslint/js'
import globals from 'globals'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

// One project, one config. The TypeScript under src/ is type-linted against the
// real tsconfig; the loose JavaScript at the edges - the bin shim, the build
// script, the tests - gets the untyped recommended set, because none of it is
// covered by a tsconfig and pretending otherwise would mean linting it against
// types that do not exist.

// Three typed rules, nothing else. A dropped `await` here is not a style
// finding: `analyse` and `doctor` report their results after the work is
// supposed to be done, so an unawaited promise means the CLI prints a summary
// of a run that has not finished, and exits. That is the whole reason ESLint is
// on this project.
//
// The full `recommendedTypeChecked` set is deliberately NOT enabled: it reports
// hundreds of stylistic findings on a codebase that is already clean, and a
// gate nobody can get green is a gate nobody runs. Add a rule when it catches a
// bug we actually shipped.
const asyncCorrectness = {
  '@typescript-eslint/await-thenable': 'error',
  '@typescript-eslint/no-floating-promises': 'error',
  '@typescript-eslint/no-misused-promises': 'error',
}

export default defineConfig([
  globalIgnores([
    '**/node_modules/**',
    // Compiled output, and the copy of the skill the build makes for packaging.
    'dist/**',
    'skills/**',
  ]),

  // --- src: the TypeScript, type-linted ---------------------------------------
  {
    files: ['src/**/*.ts'],
    extends: [js.configs.recommended, tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2023,
      globals: globals.node,
      parserOptions: {
        project: ['./tsconfig.json'],
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      ...asyncCorrectness,
      // A leading underscore is this codebase's existing marker for "part of
      // the signature, deliberately unused" - handler arguments that exist to
      // match a shape, mostly.
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      // Rows come back from SQLite as whatever the column holds, and OTLP
      // payloads are arbitrary JSON. Both are genuinely unknown shapes that get
      // narrowed at the point of use; `any` at those boundaries is the honest
      // type, and the alternative is a cast on every field access.
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },

  // --- the JavaScript at the edges --------------------------------------------
  {
    files: ['bin/**/*.js', 'scripts/**/*.mjs', 'test/**/*.js'],
    extends: [js.configs.recommended],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: 'module',
      globals: globals.node,
    },
  },
])
