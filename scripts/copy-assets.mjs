// schema.sql and the skill ship beside the compiled JS: they are data, not
// code, and tsc only emits what it compiles. The skill's canonical home is
// `.claude/skills/` so that it also applies inside this checkout; the copy
// under `skills/` is what npm packs and what `telemetry init` installs.
import { copyFileSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(dirname(fileURLToPath(import.meta.url)))

mkdirSync(join(root, 'dist'), { recursive: true })
copyFileSync(join(root, 'src', 'schema.sql'), join(root, 'dist', 'schema.sql'))

mkdirSync(join(root, 'skills', 'telemetry'), { recursive: true })
copyFileSync(
  join(root, '.claude', 'skills', 'telemetry', 'SKILL.md'),
  join(root, 'skills', 'telemetry', 'SKILL.md'),
)
console.log('copied schema.sql and SKILL.md into the package')
