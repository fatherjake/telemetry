/** Paths and runtime configuration.
 *
 * Nothing here reaches the network: collection, normalization and every query
 * run entirely on this machine.
 *
 * Everything lives under ~/.telemetry. The install holds code; the home
 * directory holds data. That way the database survives reinstalling the
 * package, one install serves every project, and nothing sensitive is ever
 * sitting inside a repository that might get committed.
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const MODULE_DIR = dirname(fileURLToPath(import.meta.url))

/** The installed package root. */
export const ROOT = dirname(MODULE_DIR)

/** schema.sql ships beside the compiled JS. */
export const SCHEMA_PATH = join(MODULE_DIR, 'schema.sql')

/** The skill that teaches Claude Code which tool answers which question.
 *
 * Packed under `skills/`; in a checkout it lives under `.claude/skills/` so it
 * also applies to work done in this repository. Whichever exists is the one
 * `telemetry init` installs.
 */
export function skillSource(): string | null {
  for (const candidate of [
    join(ROOT, 'skills', 'telemetry', 'SKILL.md'),
    join(ROOT, '.claude', 'skills', 'telemetry', 'SKILL.md'),
  ]) {
    if (existsSync(candidate)) return candidate
  }
  return null
}

/** Where it gets installed, so every project on this machine sees it. */
export const SKILL_TARGET = join(
  homedir(),
  '.claude',
  'skills',
  'telemetry',
  'SKILL.md',
)

/** The one knob: point TELEMETRY_HOME somewhere else and every path below
 * follows it. */
export const HOME = process.env.TELEMETRY_HOME || join(homedir(), '.telemetry')

export const RAW_DIR = join(HOME, 'raw')
export const DB_PATH = process.env.TELEMETRY_DB || join(HOME, 'telemetry.db')
export const LOG_FILE = join(HOME, 'receiver.log')
export const PID_FILE = join(HOME, 'receiver.pid')
export const ANALYSE_LOCK = join(HOME, 'analyse.lock')

/** Locally collected session context (not OTLP). Append-only, and read back
 * by `analyse`, so the database stays fully rebuildable from files on disk. */
export const SESSION_CONTEXT_FILE = join(HOME, 'session_context.jsonl')

/** The receiver listens on loopback only, by design. */
export const OTLP_PORT = Number.parseInt(
  process.env.TELEMETRY_OTLP_PORT || '4318',
  10,
)

/** The name Claude Code registers this server under; it also prefixes every
 * tool the session sees, as mcp__<name>__telemetry_*. */
export const MCP_SERVER_NAME = 'telemetry'

/** Glob patterns for paths that should never count as file activity. Agent
 * scratchpads and dependency trees are churn, not work on the project. */
export const IGNORE_FILE = join(HOME, 'ignore')

export const DEFAULT_IGNORES = [
  '/tmp/*',
  '/private/tmp/*',
  '*/node_modules/*',
  '*/.git/*',
  '*/.venv/*',
  '*/__pycache__/*',
  '*/.next/*',
  '*/dist/*',
  '*/.DS_Store',
]

export function ensureDirs(): void {
  for (const dir of [HOME, RAW_DIR]) mkdirSync(dir, { recursive: true })
}

/** How another program should launch this CLI.
 *
 * An absolute node binary plus an absolute script path, because the two
 * callers that need this - Claude Code's hooks and its MCP registration -
 * bake the answer into a config file and run it with a PATH we do not
 * control. TELEMETRY_LAUNCHER overrides it for installs that put a stable
 * shim somewhere else.
 */
/** Is this program running out of a cache npm is free to delete?
 *
 * `npx` resolves into `~/.npm/_npx/<hash>/`, which is version-keyed and
 * disposable. Fine for a one-shot command, wrong for the two places that write
 * this path into a config file and expect it to work for months.
 */
export function isEphemeralInstall(): boolean {
  return /[\\/]_npx[\\/]/.test(ROOT)
}

export function launcherCommand(): string[] {
  const override = process.env.TELEMETRY_LAUNCHER
  if (override) return [override]
  // Invoked as an argument to node, the shebang's warning flag does not apply,
  // and node:sqlite's ExperimentalWarning would land on every hook run and in
  // every MCP session's stderr.
  return [
    process.execPath,
    '--disable-warning=ExperimentalWarning',
    join(ROOT, 'bin', 'telemetry.js'),
  ]
}

// --- ignore patterns --------------------------------------------------------

export function loadIgnores(): string[] {
  try {
    if (existsSync(IGNORE_FILE)) {
      const lines: string[] = []
      for (const raw of readFileSync(IGNORE_FILE, 'utf8').split('\n')) {
        const line = raw.split('#', 1)[0]!.trim()
        if (line) lines.push(line)
      }
      return lines
    }
  } catch {
    /* unreadable ignore file: fall through to the defaults */
  }
  return [...DEFAULT_IGNORES]
}

/** Test a path against the ignore patterns.
 *
 * Not `path.matchesGlob`: these are matched against whole paths, so a star has
 * to cross `/` - `/tmp` excludes everything below /tmp - and has to match a
 * leading dot, so the node_modules pattern also excludes node_modules/.bin.
 * `matchesGlob` does neither. Only `*` and `?` are supported, which is all an
 * ignore file has ever used.
 */
export function ignoreFilter(): (path: string) => boolean {
  const patterns = loadIgnores().map((p) => {
    const body = p.replace(/[.*+?^${}()|[\]\\]/g, (ch) =>
      ch === '*' ? '.*' : ch === '?' ? '.' : '\\' + ch,
    )
    return new RegExp(`^${body}$`, 's')
  })
  return (path: string) => patterns.some((re) => re.test(path))
}

export function saveIgnores(patterns: string[]): void {
  ensureDirs()
  const header =
    '# Paths matching these globs are excluded from file activity.\n' +
    '# One glob per line; # starts a comment. Applied on every `telemetry analyse`.\n'
  writeFileSync(IGNORE_FILE, header + patterns.join('\n') + '\n')
}

// --- the one remaining switch ------------------------------------------------

/** Run read-only git commands against detected repositories for
 * reconciliation. On by default; off is for the rare machine where shelling
 * out to git is unwelcome, at the cost of project mapping and commits. */
export const GIT_RECONCILE = !['0', 'false', 'no', 'off'].includes(
  (process.env.TELEMETRY_GIT_RECONCILE ?? '').trim().toLowerCase(),
)

// --- what Claude Code is told to export -------------------------------------

/** The env block that points Claude Code at the local receiver.
 *
 * Written straight into ~/.claude/settings.json by `telemetry install`. There
 * is no generated shell file and nothing to `source`: settings.json covers
 * every session however it was launched.
 */
export function otelEnv(): Record<string, string> {
  const env: Record<string, string> = {
    CLAUDE_CODE_ENABLE_TELEMETRY: '1',
    OTEL_METRICS_EXPORTER: 'otlp',
    OTEL_LOGS_EXPORTER: 'otlp',
    OTEL_TRACES_EXPORTER: 'otlp',
    // Tracing is a beta feature. It is what supplies per-tool spans carrying
    // file_path, full_command, skill_name and subagent_type.
    CLAUDE_CODE_ENHANCED_TELEMETRY_BETA: '1',
    // http/json is what the built-in receiver speaks.
    OTEL_EXPORTER_OTLP_PROTOCOL: 'http/json',
    OTEL_EXPORTER_OTLP_ENDPOINT: `http://localhost:${OTLP_PORT}`,
    // Defaults are 60s for metrics and 5s for logs; shorter intervals make
    // `telemetry status` feel responsive during a live session.
    OTEL_METRIC_EXPORT_INTERVAL: '10000',
    OTEL_LOGS_EXPORT_INTERVAL: '5000',
    OTEL_TRACES_EXPORT_INTERVAL: '5000',
    // Without this Claude Code redacts tool parameters, and you lose file
    // paths, bash commands, skill names and subagent types entirely.
    OTEL_LOG_TOOL_DETAILS: '1',
    // Prompts, responses, tool bodies and full API request JSON. Without
    // these the database can say a session cost $20 but not what it was
    // asked to do, which is most of the value - "which skill should have
    // fired" is a question about the words, not the counts.
    OTEL_LOG_USER_PROMPTS: '1',
    OTEL_LOG_ASSISTANT_RESPONSES: '1',
    OTEL_LOG_TOOL_CONTENT: '1',
    OTEL_LOG_RAW_API_BODIES: '1',
    CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH: '262144',
  }
  return env
}
