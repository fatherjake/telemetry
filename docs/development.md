# Development

```bash
npm install     # installs dependencies and builds
npm test        # build, then run the suite
npm run lint    # eslint
npm run format  # prettier --write .
npm run check   # tsc --noEmit
```

There are no runtime dependencies. Everything in `devDependencies` is
TypeScript, ESLint, Prettier and the git hooks.

---

## Tests

Twenty-five of them. Thirteen are on `src/redact.ts` — the one component with
no off switch, where a mistake is silent and permanent; every "must not redact"
case was found in real telemetry rather than invented. The rest cover the
places where a wrong answer would be silent rather than loud: posix word
splitting, path normalisation, glob matching, and how a number is written back
out as JSON.

`telemetry doctor` covers the pipeline end to end — receiver, raw file,
database, redaction, MCP. These cover the things doctor cannot, which are what
redaction does to content that merely _looks_ like a credential, and what the
command parser does to a command line.

---

## Linting and formatting

Prettier settings are two lines — `singleQuote`, `semi: false` — and everything
else is the default, including the 80-column width. There is nothing to argue
about and no local overrides.

ESLint is flat config, and deliberately thin. The TypeScript under `src/` gets
the recommended sets plus exactly three typed rules — `await-thenable`,
`no-floating-promises`, `no-misused-promises`. Those three are not style: this
CLI reports what a command did _after_ doing it, so a dropped `await` means
`analyse` prints a summary of a run that has not finished and then exits. The
full `recommendedTypeChecked` set is off on purpose — it reports hundreds of
stylistic findings on a clean codebase, and a gate nobody can get green is a
gate nobody runs. Add a rule when it catches a bug that actually shipped.

Two rules are turned off, both with a reason in the config: `no-explicit-any`,
because SQLite rows and OTLP payloads are genuinely unknown shapes narrowed at
the point of use, and the unused-variable rule is relaxed for a leading `_`,
which is this codebase's existing marker for an argument that exists to match a
signature.

A husky `pre-commit` hook runs `lint-staged`, which runs `prettier --check` on
the staged files — check, not write, so a commit fails rather than silently
rewriting what you are about to commit.

---

## Build output

`tsc` writes new files into `dist/` but never removes orphaned ones, so a
deleted source module leaves its compiled output behind and it keeps getting
packaged. After deleting or renaming modules, `rm -rf dist` before rebuilding.
