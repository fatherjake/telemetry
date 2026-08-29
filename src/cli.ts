/** `telemetry` command line. */
import { spawn, spawnSync } from 'node:child_process'
import {
  closeSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { createInterface } from 'node:readline/promises'
import { parseArgs } from 'node:util'
import { randomUUID } from 'node:crypto'
import * as config from './config.js'
import * as db from './db.js'
import * as gitctx from './gitctx.js'
import * as ingest from './ingest.js'
import { pendingBytes } from './freshen.js'
import * as Q from './queries.js'
import { dumps, group, humanBytes, utcNow } from './util/text.js'

const BOLD = '\u001b[1m'
const DIM = '\u001b[2m'
const RESET = '\u001b[0m'
const GREEN = '\u001b[32m'
const RED = '\u001b[31m'
const YELLOW = '\u001b[33m'

function c(text: unknown, colour: string): string {
  return process.stdout.isTTY ? `${colour}${text}${RESET}` : String(text)
}

const out = (line = '') => process.stdout.write(line + '\n')

// ------------------------------------------------------------------ helpers --

export async function healthOk(timeoutMs = 3000): Promise<boolean> {
  try {
    const res = await fetch(`http://127.0.0.1:${config.OTLP_PORT}/health`, {
      signal: AbortSignal.timeout(timeoutMs),
    })
    return res.status === 200
  } catch {
    return false
  }
}

/** The receiver's pid, or null. A stale pidfile is cleaned up in passing. */
export function receiverRunning(): number | null {
  if (!existsSync(config.PID_FILE)) return null
  try {
    const pid = Number.parseInt(
      readFileSync(config.PID_FILE, 'utf8').trim(),
      10,
    )
    if (!Number.isFinite(pid)) throw new Error('bad pidfile')
    process.kill(pid, 0)
    return pid
  } catch {
    try {
      unlinkSync(config.PID_FILE)
    } catch {
      /* already gone */
    }
    return null
  }
}

export function rawStats(): {
  files: number
  bytes: number
  newest_mtime: number
} {
  const files = ingest.scanRawFiles()
  let bytes = 0
  let newest = 0
  for (const [path] of files) {
    try {
      const st = statSync(path)
      bytes += st.size
      newest = Math.max(newest, st.mtimeMs / 1000)
    } catch {
      /* rotated away between the scan and the stat */
    }
  }
  return { files: files.length, bytes, newest_mtime: newest }
}

// ----------------------------------------------------------------- commands --

export async function cmdStart(): Promise<number> {
  config.ensureDirs()
  if (receiverRunning()) {
    out('receiver already running')
    return 0
  }
  const logFd = openSync(config.LOG_FILE, 'a')
  const child = spawn(
    process.execPath,
    [
      join(config.ROOT, 'bin', 'telemetry.js'),
      'receive',
      String(config.OTLP_PORT),
    ],
    { detached: true, stdio: ['ignore', logFd, logFd] },
  )
  child.unref()
  closeSync(logFd)
  writeFileSync(config.PID_FILE, String(child.pid))
  await new Promise((r) => setTimeout(r, 1000))
  const ok = await healthOk()
  out(
    `${c(ok ? 'started' : 'failed to start', ok ? GREEN : RED)} ` +
      `OTLP receiver on http://127.0.0.1:${config.OTLP_PORT}`,
  )
  out(`  raw data   ${config.RAW_DIR}`)
  out(`  log        ${config.LOG_FILE}`)
  if (!ok)
    out(`${DIM}check the log; something else may be holding the port${RESET}`)
  return ok ? 0 : 1
}

export async function cmdStop(): Promise<number> {
  const pid = receiverRunning()
  if (!pid) {
    out('nothing running')
    return 0
  }
  try {
    process.kill(pid, 'SIGTERM')
  } catch (e) {
    out(c(`could not stop receiver: ${(e as Error).message}`, RED))
    return 1
  }
  // Wait for it to actually go. SIGTERM starts a graceful shutdown, and until
  // that finishes the port is still bound - so returning early makes the
  // obvious `telemetry stop && telemetry start` fail on EADDRINUSE.
  for (let i = 0; i < 100; i++) {
    try {
      process.kill(pid, 0)
    } catch {
      break
    }
    await new Promise((r) => setTimeout(r, 100))
  }
  try {
    unlinkSync(config.PID_FILE)
  } catch {
    /* already gone */
  }
  out('stopped receiver')
  return 0
}

export async function cmdStatus(): Promise<number> {
  const pid = receiverRunning()

  out(`${BOLD}Receiver${RESET}`)
  if (pid) out(`  process        ${c('running', GREEN)} (pid ${pid})`)
  else
    out(
      `  process        ${c('not running', RED)}  ${DIM}telemetry start${RESET}`,
    )
  out(
    `  OTLP ${config.OTLP_PORT}     ` +
      `${(await healthOk()) ? c('open', GREEN) : c('closed', RED)}`,
  )

  const rs = rawStats()
  out(`\n${BOLD}Raw telemetry${RESET}  ${config.RAW_DIR}`)
  out(`  files          ${rs.files}`)
  out(`  size           ${humanBytes(rs.bytes)}`)
  if (rs.newest_mtime) {
    const age = Date.now() / 1000 - rs.newest_mtime
    const state =
      age < 300
        ? c('events arriving', GREEN)
        : c('no new events recently', YELLOW)
    const d = new Date(rs.newest_mtime * 1000)
    const p = (n: number) => String(n).padStart(2, '0')
    const last =
      `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
      `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
    out(
      `  last write     ${last}  (${(age / 60).toFixed(1)} min ago)  ${state}`,
    )
  } else {
    out(`  last write     ${c('never - no telemetry received yet', YELLOW)}`)
  }

  if (!existsSync(config.DB_PATH)) {
    out(`\n${BOLD}Database${RESET}\n  not created yet - run: telemetry analyse`)
  } else {
    const conn = db.connect()
    const o = Q.overview(conn)
    const pending = pendingBytes()
    out(`\n${BOLD}Database${RESET}  ${config.DB_PATH}`)
    out(`  sessions       ${o.sessions}`)
    out(
      `  events         ${group(o.events as number)}   metrics ${group(o.metric_points as number)}` +
        `   spans ${group(o.spans as number)}`,
    )
    out(
      `  api calls      ${group(o.api_calls as number)}   cost $${(o.cost_usd as number).toFixed(4)}`,
    )
    out(
      `  tool calls     ${group(o.tool_calls as number)}   files touched ` +
        `${group((o.distinct_files_read as number) + (o.distinct_files_changed as number))}`,
    )
    out(`  last analysed  ${db.getMeta(conn, 'last_analyse_at') || 'never'}`)
    if (pending > 0) {
      out(
        `  ${c(`${humanBytes(pending)} of raw telemetry not yet analysed - run telemetry analyse`, YELLOW)}`,
      )
    }
    conn.close()
  }

  out(`\n${BOLD}Storage${RESET}`)
  out(
    `  ${c('full fidelity', GREEN)} - prompts, responses, tool arguments and API bodies`,
  )
  out(
    `  ${DIM}credentials are scrubbed on the way in; everything else is kept. ` +
      `${config.HOME} holds source code and conversation text.${RESET}`,
  )
  const gb = rs.bytes / 1024 ** 3
  if (gb >= 1)
    out(
      `  ${c(`raw archive is ${gb.toFixed(1)} GB`, YELLOW)} - see README on pruning`,
    )

  const installed = installedEnv()
  out(`\n${BOLD}Claude Code${RESET}`)
  if (Object.keys(installed).length) {
    out(
      `  telemetry env  ${c('installed', GREEN)} ` +
        `(${Object.keys(installed).length} variable(s) in settings.json)`,
    )
  } else {
    out(
      `  telemetry env  ${c('not installed', YELLOW)}  ${DIM}telemetry install${RESET}`,
    )
  }
  return 0
}

export function cmdAnalyse(): number {
  const conn = db.connect()
  const t0 = Date.now()
  const res = ingest.analyse(conn, undefined, (msg) =>
    out(`${DIM}${msg}${RESET}`),
  )
  out(
    `scanned ${res.files} raw file(s) in ${((Date.now() - t0) / 1000).toFixed(1)}s`,
  )
  out(`  log records   ${group(res.logs)}`)
  out(`  metric points ${group(res.metrics)}`)
  out(`  spans         ${group(res.traces)}`)
  if (res.skipped) {
    out(
      c(
        `  skipped       ${res.skipped} (incomplete or unparseable lines)`,
        YELLOW,
      ),
    )
  }
  const o = Q.overview(conn)
  out(
    `\ntotals: ${o.sessions} sessions, ${o.api_calls} api calls, ` +
      `$${(o.cost_usd as number).toFixed(4)}, ${o.tool_calls} tool calls, ` +
      `${o.files_read} reads, ${o.files_changed} writes/edits, ${o.bash_commands} commands`,
  )
  conn.close()
  return 0
}

function printTable(headers: string[], rows: unknown[][]): void {
  if (!rows.length) {
    out(`${DIM}(no rows)${RESET}`)
    return
  }
  const widths = headers.map((h) => h.length)
  const srows = rows.map((r) =>
    r.map((cell) => (cell === null || cell === undefined ? '' : String(cell))),
  )
  for (const r of srows) {
    r.forEach((cell, i) => {
      widths[i] = Math.max(widths[i] ?? 0, cell.length)
    })
  }
  const line = headers.map((h, i) => h.padEnd(widths[i]!)).join('  ')
  out(process.stdout.isTTY ? BOLD + line + RESET : line)
  const rule = widths.map((w) => '-'.repeat(w)).join('  ')
  out(process.stdout.isTTY ? DIM + rule + RESET : rule)
  for (const r of srows)
    out(r.map((cell, i) => cell.padEnd(widths[i]!)).join('  '))
}

/** Record local git context for a session. Reads Claude Code hook JSON on stdin. */
export function cmdSessionHook(args: Parsed): number {
  let payload: Record<string, any> = {}
  try {
    if (!process.stdin.isTTY) {
      const raw = readFileSync(0, 'utf8')
      if (raw.trim()) payload = JSON.parse(raw)
    }
  } catch {
    payload = {}
  }

  const sessionId = (args.values['session-id'] as string) || payload.session_id
  const cwd = (args.values.cwd as string) || payload.cwd || process.cwd()
  if (!sessionId) {
    // Nothing useful to record; never fail the user's session over telemetry.
    return 0
  }

  const desc = gitctx.describeFull(cwd)
  // Append to a plain file rather than writing SQLite. This keeps the hook fast
  // (it runs on every session start), avoids any chance of lock contention with
  // a concurrent `analyse`, and - importantly - keeps this context on disk so
  // the database remains rebuildable from files alone.
  const record = {
    session_id: sessionId,
    phase: (args.values.phase as string) || 'start',
    captured_at: utcNow(),
    cwd,
    repo_root: desc.repo_root ?? null,
    remote_url: desc.remote_url ?? null,
    branch: desc.branch ?? null,
    head_sha: desc.head_sha ?? null,
    is_dirty: desc.is_dirty ?? null,
  }
  mkdirSync(dirname(config.SESSION_CONTEXT_FILE), { recursive: true })
  const fd = openSync(config.SESSION_CONTEXT_FILE, 'a')
  try {
    writeFileSync(fd, dumps(record) + '\n')
  } finally {
    closeSync(fd)
  }
  if (!args.values.quiet) {
    out(
      `recorded ${record.phase} context for ${String(sessionId).slice(0, 8)} ` +
        `(${desc.repo_root || cwd})`,
    )
  }
  return 0
}

// --- Claude Code settings ----------------------------------------------------

const HOOK_SETTINGS_KEY = 'hooks'

/** How Claude Code should invoke this CLI from a hook: one shell string. */
function launcherString(): string {
  return config
    .launcherCommand()
    .map((part) =>
      /[\s"']/.test(part) ? `'${part.replace(/'/g, "'\\''")}'` : part,
    )
    .join(' ')
}

/** Baking a disposable path into a long-lived config is a trap, so say so
 * before doing it rather than after it breaks. */
function warnIfEphemeral(): void {
  if (!config.isEphemeralInstall()) return
  out(
    c(
      "\nThis is running from npx's cache, which npm may clear at any time.\n" +
        "The path about to be written into Claude Code's config would then stop\n" +
        'working. Install it properly first:\n',
      YELLOW,
    ),
  )
  out('  npm install -g agent-telemetry')
  out(`  telemetry ${process.argv[2] ?? 'init'}\n`)
}

function hookBlock(): Record<string, unknown[]> {
  const exe = launcherString()
  return {
    SessionStart: [
      {
        hooks: [
          {
            type: 'command',
            command: `${exe} session-hook --phase start --quiet`,
          },
        ],
      },
    ],
    SessionEnd: [
      {
        hooks: [
          {
            type: 'command',
            command: `${exe} session-hook --phase end --quiet`,
          },
        ],
      },
    ],
  }
}

function settingsPath(explicit?: string | null): string {
  return explicit || join(homedir(), '.claude', 'settings.json')
}

function readSettings(target: string): Record<string, any> {
  if (!existsSync(target)) return {}
  return JSON.parse(readFileSync(target, 'utf8') || '{}')
}

function backup(target: string): void {
  const stamp = Math.floor(Date.now() / 1000)
  const backupPath = target.replace(/\.json$/, '') + `.json.bak-${stamp}`
  copyFileSync(target, backupPath)
  out(`backed up existing settings to ${backupPath}`)
}

export async function cmdInstallHooks(args: Parsed): Promise<number> {
  const target = settingsPath(args.values.settings as string)
  warnIfEphemeral()
  out(`${BOLD}This will modify ${target}${RESET}`)
  out(
    'It adds SessionStart and SessionEnd hooks that record the working directory,',
  )
  out(
    'repo root, branch and HEAD for each Claude Code session. It reads no file contents.',
  )
  out(JSON.stringify(hookBlock(), null, 2))
  if (!(await ask('\nProceed?', Boolean(args.values.yes)))) {
    out('aborted')
    return 1
  }

  let settings: Record<string, any> = {}
  if (existsSync(target)) {
    backup(target)
    try {
      settings = readSettings(target)
    } catch {
      out(c('existing settings.json is not valid JSON; aborting', RED))
      return 1
    }
  }

  const hooks = (settings[HOOK_SETTINGS_KEY] ??= {})
  for (const [event, block] of Object.entries(hookBlock())) {
    const existing = (hooks[event] ??= [])
    if (JSON.stringify(existing).includes('session-hook')) continue
    existing.push(...(block as unknown[]))
  }
  mkdirSync(dirname(target), { recursive: true })
  writeFileSync(target, JSON.stringify(settings, null, 2) + '\n')
  out(c('hooks installed', GREEN))
  out('They take effect in newly started Claude Code sessions.')
  return 0
}

export function cmdUninstallHooks(args: Parsed): number {
  const target = settingsPath(args.values.settings as string)
  if (!existsSync(target)) {
    out('nothing to do')
    return 0
  }
  const settings = readSettings(target)
  const hooks = settings[HOOK_SETTINGS_KEY] ?? {}
  let removed = 0
  for (const event of Object.keys(hooks)) {
    const kept: unknown[] = []
    for (const entry of hooks[event]) {
      if (JSON.stringify(entry).includes('session-hook')) {
        removed += 1
        continue
      }
      kept.push(entry)
    }
    if (kept.length) hooks[event] = kept
    else delete hooks[event]
  }
  if (!Object.keys(hooks).length) delete settings[HOOK_SETTINGS_KEY]
  writeFileSync(target, JSON.stringify(settings, null, 2) + '\n')
  out(`removed ${removed} hook entr${removed === 1 ? 'y' : 'ies'}`)
  return 0
}

/** Keys this project owns inside settings.json "env". `config uninstall`
 * removes exactly these and leaves anything else you have put there alone. */
const MANAGED_ENV_PREFIXES = [
  'CLAUDE_CODE_ENABLE_TELEMETRY',
  'CLAUDE_CODE_ENHANCED_TELEMETRY_BETA',
  'CLAUDE_CODE_OTEL_',
  'OTEL_',
  'TELEMETRY_',
]

const managed = (key: string) =>
  MANAGED_ENV_PREFIXES.some((p) => key.startsWith(p))

/** The telemetry variables currently live in Claude Code's settings. */
function installedEnv(target?: string): Record<string, string> {
  try {
    const env = readSettings(target || settingsPath()).env ?? {}
    return Object.fromEntries(
      Object.entries(env).filter(([k]) => managed(k)),
    ) as Record<string, string>
  } catch {
    return {}
  }
}

/** Point every Claude Code session at the local receiver.
 *
 * Writing into settings.json rather than a shell file is deliberate: it covers
 * sessions launched from an editor or a launcher, not only from a terminal that
 * happened to source the right thing.
 */
export async function cmdInstall(args: Parsed): Promise<number> {
  const target = settingsPath(args.values.settings as string)
  const env = config.otelEnv()

  out(`${BOLD}This will add an "env" block to ${target}${RESET}`)
  for (const [k, v] of Object.entries(env)) out(`  ${k}=${v}`)
  out(
    c(
      `\nThis exports prompts, responses, tool bodies and raw API JSON, so ` +
        `${config.HOME}\nwill hold your source code and conversation text. ` +
        `Credentials are scrubbed;\nnothing else is. See PRIVACY.md.`,
      YELLOW,
    ),
  )
  out()
  if (!(await ask('Proceed?', Boolean(args.values.yes)))) {
    out('aborted')
    return 1
  }

  let settings: Record<string, any> = {}
  if (existsSync(target)) {
    backup(target)
    try {
      settings = readSettings(target)
    } catch {
      out(c('existing settings.json is not valid JSON; aborting', RED))
      return 1
    }
  }

  settings.env = { ...(settings.env ?? {}), ...env }
  mkdirSync(dirname(target), { recursive: true })
  writeFileSync(target, JSON.stringify(settings, null, 2) + '\n')
  out(c(`installed ${Object.keys(env).length} variable(s)`, GREEN))
  out('Takes effect in newly started Claude Code sessions - settings are read')
  out('at startup, so sessions already running are unaffected.')
  return 0
}

export function cmdUninstallEnv(args: Parsed): number {
  const target = settingsPath(args.values.settings as string)
  if (!existsSync(target)) {
    out('nothing to do')
    return 0
  }
  const settings = readSettings(target)
  const block = settings.env ?? {}
  const removed = Object.keys(block).filter(managed)
  for (const k of removed) delete block[k]
  if (!Object.keys(block).length) delete settings.env
  writeFileSync(target, JSON.stringify(settings, null, 2) + '\n')
  out(`removed ${removed.length} telemetry variable(s) from ${target}`)
  return 0
}

/** Install the skill that tells Claude Code which tool answers which question.
 *
 * The MCP server gives a session the tools; the skill is what makes it reach
 * for them unprompted. Without it the data is there and nothing asks for it.
 */
export async function cmdInstallSkill(args: Parsed): Promise<number> {
  const source = config.skillSource()
  if (!source) {
    out(c('no SKILL.md in this install; skipping', YELLOW))
    return 1
  }
  const target = config.SKILL_TARGET
  if (
    existsSync(target) &&
    readFileSync(target, 'utf8') === readFileSync(source, 'utf8')
  ) {
    out('the telemetry skill is already installed and current')
    return 0
  }
  out(`${BOLD}This will write ${target}${RESET}`)
  out('It teaches Claude Code which telemetry tool answers which question, so')
  out('the data gets used without being asked for by name. It reads nothing.')
  if (!(await ask('\nProceed?', Boolean(args.values.yes)))) {
    out('skipped')
    return 1
  }
  mkdirSync(dirname(target), { recursive: true })
  copyFileSync(source, target)
  out(c('skill installed', GREEN) + ' - it applies to new Claude Code sessions')
  return 0
}

/** Paths excluded from file activity. */
export function cmdIgnore(args: Parsed): number {
  let patterns = config.loadIgnores()
  let changed = false
  for (const pat of (args.values.add ?? []) as string[]) {
    if (!patterns.includes(pat)) {
      patterns.push(pat)
      changed = true
    }
  }
  for (const pat of (args.values.remove ?? []) as string[]) {
    const idx = patterns.indexOf(pat)
    if (idx !== -1) {
      patterns.splice(idx, 1)
      changed = true
    }
  }
  if (args.values.reset) {
    patterns = [...config.DEFAULT_IGNORES]
    changed = true
  }
  if (changed) config.saveIgnores(patterns)

  const usingDefaults = !existsSync(config.IGNORE_FILE)
  out(
    `${BOLD}Ignored paths${RESET}  ${usingDefaults ? 'built-in defaults' : config.IGNORE_FILE}`,
  )
  for (const pat of patterns) out(`  ${pat}`)
  if (changed)
    out(`\n${DIM}saved - takes effect on the next telemetry analyse${RESET}`)

  if (existsSync(config.DB_PATH)) {
    const conn = db.connect(undefined, false)
    const ignored = db.getMeta(conn, 'ignored_paths')
    const total = db.scalar<number>(
      conn,
      'SELECT COUNT(DISTINCT path) FROM file_activity',
    )
    conn.close()
    out(
      `\n${DIM}${total} distinct paths kept` +
        (ignored ? `; ${ignored} dropped on the last analyse` : '') +
        RESET,
    )
  }
  return 0
}

export function cmdSql(args: Parsed): number {
  const conn = existsSync(config.DB_PATH)
    ? db.connect(undefined, false)
    : db.connect()
  const query = args.positionals[0] ?? ''
  if (!/^\s*(select|with|pragma|explain)\b/i.test(query)) {
    out(
      c(
        'only read queries are allowed here; use sqlite3 directly if you really mean to write',
        RED,
      ),
    )
    conn.close()
    return 1
  }
  const rows = conn.all(query)
  if (args.values.json) {
    out(
      JSON.stringify(
        rows,
        (_k, v) => (typeof v === 'bigint' ? String(v) : v),
        2,
      ),
    )
  } else if (rows.length) {
    const headers = Object.keys(rows[0]!)
    printTable(
      headers,
      rows.map((r) => headers.map((h) => r[h])),
    )
  } else {
    out(`${DIM}(no rows)${RESET}`)
  }
  conn.close()
  return 0
}

// --- self test ---------------------------------------------------------------

const SELF_TEST_USER = 'telemetry-self-test'
const SELF_TEST_SECRET = 'sk-ant-secret1234567890abcd'

/** One log record in Claude Code's OTLP shape, carrying a planted secret.
 *
 * Small on purpose. The point is to prove the path end to end - receiver, raw
 * file, normalization, redaction - not to simulate a realistic session.
 */
function testPayload(
  sessionId: string,
  workspace: string,
): Record<string, unknown> {
  const now = new Date()
  const ts = String(BigInt(now.getTime()) * 1_000_000n)

  const kv = (d: Record<string, unknown>) =>
    Object.entries(d).map(([k, v]) => {
      let value: Record<string, unknown>
      if (typeof v === 'boolean') value = { boolValue: v }
      else if (typeof v === 'number' && Number.isInteger(v))
        value = { intValue: String(v) }
      else if (Array.isArray(v)) {
        value = {
          arrayValue: { values: v.map((x) => ({ stringValue: String(x) })) },
        }
      } else value = { stringValue: String(v) }
      return { key: k, value }
    })

  const attrs = {
    'event.name': 'tool_result',
    'event.timestamp': now.toISOString(),
    'event.sequence': 1,
    'session.id': sessionId,
    'user.id': SELF_TEST_USER,
    'app.version': 'self-test',
    'organization.id': 'self-test',
    'workspace.host_paths': [workspace],
    tool_name: 'Bash',
    success: 'true',
    duration_ms: 12,
    tool_parameters: JSON.stringify({
      command: `export API_TOKEN=${SELF_TEST_SECRET} && echo ok`,
      description: 'self test',
    }),
  }
  return {
    resourceLogs: [
      {
        resource: { attributes: kv({ 'service.name': 'claude-code' }) },
        scopeLogs: [
          {
            scope: { name: 'com.anthropic.claude_code.events' },
            logRecords: [
              {
                timeUnixNano: ts,
                body: { stringValue: 'claude_code.tool_result' },
                attributes: kv(attrs),
              },
            ],
          },
        ],
      },
    ],
  }
}

/** End-to-end self test: receiver -> raw file -> database -> MCP. */
export async function cmdDoctor(args: Parsed): Promise<number> {
  const results: [string, boolean, string][] = []

  const check = async (
    name: string,
    fn: () => Promise<[boolean, string]> | [boolean, string],
  ) => {
    let ok: boolean
    let detail: string
    try {
      ;[ok, detail] = await fn()
    } catch (exc) {
      ok = false
      detail = `${(exc as Error).name}: ${(exc as Error).message}`
    }
    results.push([name, ok, detail])
    out(
      `  [${ok ? c('PASS', GREEN) : c('FAIL', RED)}] ${name}` +
        (detail ? `  ${DIM}${detail}${RESET}` : ''),
    )
    return ok
  }

  out(`${BOLD}Claude Telemetry self-test${RESET}\n`)

  await check('receiver process running', () => [
    Boolean(receiverRunning()),
    `pid ${receiverRunning()}`,
  ])
  await check('health endpoint responds', async () => [
    await healthOk(),
    `port ${config.OTLP_PORT}`,
  ])

  const before = rawStats().bytes
  const sessionId = randomUUID()

  const accepted = await check('test event accepted', async () => {
    const res = await fetch(`http://127.0.0.1:${config.OTLP_PORT}/v1/logs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(testPayload(sessionId, process.cwd())),
      signal: AbortSignal.timeout(10_000),
    })
    return [res.status === 200, `HTTP ${res.status}`]
  })
  if (!accepted) {
    out(
      c('\ncannot continue without a running receiver - telemetry start', RED),
    )
    return 1
  }

  await check('event landed in raw storage', async () => {
    for (let i = 0; i < 20; i++) {
      if (rawStats().bytes > before)
        return [true, `raw grew from ${humanBytes(before)}`]
      await new Promise((r) => setTimeout(r, 1000))
    }
    return [false, 'no new raw bytes after 20s']
  })

  const conn = db.connect()

  await check('normalization works', () => {
    ingest.analyse(conn)
    const n = db.scalar<number>(
      conn,
      'SELECT COUNT(*) FROM events WHERE session_id=?',
      [sessionId],
    )
    const tools = db.scalar<number>(
      conn,
      'SELECT COUNT(*) FROM tool_calls WHERE session_id=?',
      [sessionId],
    )
    return [
      n > 0 && tools > 0,
      `${n} event(s), ${tools} tool call(s) for the test session`,
    ]
  })

  await check('secret redaction applied', () => {
    // Redaction has no off switch, so this must hold in every posture.
    const leaked = db.scalar<number>(
      conn,
      'SELECT COUNT(*) FROM bash_activity WHERE command LIKE ?',
      [`%${SELF_TEST_SECRET}%`],
    )
    return [
      !leaked,
      leaked ? `SECRET LEAKED into ${leaked} row(s)` : 'secret redacted',
    ]
  })

  await check('MCP server answers', async () => {
    // handle() returns the result payload itself, not a JSON-RPC envelope.
    const mcp = await import('./mcp.js')
    const init = mcp.handle({
      jsonrpc: '2.0',
      id: 1,
      method: 'initialize',
      params: {},
    })!
    const listed = mcp.handle({
      jsonrpc: '2.0',
      id: 2,
      method: 'tools/list',
      params: {},
    })!
    const names = (listed.tools as { name: string }[]).map((t) => t.name)
    const called = mcp.handle({
      jsonrpc: '2.0',
      id: 3,
      method: 'tools/call',
      params: { name: 'telemetry_overview', arguments: {} },
    })!
    const ok = Boolean(init.serverInfo) && names.length > 0 && !called.isError
    return [ok, `${names.length} tools; telemetry_overview answered`]
  })

  // The test session is real data in every respect, so leaving it behind would
  // quietly skew every number the database reports.
  if (args.values.keep) {
    out(`\n${DIM}--keep: the test session is still in your database${RESET}`)
  } else {
    const purged = purgeSelfTest(conn)
    if (purged) {
      out(
        `\n${DIM}removed ${purged} test session(s) from the database; ` +
          `raw files are unchanged${RESET}`,
      )
    }
  }
  conn.close()
  const failed = results.filter(([, ok]) => !ok).map(([n]) => n)
  out()
  if (failed.length) {
    out(c(`${failed.length} check(s) failed: ${failed.join(', ')}`, RED))
    return 1
  }
  out(c(`all ${results.length} checks passed`, GREEN))
  return 0
}

/** Delete self-test sessions from the database (raw files are untouched). */
export function purgeSelfTest(conn: db.Db): number {
  const sids = conn
    .all('SELECT session_id FROM sessions WHERE user_id=?', [SELF_TEST_USER])
    .map((r) => String(r.session_id))
  if (!sids.length) return 0
  db.purgeSessions(conn, sids, 'self-test')
  conn.commit()
  return sids.length
}

/** Print the settings Claude Code is running with, and what we would set. */
export function cmdEnv(): number {
  const live = installedEnv()
  out(`${BOLD}Installed in ${settingsPath()}${RESET}`)
  if (Object.keys(live).length) {
    for (const [k, v] of Object.entries(live)) out(`  ${k}=${v}`)
  } else {
    out(`  ${DIM}nothing - run telemetry install${RESET}`)
  }
  out(`\n${BOLD}What telemetry install would set${RESET}`)
  for (const [k, v] of Object.entries(config.otelEnv())) {
    const marker = live[k] === v ? ' ' : c('*', YELLOW)
    out(` ${marker}${k}=${v}`)
  }
  return 0
}

// ------------------------------------------------------------ composites --

async function ask(question: string, assumeYes: boolean): Promise<boolean> {
  if (assumeYes) return true
  if (!process.stdin.isTTY) return false
  const rl = createInterface({ input: process.stdin, output: process.stdout })
  try {
    const answer = await rl.question(`${question} [y/N] `)
    return ['y', 'yes'].includes(answer.trim().toLowerCase())
  } catch {
    out()
    return false
  } finally {
    rl.close()
  }
}

/** How another program should launch this server. */
function mcpCommand(): string[] {
  return [...config.launcherCommand(), 'mcp']
}

/** Serve the database to an MCP client on stdin/stdout.
 *
 * Not something to run by hand - Claude Code launches it. `telemetry init`
 * registers it; `--register` does that on its own.
 */
export async function cmdMcp(args: Parsed): Promise<number> {
  if (args.values.register) return registerMcp(args)
  const mcp = await import('./mcp.js')
  if (process.stdin.isTTY) {
    out(
      `${DIM}This speaks JSON-RPC on stdin/stdout and is meant to be ` +
        `launched by an MCP client.${RESET}`,
    )
    out(
      `Register it with Claude Code:\n  claude mcp add ${config.MCP_SERVER_NAME} ` +
        `--scope user -- ${mcpCommand().join(' ')}`,
    )
    out(`\n${DIM}Waiting for protocol traffic; ctrl-c to quit.${RESET}`)
  }
  return mcp.serve()
}

/** Add this server to Claude Code's MCP configuration. */
async function registerMcp(args: Parsed): Promise<number> {
  const which = spawnSync('which', ['claude'], { encoding: 'utf8' })
  if (which.status !== 0) {
    out(c('the claude CLI is not on PATH; register it yourself with:', YELLOW))
    out(
      `  claude mcp add ${config.MCP_SERVER_NAME} --scope user -- ${mcpCommand().join(' ')}`,
    )
    return 1
  }
  const listed = spawnSync('claude', ['mcp', 'list'], { encoding: 'utf8' })
  if ((listed.stdout || '').includes(`${config.MCP_SERVER_NAME}:`)) {
    out(`${config.MCP_SERVER_NAME} is already registered with Claude Code`)
    return 0
  }
  const cmd = [
    'mcp',
    'add',
    config.MCP_SERVER_NAME,
    '--scope',
    'user',
    '--',
    ...mcpCommand(),
  ]
  warnIfEphemeral()
  out(`${BOLD}This will run:${RESET}\n  claude ${cmd.join(' ')}`)
  out('Claude Code sessions will then be able to query the database directly.')
  if (!(await ask('\nProceed?', Boolean(args.values.yes)))) {
    out('skipped')
    return 1
  }
  const res = spawnSync('claude', cmd, { stdio: 'inherit' })
  if (res.status === 0)
    out(c('registered', GREEN) + ' - it appears in new Claude Code sessions')
  return res.status ?? 1
}

// --- first run --------------------------------------------------------------

/** Guided first run.
 *
 * Every step that writes outside ~/.telemetry asks first, and every step is
 * independently re-runnable, so stopping half way is safe.
 */
export async function cmdInit(args: Parsed): Promise<number> {
  out(`${BOLD}Claude Telemetry — setup${RESET}`)
  out(`${DIM}data lives in ${config.HOME}${RESET}\n`)
  config.ensureDirs()

  out(`${BOLD}1. Point Claude Code at the local receiver${RESET}`)
  await cmdInstall(args)

  out(`\n${BOLD}2. Session hooks${RESET}`)
  await cmdInstallHooks(args)

  out(`\n${BOLD}3. Receiver${RESET}`)
  await cmdStart()

  out(`\n${BOLD}4. Query the database from inside Claude Code${RESET}`)
  await registerMcp(args)

  out(`\n${BOLD}5. The skill that makes Claude reach for it${RESET}`)
  await cmdInstallSkill(args)

  out(`\n${BOLD}6. First analyse${RESET}`)
  cmdAnalyse()

  out(
    `\n${c('ready', GREEN)} - new sessions are captured from now on. ` +
      `Ask Claude about them, or run telemetry status.`,
  )
  return 0
}

/** Take telemetry back out of Claude Code's settings. */
export function cmdUninstall(args: Parsed): number {
  let rc = cmdUninstallEnv(args) || 0
  rc = cmdUninstallHooks(args) || rc
  const which = spawnSync('which', ['claude'], { encoding: 'utf8' })
  if (which.status === 0) {
    spawnSync(
      'claude',
      ['mcp', 'remove', config.MCP_SERVER_NAME, '--scope', 'user'],
      {
        stdio: 'ignore',
      },
    )
    out('removed the MCP server registration')
  }
  if (existsSync(config.SKILL_TARGET)) {
    try {
      unlinkSync(config.SKILL_TARGET)
      out(`removed ${config.SKILL_TARGET}`)
    } catch {
      /* somebody else got there first */
    }
  }
  out(
    `${DIM}The receiver is untouched - telemetry stop shuts it down. ` +
      `Data in ${config.HOME} is left alone.${RESET}`,
  )
  return rc
}

// -------------------------------------------------------------------- parser --

const HELP = `usage: telemetry <command> [options]

Capture and analyse Claude Code OpenTelemetry data locally.

commands:
  init                  guided first run: settings, hooks, receiver, MCP
                          --yes            accept every step
  install               point Claude Code at the local receiver
                          --yes --settings PATH
  install-hooks         add the SessionStart/SessionEnd context hooks
  start                 start the OTLP receiver
  stop                  stop the receiver
  status                is it running, and are events arriving?
  analyse               ingest new raw telemetry into the database
  install-skill         install the skill that tells Claude which tool to use
  mcp                   serve the database to Claude Code over MCP
                          --register       add this server to Claude Code and exit
  sql QUERY             run a read-only SQL query against the database
                          --json
  doctor                run the end-to-end self test
                          --keep           leave the test session in the database
  config <what>         ignores, teardown
    ignore                paths excluded from file activity
                            --add GLOB --remove GLOB --reset
    env                   show the telemetry settings Claude Code has
    uninstall             remove the env block, hooks, skill and MCP registration
                            --settings PATH
`

const OPTIONS = {
  settings: { type: 'string' },
  phase: { type: 'string' },
  'session-id': { type: 'string' },
  cwd: { type: 'string' },
  add: { type: 'string', multiple: true },
  remove: { type: 'string', multiple: true },
  yes: { type: 'boolean' },
  quiet: { type: 'boolean' },
  json: { type: 'boolean' },
  keep: { type: 'boolean' },
  register: { type: 'boolean' },
  reset: { type: 'boolean' },
} as const

const parse = (argv: string[]) =>
  parseArgs({ args: argv, options: OPTIONS, allowPositionals: true })

type Parsed = ReturnType<typeof parse>

export async function main(argv: string[]): Promise<number> {
  // Bare `telemetry` reports where things stand; the analysis itself lives in
  // Claude Code, over MCP.
  const args = argv.length ? argv : ['status']
  const command = args[0]!

  if (command === '-h' || command === '--help' || command === 'help') {
    process.stdout.write(HELP)
    return 0
  }

  let parsed: Parsed
  try {
    parsed = parse(args.slice(1))
  } catch (exc) {
    out(c((exc as Error).message, RED))
    process.stdout.write(HELP)
    return 1
  }
  config.ensureDirs()

  switch (command) {
    case 'init':
      return cmdInit(parsed)
    case 'install':
      return cmdInstall(parsed)
    case 'install-hooks':
      return cmdInstallHooks(parsed)
    case 'install-skill':
      return cmdInstallSkill(parsed)
    case 'start':
      return cmdStart()
    case 'stop':
      return cmdStop()
    case 'status':
      return cmdStatus()
    case 'analyse':
    case 'analyze':
      return cmdAnalyse()
    case 'mcp':
      return cmdMcp(parsed)
    case 'sql':
      return cmdSql(parsed)
    case 'doctor':
      return cmdDoctor(parsed)
    case 'session-hook':
      return cmdSessionHook(parsed)
    case 'receive': {
      // Hidden: how `start` runs the receiver in the background.
      const { serve } = await import('./receiver.js')
      serve(
        Number.parseInt(parsed.positionals[0] ?? String(config.OTLP_PORT), 10),
      )
      return new Promise<number>(() => {}) // runs until signalled
    }
    case 'config': {
      const sub = parsed.positionals[0]
      switch (sub) {
        case 'ignore':
          return cmdIgnore(parsed)
        case 'env':
          return cmdEnv()
        case 'uninstall':
          return cmdUninstall(parsed)
        default:
          process.stdout.write(HELP)
          return sub === undefined ? 0 : 1
      }
    }
    default:
      process.stdout.write(HELP)
      out(c(`\nunknown command: ${command}`, RED))
      return 1
  }
}
