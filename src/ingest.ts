/** Raw OTLP JSONL  ->  normalized SQLite.
 *
 * Incremental and idempotent: each raw file has a line cursor in `raw_files`,
 * and every normalized row carries a dedupe key, so re-running `analyse` never
 * double counts. The raw files themselves are never modified.
 */
import {
  closeSync,
  existsSync,
  openSync,
  readdirSync,
  readSync,
  statSync,
} from 'node:fs'
import { createHash } from 'node:crypto'
import { homedir } from 'node:os'
import {
  basename,
  dirname,
  extname,
  isAbsolute,
  join,
  matchesGlob,
  relative,
  resolve,
} from 'node:path'
import { realpathSync } from 'node:fs'
import * as config from './config.js'
import * as db from './db.js'
import type { Db, Row } from './db.js'
import * as gitctx from './gitctx.js'
import * as otlp from './otlp.js'
import * as shellfiles from './shellfiles.js'
import { refresh as refreshInventory } from './inventory.js'
import { filterToolParams, hashText, scrub, scrubDeep } from './redact.js'
import { dumps, purePath, stableStringify, strip, utcNow } from './util/text.js'

// --------------------------------------------------------------- constants ---

const EVENT_PREFIX = 'claude_code.'

/** Tool name -> category. Only categories we can justify from observed tool
 * names are used; anything unrecognised falls through to "other". */
const FILE_TOOLS: Record<string, string> = {
  Read: 'read',
  Edit: 'edit',
  MultiEdit: 'edit',
  Write: 'write',
  NotebookEdit: 'notebook_edit',
}
const SHELL_TOOLS = new Set(['Bash', 'BashOutput', 'KillShell', 'KillBash'])
const SEARCH_TOOLS = new Set(['Grep', 'Glob', 'LS', 'ToolSearch'])
const PLANNING_TOOLS = new Set(['TodoWrite', 'ExitPlanMode', 'EnterPlanMode'])
const WEB_TOOLS = new Set(['WebSearch', 'WebFetch'])
const SUBAGENT_TOOLS = new Set(['Task', 'Agent'])
const SKILL_TOOLS = new Set(['Skill'])

export function toolCategory(toolName: string | null | undefined): string {
  if (!toolName) return 'unknown'
  // Claude Code reports every MCP invocation with the literal tool_name
  // "mcp_tool"; the real server and tool live in tool_parameters.
  if (toolName.startsWith('mcp__') || toolName === 'mcp_tool') return 'mcp'
  if (toolName in FILE_TOOLS) return 'file'
  if (SHELL_TOOLS.has(toolName)) return 'shell'
  if (SEARCH_TOOLS.has(toolName)) return 'search'
  if (WEB_TOOLS.has(toolName)) return 'web'
  if (SUBAGENT_TOOLS.has(toolName)) return 'subagent'
  if (SKILL_TOOLS.has(toolName)) return 'skill'
  if (PLANNING_TOOLS.has(toolName)) return 'planning'
  return 'other'
}

/** Shell tokens that are not themselves the work being done.
 *   WRAPPERS   - transparent: the real program is the next token (`sudo docker`)
 *   TERMINAL   - the segment carries no program at all (`cd /some/path`)
 *   LOW_SIGNAL - real programs, but plumbing; kept in `programs`, avoided as
 *                the primary when something more meaningful is present
 */
const WRAPPERS = new Set([
  'sudo',
  'env',
  'time',
  'nohup',
  'exec',
  'command',
  'builtin',
  'eval',
  'npx',
  'bunx',
  'pnpx',
  'xargs',
  'nice',
  'then',
  'do',
  'else',
  'elif', // `do tsc $f` - tsc is the program
])
const TERMINAL = new Set([
  'cd',
  'export',
  'set',
  'unset',
  'source',
  '.',
  'fi',
  'done',
  'if',
  'while',
  'for',
])
const LOW_SIGNAL = new Set([
  'echo',
  'printf',
  'cat',
  'head',
  'tail',
  'wc',
  'ls',
  'pwd',
  'true',
  'false',
  'sleep',
  'which',
  'basename',
  'dirname',
  'tee',
  'sort',
  'uniq',
  'tr',
  'cut',
  'date',
  'mkdir',
  'touch',
])

const SEGMENT_SPLIT = /&&|\|\||;|\||\n/
const ENV_ASSIGN = /^[A-Za-z_][A-Za-z0-9_]*=/

/** Every meaningful program in a command line, in order.
 *
 * `cd /x/y && sed -n '1,40p' f` yields ["sed"]: the directory change is not
 * the work, and `/x/y` is an argument, not a program. Pipelines and `&&`
 * chains each contribute their own program.
 */
export function bashPrograms(command: string | null | undefined): string[] {
  if (!command) return []
  // Everything after a heredoc marker is data, not shell. Without this, the
  // body of `python3 - <<EOF ... import json ...` parses as programs.
  const head = command.split('<<')[0]!
  const out: string[] = []
  for (const rawSegment of head.split(SEGMENT_SPLIT)) {
    const segment = rawSegment.trim()
    if (segment.startsWith('#')) continue
    for (const rawToken of segment.split(/\s+/)) {
      const token = strip(rawToken, '()`$\'"')
      if (!token || ENV_ASSIGN.test(token)) continue // leading FOO=bar assignments
      if (token.startsWith('-')) break // a flag: we already passed the program
      const name = token.split('/').pop()!
      if (WRAPPERS.has(name)) continue // transparent: keep looking in this segment
      if (TERMINAL.has(name)) break // nothing in this segment is a program
      if (name && !out.includes(name)) out.push(name)
      break
    }
  }
  return out
}

/** The most descriptive program in a command line. */
export function primaryProgram(programs: string[]): string | null {
  if (!programs.length) return null
  for (const name of programs) if (!LOW_SIGNAL.has(name)) return name
  return programs[0]!
}

/** Split `mcp__server__tool` into [server, tool]. */
export function mcpParts(
  toolName: string | null | undefined,
): [string | null, string | null] {
  if (!toolName || !toolName.startsWith('mcp__')) return [null, null]
  const bits = toolName.split('__')
  const server = bits.length > 1 ? bits[1]! : null
  const tool = bits.length > 2 ? bits.slice(2).join('__') : null
  return [server, tool]
}

function asInt(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null
  const f = Number(v)
  return Number.isFinite(f) ? Math.trunc(f) : null
}

function asFloat(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null
  const f = Number(v)
  return Number.isFinite(f) ? f : null
}

function asBoolInt(v: unknown): number | null {
  if (v === null || v === undefined) return null
  if (typeof v === 'boolean') return v ? 1 : 0
  const s = String(v).trim().toLowerCase()
  if (s === 'true' || s === '1' || s === 'yes') return 1
  if (s === 'false' || s === '0' || s === 'no') return 0
  return null
}

/** Attribute values arrive as JSON strings; decode when possible. */
function maybeJson(v: unknown): Record<string, unknown> | unknown[] | null {
  if (v === null || v === undefined) return null
  if (typeof v === 'object') return v as Record<string, unknown> | unknown[]
  if (typeof v === 'string') {
    const s = v.trim()
    if (s.startsWith('{') || s.startsWith('[')) {
      try {
        const parsed = JSON.parse(s)
        return parsed && typeof parsed === 'object' ? parsed : null
      } catch {
        return null
      }
    }
  }
  return null
}

function attrHash(d: Record<string, unknown>): string {
  return createHash('sha256')
    .update(stableStringify(d), 'utf8')
    .digest('hex')
    .slice(0, 16)
}

/** `Path(base) / rel`.
 *
 * Not `path.join`: PurePath collapses repeated slashes, drops `.` segments and
 * strips a trailing slash, but leaves `..` alone rather than resolving it
 * against a directory that may no longer exist.
 */
function pathJoin(base: string, rel: string): string {
  return purePath(`${base}/${rel}`)
}

/** `str(Path(p).expanduser())` - including the normalisation `Path()` does on
 * the way out, which is what drops a trailing slash from `~/.claude/skills/`. */
function expandUser(path: string): string {
  const home = homedir()
  if (path === '~') return purePath(home)
  if (path.startsWith('~/')) return purePath(`${home}/${path.slice(2)}`)
  return purePath(path)
}

/** `Path.resolve()`: absolute, with symlinks resolved as far as they exist. */
function pyResolve(path: string): string {
  const abs = resolve(path)
  const tail: string[] = []
  let cur = abs
  for (;;) {
    try {
      const real = realpathSync(cur)
      return tail.length ? join(real, ...tail.reverse()) : real
    } catch {
      const parent = dirname(cur)
      if (parent === cur) return abs
      tail.push(basename(cur))
      cur = parent
    }
  }
}

// ------------------------------------------------------------ raw scanning ---

const SIGNAL_GLOBS: [string, string][] = [
  ['logs', 'logs*.jsonl'],
  ['metrics', 'metrics*.jsonl'],
  ['traces', 'traces*.jsonl'],
]

export function scanRawFiles(rawDir?: string): [string, string][] {
  const dir = rawDir || config.RAW_DIR
  const found: [string, string][] = []
  if (!existsSync(dir)) return found
  let names: string[]
  try {
    names = readdirSync(dir)
  } catch {
    return found
  }
  for (const [signal, pattern] of SIGNAL_GLOBS) {
    for (const name of names.filter((n) => matchesGlob(n, pattern)).sort()) {
      const full = join(dir, name)
      try {
        if (statSync(full).isFile()) found.push([full, signal])
      } catch {
        /* vanished between readdir and stat */
      }
    }
  }
  return found
}

/** Lines of a file, without holding the whole thing in memory. */
function* readLines(path: string): Generator<[number, string]> {
  const fd = openSync(path, 'r')
  try {
    const CHUNK = 1 << 20
    const buf = Buffer.allocUnsafe(CHUNK)
    let leftover = Buffer.alloc(0)
    let lineno = 0
    for (;;) {
      const n = readSync(fd, buf, 0, CHUNK, null)
      if (n === 0) break
      const data = leftover.length
        ? Buffer.concat([leftover, buf.subarray(0, n)])
        : Buffer.from(buf.subarray(0, n))
      let start = 0
      let idx: number
      while ((idx = data.indexOf(0x0a, start)) !== -1) {
        lineno += 1
        yield [lineno, data.subarray(start, idx).toString('utf8')]
        start = idx + 1
      }
      leftover = data.subarray(start)
    }
    if (leftover.length) {
      lineno += 1
      yield [lineno, leftover.toString('utf8')]
    }
  } finally {
    closeSync(fd)
  }
}

// ------------------------------------------------------------- normalizing ---

export interface Counts {
  logs: number
  metrics: number
  traces: number
  skipped: number
}

interface Ctx {
  event_id?: number
  session_id: string | null
  ts: string | null
  ts_ns?: bigint | null
  dk?: string
  prompt_id?: string | null
}

type Attrs = Record<string, unknown>

const STANDARD_KEYS = [
  'session.id',
  'user.id',
  'user.email',
  'user.account_uuid',
  'user.account_id',
  'organization.id',
  'app.version',
  'app.entrypoint',
  'terminal.type',
  'workspace.host_paths',
  'user.groups',
  'identity.source',
]

const API_COMMON: [string, string][] = [
  ['model', 'model'],
  ['request_id', 'request_id'],
  ['client_request_id', 'client_request_id'],
  ['speed', 'speed'],
  ['query_source', 'query_source'],
  ['effort', 'effort'],
  ['agent.name', 'agent_name'],
  ['skill.name', 'skill_name'],
  ['plugin.name', 'plugin_name'],
  ['marketplace.name', 'marketplace_name'],
  ['mcp_server.name', 'mcp_server_name'],
  ['mcp_tool.name', 'mcp_tool_name'],
]

const ABS_PATH_RE = /\/(?:[A-Za-z0-9._+@-]+\/)*[A-Za-z0-9._+@-]+/g

const CONVENTIONAL =
  /^(?<type>[a-zA-Z]+)(?:\((?<scope>[^)]+)\))?(?<breaking>!)?:\s*(?<subject>.*)$/
const REVERT = /^Revert\s+"(?<subject>.*)"\s*$/

/** Conservative correction cues. Matched against the start of a prompt or as a
 * whole phrase, because "no" inside a sentence means nothing. These are
 * heuristics and are recorded with the cue that fired so a human can audit any
 * number built on them. */
const CORRECTION_CUES = [
  'no,',
  'no ',
  'nope',
  'not quite',
  'not right',
  "that's wrong",
  'thats wrong',
  "doesn't work",
  'doesnt work',
  "didn't work",
  'didnt work',
  'still not',
  'still broken',
  'try again',
  'revert',
  'undo',
  'instead',
  'actually',
  'i meant',
  'wrong ',
  'fix that',
  'that broke',
  'go back',
  'not what i',
  're-review',
  'review again',
]
const STEERING_MAX_CHARS = 45

/** Prompts the harness injects rather than the human typing: monitor
 * notifications, system reminders, slash-command echoes. They cost money and
 * belong in the cost figures, but counting them as human turns overstates
 * effort and skews the steering share. */
const SYSTEM_PROMPT_RE =
  /^\s*<(task-notification|system-reminder|local-command-[a-z]+|command-name|command-message|command-args)\b/i

const DIR_CACHE_TTL_S = 86400
const ADDED_CACHE_TTL_S = 6 * 3600

export class Ingestor {
  private readonly db: Db
  private readonly progress: (msg: string) => void
  readonly counts: Counts = { logs: 0, metrics: 0, traces: 0, skipped: 0 }
  /** session_id -> merged standard attributes seen so far */
  private readonly sessionAttrs = new Map<string, Attrs>()
  private readonly logHandlers: Record<string, (attrs: Attrs, ctx: Ctx) => void>

  constructor(conn: Db, progress?: (msg: string) => void) {
    this.db = conn
    // Where a caller wants to say what is happening.
    this.progress = progress || (() => {})
    this.logHandlers = {
      api_request: (a, c) => this.evApiRequest(a, c),
      api_error: (a, c) => this.evApiError(a, c),
      api_refusal: (a, c) => this.evApiRefusal(a, c),
      user_prompt: (a, c) => this.evUserPrompt(a, c),
      assistant_response: (a, c) => this.evAssistantResponse(a, c),
      tool_result: (a, c) => this.toolRow(a, c, 'tool_result'),
      tool_decision: (a, c) => this.toolRow(a, c, 'tool_decision'),
      internal_error: (a, c) => this.evInternalError(a, c),
      compaction: (a, c) => this.evCompaction(a, c),
      mcp_server_connection: (a, c) => this.evMcpServerConnection(a, c),
    }
  }

  // -- entry point ---------------------------------------------------------

  run(rawDir?: string): Counts & { files: number } {
    const started = utcNow()
    const files = scanRawFiles(rawDir)
    for (const [path, signal] of files) this.ingestFile(path, signal)
    this.progress('deriving sessions, projects, files and turns')
    this.db.commit()

    this.importSessionContext()
    this.flushSessions()
    this.resolveProjects()
    // Needs session cwd, which resolveProjects has just settled.
    this.deriveShellFiles()
    this.applyFileIgnores()
    this.propagateProjectIds()
    // Session timestamps must be settled before the two passes that depend on
    // a session's time window.
    this.finaliseSessions()
    this.deriveFileCreation()
    this.deriveTurns()
    if (config.GIT_RECONCILE) {
      this.reconcileGit()
      this.classifyCommits()
    }
    // Inventory is a local directory scan and several analyses depend on it,
    // so refresh it here rather than only when a report is run.
    try {
      refreshInventory(this.db)
    } catch {
      /* inventory is a nicety, not a precondition */
    }
    this.dropPurged()
    this.db.checkpoint()

    this.db.run(
      'INSERT INTO ingest_runs(started_at, finished_at, files_scanned,' +
        ' logs_ingested, metrics_ingested, spans_ingested, notes) VALUES (?,?,?,?,?,?,?)',
      [
        started,
        utcNow(),
        files.length,
        this.counts.logs,
        this.counts.metrics,
        this.counts.traces,
        dumps({ skipped_lines: this.counts.skipped }),
      ],
    )
    db.setMeta(this.db, 'last_analyse_at', utcNow())
    this.db.commit()
    return { ...this.counts, files: files.length }
  }

  /** Keep a purged session purged.
   *
   * The collector batches, so events for a session can land after it was
   * deleted - which is how the self-test's synthetic sessions kept coming back
   * into a real database, one per run. Anything belonging to a purged id is
   * dropped again here rather than being allowed to rebuild it.
   */
  private dropPurged(): void {
    const ids = this.db
      .all('SELECT session_id FROM purged_sessions')
      .map((r) => String(r.session_id))
    if (!ids.length) return
    const marks = ids.map(() => '?').join(',')
    this.db.run('PRAGMA defer_foreign_keys = ON')
    for (const table of db.SESSION_TABLES) {
      this.db.run(`DELETE FROM ${table} WHERE session_id IN (${marks})`, ids)
    }
    this.db.commit()
  }

  // -- file handling -------------------------------------------------------

  private ingestFile(path: string, signal: string): void {
    const st = statSync(path)
    this.progress(
      `reading ${basename(path)} (${(st.size / 1024 / 1024).toFixed(1)} MB)`,
    )
    const row = this.db.get(
      'SELECT lines_consumed, bytes_consumed, inode FROM raw_files WHERE path=?',
      [path],
    )
    let startLine = row ? Number(row.lines_consumed) : 0

    // The receiver rotates its active file at a size threshold: the old
    // content is renamed and a fresh file takes the same path. A cursor keyed
    // on path alone would then skip the start of the new file, so compare
    // identity and size and rewind when either says "different file".
    // Re-reading is safe because every row has a semantic dedupe key, so
    // replaying a file cannot double count.
    if (row !== undefined) {
      const rotated =
        row.inode === null ||
        row.inode === undefined ||
        Number(row.inode) !== st.ino ||
        st.size < Number(row.bytes_consumed || 0)
      if (rotated) startLine = 0
    }

    let consumed = startLine
    let ingested = 0
    for (const [lineno, rawLine] of readLines(path)) {
      if (lineno <= startLine) continue
      consumed = lineno
      const line = rawLine.trim()
      if (!line) continue
      let payload: Record<string, any>
      try {
        payload = JSON.parse(line)
      } catch {
        // Partially flushed final line: stop here and retry next run.
        consumed = lineno - 1
        this.counts.skipped += 1
        break
      }
      try {
        ingested += this.dispatch(payload, signal, path, lineno)
      } catch (exc) {
        // Never let one bad record stop ingest.
        this.counts.skipped += 1
        db.setMeta(
          this.db,
          'last_ingest_error',
          `${path}:${lineno}: ${(exc as Error).name}: ${(exc as Error).message}`,
        )
      }
    }

    const size = statSync(path).size
    this.db.run(
      'INSERT INTO raw_files(path, signal, lines_consumed, bytes_consumed,' +
        ' inode, records_ingested, first_ingested_at, last_ingested_at)' +
        ' VALUES (?,?,?,?,?,?,?,?)' +
        ' ON CONFLICT(path) DO UPDATE SET lines_consumed=excluded.lines_consumed,' +
        ' bytes_consumed=excluded.bytes_consumed, inode=excluded.inode,' +
        ' records_ingested=raw_files.records_ingested+excluded.records_ingested,' +
        ' last_ingested_at=excluded.last_ingested_at',
      [path, signal, consumed, size, st.ino, ingested, utcNow(), utcNow()],
    )
    this.db.checkpoint()
  }

  private dispatch(
    payload: Record<string, any>,
    signal: string,
    path: string,
    lineno: number,
  ): number {
    let n = 0
    if (signal === 'logs') {
      let i = 0
      for (const rec of otlp.iterLogs(payload)) {
        this.handleLog(rec, path, lineno, i)
        i += 1
        n += 1
      }
      this.counts.logs += n
    } else if (signal === 'metrics') {
      let i = 0
      for (const pt of otlp.iterMetricPoints(payload)) {
        this.handleMetric(pt, path, lineno, i)
        i += 1
        n += 1
      }
      this.counts.metrics += n
    } else if (signal === 'traces') {
      let i = 0
      for (const sp of otlp.iterSpans(payload)) {
        this.handleSpan(sp, path, lineno, i)
        i += 1
        n += 1
      }
      this.counts.traces += n
    }
    return n
  }

  // -- shared attribute handling ------------------------------------------

  private trackSession(attrs: Attrs, resource: Attrs): string | null {
    const sid = (attrs['session.id'] ?? resource['session.id']) as
      string | undefined
    if (!sid) return null
    let cur = this.sessionAttrs.get(sid)
    if (!cur) {
      cur = { resource: {} }
      this.sessionAttrs.set(sid, cur)
    }
    for (const k of STANDARD_KEYS) {
      const v = k in attrs ? attrs[k] : resource[k]
      const existing = cur[k]
      if (
        v !== null &&
        v !== undefined &&
        v !== '' &&
        (existing === null || existing === undefined || existing === '')
      ) {
        cur[k] = v
      }
    }
    const resourceBag = cur.resource as Attrs
    for (const [k, v] of Object.entries(resource)) {
      if (!STANDARD_KEYS.includes(k) && k !== 'service.name') resourceBag[k] = v
    }
    return sid
  }

  // -- logs ----------------------------------------------------------------

  private handleLog(
    rec: otlp.LogRecord,
    path: string,
    lineno: number,
    idx: number,
  ): void {
    const attrs = rec.attributes
    const resource = rec.resource
    const body = rec.body

    let name = attrs['event.name'] as string | undefined
    if (!name && typeof body === 'string') {
      name = body.startsWith(EVENT_PREFIX)
        ? body.slice(EVENT_PREFIX.length)
        : body
    }
    name = (name || 'unknown').trim()

    const sid = this.trackSession(attrs, resource)
    const ts =
      otlp.nsToIso(rec.ts_ns) ?? ((attrs['event.timestamp'] as string) || null)
    const seq = asInt(attrs['event.sequence'])

    const dk =
      sid && seq !== null
        ? `ev|${sid}|${seq}|${name}`
        : `ev|${path}|${lineno}|${idx}`

    const safeAttrs = this.sanitiseEventAttrs(name, attrs)

    const eventId = db.insertIgnore(this.db, 'events', {
      dedupe_key: dk,
      session_id: sid,
      event_name: name,
      ts,
      ts_ns: rec.ts_ns,
      sequence: seq,
      prompt_id: (attrs['prompt.id'] as string) ?? null,
      message_uuid: (attrs['message.uuid'] as string) ?? null,
      trace_id: rec.trace_id,
      span_id: rec.span_id,
      attrs_json: dumps(safeAttrs),
      raw_json: dumps(rec.record),
      source_path: path,
      source_line: lineno,
    })
    if (eventId === null) return // already ingested

    const ctx: Ctx = {
      event_id: eventId,
      session_id: sid,
      ts,
      ts_ns: rec.ts_ns,
      dk,
      prompt_id: (attrs['prompt.id'] as string) ?? null,
    }

    const handler = this.logHandlers[name]
    if (handler) handler(attrs, ctx)
  }

  /** Apply the content policy before anything is written to the DB. */
  private sanitiseEventAttrs(_name: string, attrs: Attrs): Attrs {
    const out: Attrs = {}
    for (const [k, v] of Object.entries(attrs)) {
      if (
        (k === 'prompt' || k === 'response' || k === 'user_prompt') &&
        !config.STORE_CONTENT
      ) {
        out[k] =
          v !== null && v !== undefined && v !== '' ? '[CONTENT NOT STORED]' : v
        continue
      }
      if (k === 'body' && !config.STORE_API_BODIES) {
        out[k] = '[CONTENT NOT STORED]'
        continue
      }
      if (k === 'tool_parameters' || k === 'tool_input') {
        const parsed = maybeJson(v)
        const [filtered, dropped] = filterToolParams(
          parsed,
          config.STORE_TOOL_CONTENT,
        )
        out[k] = filtered
        if (dropped.length) out[k + '.dropped_keys'] = dropped
        continue
      }
      out[k] = scrubDeep(v)
    }
    return out
  }

  // -- individual event handlers ------------------------------------------

  private apiRow(
    attrs: Attrs,
    ctx: Ctx,
    outcome: string,
  ): Record<string, db.Bindable> {
    const row: Record<string, db.Bindable> = {
      dedupe_key: ctx.dk!,
      event_id: ctx.event_id!,
      session_id: ctx.session_id,
      prompt_id: ctx.prompt_id ?? null,
      ts: ctx.ts,
      ts_ns: ctx.ts_ns ?? null,
      outcome,
      duration_ms: asFloat(attrs.duration_ms),
      attempt: asInt(attrs.attempt),
      status_code:
        attrs.status_code !== null && attrs.status_code !== undefined
          ? String(attrs.status_code)
          : null,
    }
    for (const [src, dest] of API_COMMON)
      row[dest] = (attrs[src] as db.Bindable) ?? null
    return row
  }

  private evApiRequest(attrs: Attrs, ctx: Ctx): void {
    const row = this.apiRow(attrs, ctx, 'ok')
    Object.assign(row, {
      cost_usd: asFloat(attrs.cost_usd),
      cost_usd_micros: asInt(attrs.cost_usd_micros),
      input_tokens: asInt(attrs.input_tokens),
      output_tokens: asInt(attrs.output_tokens),
      cache_read_tokens: asInt(attrs.cache_read_tokens),
      cache_creation_tokens: asInt(attrs.cache_creation_tokens),
    })
    if (row.cost_usd === null && row.cost_usd_micros !== null) {
      row.cost_usd = (row.cost_usd_micros as number) / 1_000_000
    }
    db.insertIgnore(this.db, 'api_calls', row)
    this.attributeSkill(attrs, ctx, 'api_attribution')
  }

  private evApiError(attrs: Attrs, ctx: Ctx): void {
    const row = this.apiRow(attrs, ctx, 'error')
    row.error = scrub(attrs.error as string | null)
    db.insertIgnore(this.db, 'api_calls', row)
    db.insertIgnore(this.db, 'errors', {
      dedupe_key: ctx.dk!,
      session_id: ctx.session_id,
      ts: ctx.ts,
      kind: 'api_error',
      source_event: 'api_error',
      model: (attrs.model as string) ?? null,
      status_code: row.status_code,
      message: row.error,
    })
  }

  private evApiRefusal(attrs: Attrs, ctx: Ctx): void {
    const row = this.apiRow(attrs, ctx, 'refusal')
    row.refusal_category = (attrs.category as string) ?? null
    db.insertIgnore(this.db, 'api_calls', row)
    db.insertIgnore(this.db, 'errors', {
      dedupe_key: ctx.dk!,
      session_id: ctx.session_id,
      ts: ctx.ts,
      kind: 'api_refusal',
      source_event: 'api_refusal',
      model: (attrs.model as string) ?? null,
      error_name: (attrs.category as string) ?? null,
    })
  }

  private evUserPrompt(attrs: Attrs, ctx: Ctx): void {
    const text = config.STORE_CONTENT
      ? (attrs.prompt as string | undefined)
      : null
    db.insertIgnore(this.db, 'prompts', {
      dedupe_key: ctx.dk!,
      session_id: ctx.session_id,
      prompt_id: ctx.prompt_id ?? null,
      message_uuid: (attrs['message.uuid'] as string) ?? null,
      ts: ctx.ts,
      prompt_length: asInt(attrs.prompt_length),
      command_name: (attrs.command_name as string) ?? null,
      command_source: (attrs.command_source as string) ?? null,
      prompt_text: text && text !== '<REDACTED>' ? scrub(text) : null,
    })
  }

  private evAssistantResponse(attrs: Attrs, ctx: Ctx): void {
    const text = config.STORE_CONTENT
      ? (attrs.response as string | undefined)
      : null
    db.insertIgnore(this.db, 'responses', {
      dedupe_key: ctx.dk!,
      session_id: ctx.session_id,
      prompt_id: ctx.prompt_id ?? null,
      message_uuid: (attrs['message.uuid'] as string) ?? null,
      ts: ctx.ts,
      response_length: asInt(attrs.response_length),
      model: (attrs.model as string) ?? null,
      request_id: (attrs.request_id as string) ?? null,
      query_source: (attrs.query_source as string) ?? null,
      response_text: text && text !== '<REDACTED>' ? scrub(text) : null,
    })
  }

  private evInternalError(attrs: Attrs, ctx: Ctx): void {
    db.insertIgnore(this.db, 'errors', {
      dedupe_key: ctx.dk!,
      session_id: ctx.session_id,
      ts: ctx.ts,
      kind: 'internal_error',
      source_event: 'internal_error',
      error_name: (attrs.error_name as string) ?? null,
      error_code: (attrs.error_code as string) ?? null,
    })
  }

  /** Undocumented event. A failed compaction burns tokens and loses context,
   * so surface it alongside the other errors. */
  private evCompaction(attrs: Attrs, ctx: Ctx): void {
    if (asBoolInt(attrs.success) === 0) {
      db.insertIgnore(this.db, 'errors', {
        dedupe_key: ctx.dk!,
        session_id: ctx.session_id,
        ts: ctx.ts,
        kind: 'compaction_failed',
        source_event: 'compaction',
        error_name: (attrs.trigger as string) ?? null,
        message: scrub(attrs.error as string | null),
      })
    }
  }

  private evMcpServerConnection(attrs: Attrs, ctx: Ctx): void {
    if (String(attrs.status) === 'failed') {
      db.insertIgnore(this.db, 'errors', {
        dedupe_key: ctx.dk!,
        session_id: ctx.session_id,
        ts: ctx.ts,
        kind: 'mcp_connection',
        source_event: 'mcp_server_connection',
        error_code: (attrs.error_code as string) ?? null,
        message: scrub(attrs.error as string | null),
      })
    }
  }

  // -- tool calls ----------------------------------------------------------

  private toolRow(attrs: Attrs, ctx: Ctx, origin: string): void {
    const sid = ctx.session_id
    const toolUseId = (attrs.tool_use_id as string) ?? null
    const mergeKey = toolUseId ? `tc|${sid}|${toolUseId}` : `tc|${ctx.dk}`

    const toolName = (attrs.tool_name as string) ?? null
    const params =
      (maybeJson(attrs.tool_parameters) as Record<string, unknown>) || {}
    const tinput =
      (maybeJson(attrs.tool_input) as Record<string, unknown>) || {}
    const mergedParams = { ...tinput, ...params }
    const [filteredRaw, dropped] = filterToolParams(
      mergedParams,
      config.STORE_TOOL_CONTENT,
    )
    const filtered = (filteredRaw as Record<string, unknown>) || {}

    const [mserver, mtool] = mcpParts(toolName)
    const filePathRaw =
      filtered.file_path ??
      filtered.filePath ??
      filtered.path ??
      filtered.notebook_path
    // Observed in 2.1.237: `bash_command` holds only the program name ("wc"),
    // while `full_command` holds the whole command line. Prefer the fullest
    // form available.
    const commandRaw =
      filtered.full_command ||
      (toolName && SHELL_TOOLS.has(toolName) ? filtered.command : null) ||
      filtered.bash_command
    const command = typeof commandRaw === 'string' ? scrub(commandRaw) : null

    const row: Record<string, db.Bindable> = {
      merge_key: mergeKey,
      session_id: sid,
      prompt_id: ctx.prompt_id ?? null,
      ts: ctx.ts,
      ts_ns: ctx.ts_ns ?? null,
      tool_use_id: toolUseId,
      tool_name: toolName,
      tool_source: (attrs.tool_source as string) ?? null,
      tool_category: toolCategory(toolName),
      success: asBoolInt(attrs.success),
      duration_ms: asFloat(attrs.duration_ms),
      error_type: (attrs.error_type as string) ?? null,
      error_message: scrub(attrs.error as string | null),
      decision: ((attrs.decision || attrs.decision_type) as string) ?? null,
      decision_source:
        ((attrs.source || attrs.decision_source) as string) ?? null,
      tool_input_size_bytes: asInt(attrs.tool_input_size_bytes),
      tool_result_size_bytes: asInt(attrs.tool_result_size_bytes),
      mcp_server_name:
        ((filtered.mcp_server_name as string) || mserver) ?? null,
      mcp_tool_name: ((filtered.mcp_tool_name as string) || mtool) ?? null,
      mcp_server_scope: (attrs.mcp_server_scope as string) ?? null,
      skill_name:
        ((filtered.skill_name ||
          filtered.skill ||
          attrs['skill.name']) as string) ?? null,
      subagent_type:
        ((filtered.subagent_type || filtered.agent_type) as string) ?? null,
      file_path: typeof filePathRaw === 'string' ? filePathRaw : null,
      bash_command: command,
      params_json: Object.keys(filtered).length ? dumps(filtered) : null,
      dropped_param_keys: dropped.length
        ? dumps([...new Set(dropped)].sort())
        : null,
      sources: origin,
    }
    db.upsertMerge(this.db, 'tool_calls', 'merge_key', row)
    // Record which signals contributed, without losing earlier ones.
    this.db.run(
      'UPDATE tool_calls SET sources = CASE WHEN sources LIKE ? THEN sources' +
        " ELSE sources || '+' || ? END WHERE merge_key=?",
      [`%${origin}%`, origin, mergeKey],
    )

    if (row.success === 0) {
      db.insertIgnore(this.db, 'errors', {
        dedupe_key: `toolfail|${mergeKey}`,
        session_id: sid,
        ts: ctx.ts,
        kind: 'tool_failure',
        source_event: origin,
        tool_name: toolName,
        error_name: row.error_type,
        message: row.error_message,
      })
    }

    this.deriveActivity(row)
  }

  private deriveActivity(row: Record<string, any>): void {
    const sid = row.session_id
    const tuid = row.tool_use_id
    const key = row.merge_key
    const toolName = row.tool_name as string | null

    const op = toolName ? FILE_TOOLS[toolName] : undefined
    if (op && row.file_path) {
      const p = String(row.file_path)
      db.upsertMerge(this.db, 'file_activity', 'merge_key', {
        merge_key: `fa|${key}`,
        session_id: sid,
        ts: row.ts,
        tool_use_id: tuid,
        tool_name: toolName,
        operation: op,
        path: p,
        file_ext: extname(p).toLowerCase() || null,
        success: row.success,
        via: 'tool',
        op_confidence: 'high',
      })
    }

    if (toolName && SHELL_TOOLS.has(toolName) && row.bash_command) {
      const cmd = String(row.bash_command)
      const progs = bashPrograms(cmd)
      db.upsertMerge(this.db, 'bash_activity', 'merge_key', {
        merge_key: `ba|${key}`,
        session_id: sid,
        ts: row.ts,
        tool_use_id: tuid,
        command: cmd,
        command_hash: hashText(cmd),
        program: (primaryProgram(progs) || '').slice(0, 64) || null,
        programs: progs.length ? dumps(progs) : null,
        success: row.success,
        duration_ms: row.duration_ms,
        error_type: row.error_type,
      })
    }

    if (row.skill_name) {
      db.upsertMerge(this.db, 'skill_calls', 'merge_key', {
        merge_key: `sk|${key}`,
        session_id: sid,
        ts: row.ts,
        skill_name: row.skill_name,
        invocation_source: 'tool_call',
        tool_use_id: tuid,
        success: row.success,
        duration_ms: row.duration_ms,
      })
    }

    if (row.subagent_type || (toolName && SUBAGENT_TOOLS.has(toolName))) {
      db.upsertMerge(this.db, 'subagent_activity', 'merge_key', {
        merge_key: `sa|${key}`,
        session_id: sid,
        ts: row.ts,
        subagent_type: row.subagent_type,
        tool_use_id: tuid,
        success: row.success,
        duration_ms: row.duration_ms,
        source: 'tool_call',
      })
    }
  }

  /** Re-parse every stored bash command for file access.
   *
   * Deliberately a rebuild rather than an incremental step: the parser is a
   * heuristic that will keep improving, and rebuilding from the stored
   * commands means those improvements apply to history without re-reading the
   * raw archive. Tool-derived rows are untouched.
   */
  private deriveShellFiles(): void {
    this.db.run("DELETE FROM file_activity WHERE via='shell'")
    const rows = this.db
      .all(`SELECT b.bash_activity_id, b.session_id, b.ts, b.tool_use_id,
              b.command, b.success,
              COALESCE(s.cwd, g.repo_root, g.cwd) AS base
         FROM bash_activity b
         LEFT JOIN sessions s ON s.session_id = b.session_id
         LEFT JOIN local_session_git_context g ON g.session_id = b.session_id
        WHERE b.command IS NOT NULL`)
    for (const r of rows) {
      const base =
        shellfiles.baseDir(r.command as string) || (r.base as string | null)
      const parsed = shellfiles.parse(r.command as string)
      for (let i = 0; i < parsed.length; i++) {
        const [path, op, conf] = parsed[i]!
        // `~` is the user's home, not a directory to append to cwd: joining it
        // produced paths like `/repo/~/Workspace/...`.
        let resolved: string
        if (path.startsWith('~')) resolved = expandUser(path)
        else if (!path.startsWith('/') && base) resolved = pathJoin(base, path)
        else resolved = path
        db.insertIgnore(this.db, 'file_activity', {
          merge_key: `fa|sh|${r.bash_activity_id}|${i}`,
          session_id: r.session_id,
          ts: r.ts,
          tool_use_id: r.tool_use_id,
          tool_name: 'Bash',
          operation: op,
          path: resolved,
          file_ext: extname(resolved).toLowerCase() || null,
          success: r.success,
          via: 'shell',
          op_confidence: conf,
        })
      }
    }
    this.db.checkpoint()
  }

  /** Skill attribution that arrives on API events rather than tool events. */
  private attributeSkill(attrs: Attrs, ctx: Ctx, source: string): void {
    const name = attrs['skill.name'] as string | undefined
    if (!name || name === 'custom') return
    db.upsertMerge(this.db, 'skill_calls', 'merge_key', {
      merge_key: `sk|api|${ctx.dk}`,
      session_id: ctx.session_id,
      ts: ctx.ts,
      skill_name: name,
      invocation_source: source,
    })
    if (attrs['agent.name']) {
      db.upsertMerge(this.db, 'subagent_activity', 'merge_key', {
        merge_key: `sa|api|${ctx.dk}`,
        session_id: ctx.session_id,
        ts: ctx.ts,
        agent_name: attrs['agent.name'] as string,
        source,
      })
    }
  }

  // -- metrics -------------------------------------------------------------

  private handleMetric(
    pt: otlp.MetricPoint,
    path: string,
    lineno: number,
    _idx: number,
  ): void {
    const attrs = pt.attributes
    const sid = this.trackSession(attrs, pt.resource)
    const ts = otlp.nsToIso(pt.ts_ns)
    const dk = `mp|${pt.metric_name}|${pt.ts_ns}|${attrHash(attrs)}`

    db.insertIgnore(this.db, 'metric_points', {
      dedupe_key: dk,
      metric_name: pt.metric_name,
      kind: pt.kind,
      unit: pt.unit || null,
      value: pt.value,
      session_id: sid,
      ts,
      ts_ns: pt.ts_ns,
      attrs_json: dumps(scrubDeep(attrs)),
      raw_json: dumps(pt.point),
      source_path: path,
      source_line: lineno,
    })
    if (pt.metric_name === 'claude_code.session.count' && sid) {
      const st = attrs.start_type
      if (st) {
        let cur = this.sessionAttrs.get(sid)
        if (!cur) {
          cur = { resource: {} }
          this.sessionAttrs.set(sid, cur)
        }
        cur.start_type = st
      }
    }
  }

  // -- spans ---------------------------------------------------------------

  private handleSpan(
    sp: otlp.SpanRecord,
    path: string,
    lineno: number,
    _idx: number,
  ): void {
    const attrs = sp.attributes
    const sid = this.trackSession(attrs, sp.resource)
    const tuid = (attrs.tool_use_id ?? attrs['gen_ai.tool.call.id']) as
      string | null
    const dk = `sp|${sp.trace_id}|${sp.span_id}`

    const safe: Attrs = {}
    const contentKeys = [
      'user_prompt',
      'system_prompt_preview',
      'user_system_prompt',
      'response.model_output',
      'tool_input',
    ]
    for (const [k, v] of Object.entries(attrs)) {
      if (contentKeys.includes(k) && !config.STORE_CONTENT) {
        safe[k] =
          v !== null && v !== undefined && v !== '' ? '[CONTENT NOT STORED]' : v
      } else {
        safe[k] = scrubDeep(v)
      }
    }

    // Span events carry tool input/output bodies when OTEL_LOG_TOOL_CONTENT is
    // on. Keep names and sizes always; keep bodies only when allowed.
    let spanEvents: string | null = null
    const rawEvents = (sp.span.events ?? []) as Record<string, any>[]
    if (rawEvents.length) {
      const collected = rawEvents.map((ev) => {
        const evAttrs = otlp.attrs(ev.attributes)
        let payload: unknown
        if (config.STORE_SPAN_EVENTS) {
          payload = scrubDeep(evAttrs)
        } else {
          const trimmed: Attrs = {}
          for (const [k, v] of Object.entries(evAttrs)) {
            trimmed[k] =
              typeof v === 'string' && String(v).length > 64
                ? `<${String(v).length} chars not stored>`
                : scrubDeep(v)
          }
          payload = trimmed
        }
        return {
          name: ev.name,
          time: otlp.nsToIso(ev.timeUnixNano),
          attributes: payload,
        }
      })
      spanEvents = dumps(collected)
    }

    const inserted = db.insertIgnore(this.db, 'spans', {
      dedupe_key: dk,
      trace_id: sp.trace_id,
      span_id: sp.span_id,
      parent_span_id: sp.parent_span_id,
      name: sp.name,
      session_id: sid,
      start_ts: otlp.nsToIso(sp.start_ns),
      end_ts: otlp.nsToIso(sp.end_ns),
      start_ns: sp.start_ns,
      duration_ms: sp.duration_ms,
      status_code: sp.status ? String(sp.status) : null,
      tool_use_id: tuid,
      attrs_json: dumps(safe),
      span_events: spanEvents,
      raw_json: dumps(sp.span),
      source_path: path,
      source_line: lineno,
    })
    if (inserted === null) return

    // Spans carry file_path / full_command / skill_name / subagent_type
    // directly (gated by OTEL_LOG_TOOL_DETAILS), so use them to enrich the
    // tool_calls row that the events pipeline created.
    if (
      (sp.name === 'claude_code.tool' ||
        sp.name === 'claude_code.tool.execution') &&
      sid &&
      tuid
    ) {
      const ts = otlp.nsToIso(sp.start_ns)
      const mergeKey = `tc|${sid}|${tuid}`
      const toolName = (attrs.tool_name as string) ?? null
      const cmd = attrs.full_command
        ? scrub(attrs.full_command as string)
        : null
      const row: Record<string, db.Bindable> = {
        merge_key: mergeKey,
        session_id: sid,
        ts,
        ts_ns: sp.start_ns,
        tool_use_id: tuid,
        tool_name: toolName,
        tool_category: toolName ? toolCategory(toolName) : null,
        duration_ms: sp.duration_ms,
        result_tokens: asInt(attrs.result_tokens),
        success: asBoolInt(attrs.success),
        file_path: (attrs.file_path as string) ?? null,
        bash_command: cmd,
        skill_name: (attrs.skill_name as string) ?? null,
        subagent_type: (attrs.subagent_type as string) ?? null,
        agent_id: (attrs.agent_id as string) ?? null,
        parent_agent_id: (attrs.parent_agent_id as string) ?? null,
        workflow_run_id: (attrs['workflow.run_id'] as string) ?? null,
        sources: 'span',
      }
      db.upsertMerge(this.db, 'tool_calls', 'merge_key', row)
      this.db.run(
        "UPDATE tool_calls SET sources = CASE WHEN sources LIKE '%span%'" +
          " THEN sources ELSE sources || '+span' END WHERE merge_key=?",
        [mergeKey],
      )
      const full = this.db.get('SELECT * FROM tool_calls WHERE merge_key=?', [
        mergeKey,
      ])
      if (full) this.deriveActivity(full)
    }
  }

  // -- session / project resolution ---------------------------------------

  /** Load git context recorded by the optional SessionStart/End hooks. */
  private importSessionContext(): void {
    const path = config.SESSION_CONTEXT_FILE
    if (!existsSync(path)) return
    for (const [, rawLine] of readLines(path)) {
      const line = rawLine.trim()
      if (!line) continue
      let r: Record<string, any>
      try {
        r = JSON.parse(line)
      } catch {
        continue
      }
      const sid = r.session_id
      if (!sid) continue
      this.db.run(
        'INSERT OR REPLACE INTO local_session_git_context' +
          '(session_id, phase, captured_at, cwd, repo_root, remote_url,' +
          ' branch, head_sha, is_dirty) VALUES (?,?,?,?,?,?,?,?,?)',
        [
          sid,
          r.phase ?? 'start',
          r.captured_at ?? null,
          r.cwd ?? null,
          r.repo_root ?? null,
          r.remote_url ?? null,
          r.branch ?? null,
          r.head_sha ?? null,
          r.is_dirty ?? null,
        ],
      )
      this.db.run(
        'INSERT OR IGNORE INTO sessions(session_id, cwd) VALUES (?,?)',
        [sid, r.cwd ?? null],
      )
      this.db.run(
        'UPDATE sessions SET cwd=COALESCE(cwd,?) WHERE session_id=?',
        [r.cwd ?? null, sid],
      )
    }
    this.db.commit()
  }

  private flushSessions(): void {
    for (const [sid, a] of this.sessionAttrs) {
      let paths = a['workspace.host_paths']
      if (typeof paths === 'string') paths = [paths]
      const resourceBag = a.resource as Attrs
      db.upsertMerge(this.db, 'sessions', 'session_id', {
        session_id: sid,
        start_type: (a.start_type as string) ?? null,
        user_id: (a['user.id'] as string) ?? null,
        user_email: (a['user.email'] as string) ?? null,
        account_uuid: (a['user.account_uuid'] as string) ?? null,
        account_id: (a['user.account_id'] as string) ?? null,
        organization_id: (a['organization.id'] as string) ?? null,
        app_version: (a['app.version'] as string) ?? null,
        app_entrypoint: (a['app.entrypoint'] as string) ?? null,
        terminal_type: (a['terminal.type'] as string) ?? null,
        workspace_paths: paths ? dumps(paths) : null,
        resource_attrs:
          resourceBag && Object.keys(resourceBag).length
            ? dumps(resourceBag)
            : null,
      })
    }
    // Sessions that only ever appeared on metrics/spans still need a row.
    this.db.run(
      'INSERT OR IGNORE INTO sessions(session_id)' +
        ' SELECT DISTINCT session_id FROM events WHERE session_id IS NOT NULL',
    )
    this.db.run(
      'INSERT OR IGNORE INTO sessions(session_id)' +
        ' SELECT DISTINCT session_id FROM metric_points WHERE session_id IS NOT NULL',
    )
    this.db.commit()
  }

  private ensureProject(desc: gitctx.ProjectDesc, method: string): string {
    db.upsertMerge(this.db, 'projects', 'project_id', {
      project_id: desc.project_id,
      project_name: desc.project_name,
      repo_root: desc.repo_root ?? null,
      remote_url: desc.remote_url ?? null,
      remote_normalized: desc.remote_normalized ?? null,
      is_git: desc.is_git ?? 0,
      detection_method: method,
      first_seen: utcNow(),
      last_seen: utcNow(),
    })
    this.db.run('UPDATE projects SET last_seen=? WHERE project_id=?', [
      utcNow(),
      desc.project_id,
    ])
    return desc.project_id
  }

  /** Attach every session to a project, best source first. */
  private resolveProjects(): void {
    this.ensureProject({ ...gitctx.UNKNOWN_PROJECT }, 'none')

    const rows = this.db.all(
      'SELECT session_id, workspace_paths, cwd, project_id FROM sessions',
    )
    for (const r of rows) {
      if (r.project_id) continue
      let candidate: string | null = null
      let method: string | null = null

      const hook = this.db.get(
        'SELECT repo_root, remote_url, cwd FROM local_session_git_context' +
          " WHERE session_id=? ORDER BY phase='start' DESC LIMIT 1",
        [r.session_id],
      )
      if (hook && (hook.repo_root || hook.cwd)) {
        candidate = (hook.repo_root as string) || (hook.cwd as string)
        method = 'session_hook'
      }

      if (!candidate && r.cwd) {
        candidate = String(r.cwd)
        method = 'session_cwd'
      }

      if (!candidate && r.workspace_paths) {
        let paths: string[] = []
        try {
          paths = JSON.parse(String(r.workspace_paths))
        } catch {
          paths = []
        }
        if (paths.length) {
          candidate = paths[0]!
          method = 'workspace.host_paths'
        }
      }

      if (!candidate) {
        // Claude Code 2.1.237 does not actually emit workspace.host_paths, so
        // fall back to inferring the repository from absolute paths seen in
        // tool arguments and bash commands.
        const root = this.inferRepoRoot(String(r.session_id))
        if (root) {
          candidate = root
          method = 'path_inference'
        }
      }

      if (candidate) {
        // Records what the checkout was sitting on, so it wants the live
        // state, not just the project.
        const desc = gitctx.describeFull(candidate)
        const pid = this.ensureProject(desc, method!)
        this.db.run(
          'UPDATE sessions SET project_id=?, cwd=COALESCE(cwd,?),' +
            ' project_detection_method=? WHERE session_id=?',
          [pid, candidate, method, r.session_id],
        )
        if (desc.is_git) {
          this.db.run(
            'INSERT OR REPLACE INTO local_session_git_context' +
              '(session_id, phase, captured_at, cwd, repo_root, remote_url,' +
              ' branch, head_sha, is_dirty) VALUES (?,?,?,?,?,?,?,?,?)',
            [
              r.session_id,
              'observed',
              utcNow(),
              candidate,
              desc.repo_root ?? null,
              desc.remote_url ?? null,
              desc.branch ?? null,
              desc.head_sha ?? null,
              desc.is_dirty ?? null,
            ],
          )
        }
      } else {
        this.db.run(
          "UPDATE sessions SET project_id=?, project_detection_method='none' WHERE session_id=?",
          [gitctx.UNKNOWN_PROJECT.project_id, r.session_id],
        )
      }
    }
    this.db.commit()
  }

  /** Absolute paths this session touched, from any signal. */
  private candidatePaths(sessionId: string): string[] {
    const out: string[] = []
    for (const sql of [
      "SELECT path AS v FROM file_activity WHERE session_id=? AND path LIKE '/%'",
      "SELECT file_path AS v FROM tool_calls WHERE session_id=? AND file_path LIKE '/%'",
    ]) {
      for (const r of this.db.all(sql, [sessionId])) out.push(String(r.v))
    }
    for (const r of this.db.all(
      'SELECT command FROM bash_activity WHERE session_id=? AND command IS NOT NULL',
      [sessionId],
    )) {
      const command = String(r.command)
      ABS_PATH_RE.lastIndex = 0
      let m: RegExpExecArray | null
      while ((m = ABS_PATH_RE.exec(command)) !== null) out.push(m[0])
    }
    return out
  }

  /** Most frequently referenced git repository for a session. */
  private inferRepoRoot(sessionId: string): string | null {
    const counts = new Map<string, number>()
    for (const path of this.candidatePaths(sessionId).slice(0, 200)) {
      if (path.length < 5) continue
      const desc = this.projectOf(path)
      if (desc.is_git && desc.repo_root) {
        counts.set(desc.repo_root, (counts.get(desc.repo_root) ?? 0) + 1)
      }
    }
    if (!counts.size) return null
    let best: string | null = null
    let bestN = -1
    for (const [root, n] of counts) {
      if (n > bestN) {
        best = root
        bestN = n
      }
    }
    return best
  }

  /** Drop file activity that matches an ignore pattern.
   *
   * Applied here rather than at write time so that changing the patterns takes
   * effect on the next analyse, whatever produced the row - tool events are
   * not re-derivable without re-reading the raw archive.
   */
  private applyFileIgnores(): void {
    const ignored = config.ignoreFilter()
    const paths = this.db
      .all('SELECT DISTINCT path FROM file_activity WHERE path IS NOT NULL')
      .map((r) => String(r.path))
    const doomed = paths.filter(ignored)
    for (let start = 0; start < doomed.length; start += 400) {
      const chunk = doomed.slice(start, start + 400)
      const marks = chunk.map(() => '?').join(',')
      this.db.run(`DELETE FROM file_activity WHERE path IN (${marks})`, chunk)
    }
    if (doomed.length)
      db.setMeta(this.db, 'ignored_paths', String(doomed.length))
    this.db.commit()
  }

  /** Stamp project_id on every row whose session now has one.
   *
   * This cannot happen at insert time. A session is mapped to a repository by
   * the weight of the paths it referenced across its whole life, so a record
   * ingested in the first batch does not know what the fortieth batch will
   * decide. The back-fill is the price of getting the mapping right rather
   * than early.
   *
   * What it does not have to be is a scan. Each of these tables carries a
   * partial index over `project_id IS NULL`, so a row is in the index only
   * while it is waiting - and once everything is assigned the UPDATEs probe an
   * empty index instead of reading every row.
   */
  private propagateProjectIds(): void {
    const tables = [
      'events',
      'metric_points',
      'spans',
      'api_calls',
      'tool_calls',
      'skill_calls',
      'file_activity',
      'bash_activity',
      'subagent_activity',
      'prompts',
      'responses',
      'errors',
    ]
    for (const t of tables) {
      this.db.run(
        `UPDATE ${t} SET project_id = (` +
          ` SELECT s.project_id FROM sessions s WHERE s.session_id = ${t}.session_id` +
          `) WHERE project_id IS NULL AND session_id IS NOT NULL`,
      )
    }

    // A file can live outside the session's project; prefer its own repo.
    const rows = this.db.all(
      'SELECT DISTINCT path FROM file_activity WHERE path IS NOT NULL AND repo_relative_path IS NULL',
    )
    for (const r of rows) {
      const path = String(r.path)
      const desc = this.projectOf(path)
      if (!desc.is_git) continue
      this.ensureProject(desc, 'file_path')
      const root = desc.repo_root!
      let rel: string | null = null
      try {
        const rp = relative(pyResolve(root), pyResolve(path))
        rel =
          rp === '' ? '.' : rp.startsWith('..') || isAbsolute(rp) ? null : rp
      } catch {
        rel = null
      }
      this.db.run(
        'UPDATE file_activity SET project_id=?, repo_relative_path=? WHERE path=?',
        [desc.project_id, rel, path],
      )
    }
    this.db.checkpoint()
  }

  private finaliseSessions(): void {
    this.db.run(`
      UPDATE sessions SET
        first_seen = (SELECT MIN(ts) FROM events e WHERE e.session_id = sessions.session_id AND e.ts IS NOT NULL),
        last_seen  = (SELECT MAX(ts) FROM events e WHERE e.session_id = sessions.session_id AND e.ts IS NOT NULL)
      WHERE EXISTS (SELECT 1 FROM events e WHERE e.session_id = sessions.session_id AND e.ts IS NOT NULL)
    `)
    // Fall back to metric timestamps for metrics-only sessions.
    this.db.run(`
      UPDATE sessions SET
        first_seen = COALESCE(first_seen, (SELECT MIN(ts) FROM metric_points m WHERE m.session_id = sessions.session_id)),
        last_seen  = COALESCE(last_seen,  (SELECT MAX(ts) FROM metric_points m WHERE m.session_id = sessions.session_id))
    `)
    this.db.run(`
      UPDATE sessions
         SET duration_s = (julianday(last_seen) - julianday(first_seen)) * 86400.0
       WHERE first_seen IS NOT NULL AND last_seen IS NOT NULL
    `)
    this.db.checkpoint()
  }

  // -- derived: created vs modified ---------------------------------------

  /** Classify write operations as create vs modify.
   *
   * Method (documented in README):
   *   1. git evidence - if the file's first `A` (added) commit in the
   *      repository is at or after the session start, treat as created
   *      (confidence: high).
   *   2. absence of a prior read - a Write with no earlier Read/Edit of the
   *      same path anywhere in the database before that moment
   *      (confidence: medium).
   *   3. otherwise unknown.
   */
  private deriveFileCreation(): void {
    const rows = this.db
      .all(`SELECT f.file_activity_id, f.path, f.ts, f.session_id, s.first_seen
         FROM file_activity f
         LEFT JOIN sessions s ON s.session_id = f.session_id
        WHERE f.operation IN ('write','notebook_edit') AND f.created IS NULL`)
    const cache = new Map<string, string | null>()
    for (const r of rows) {
      const path = String(r.path)
      const ts = r.ts as string | null
      let created: number | null = null
      let method = 'unknown'
      let conf = 'none'

      const desc = this.projectOf(path)
      const root = desc.repo_root
      if (root) {
        if (!cache.has(path)) cache.set(path, this.firstAdded(root, path))
        const addedAt = cache.get(path)!
        const sessionStart = (r.first_seen as string | null) ?? ts
        if (addedAt && sessionStart && addedAt >= sessionStart.slice(0, 19)) {
          created = 1
          method = 'git_added'
          conf = 'high'
        } else if (addedAt) {
          created = 0
          method = 'git_added'
          conf = 'high'
        }
      }

      if (created === null) {
        const prior = db.scalar(
          this.db,
          `SELECT COUNT(*) FROM file_activity
            WHERE path = ? AND ts < ? AND operation IN ('read','edit')`,
          [path, ts ?? ''],
          0,
        )
        created = prior === 0 ? 1 : 0
        method = 'no_prior_read'
        conf = 'medium'
      }

      this.db.run(
        'UPDATE file_activity SET created=?, create_method=?, create_confidence=? WHERE file_activity_id=?',
        [created, method, conf, r.file_activity_id],
      )
    }
    this.db.checkpoint()
  }

  /** `gitctx.describe`, with the answer kept in the database.
   *
   * Attribution asks the same question of the same directories on every run,
   * and each miss is a `git` subprocess. Positive answers are kept for good;
   * "not a repository" is re-checked after a day, since a directory can be
   * `git init`ed later.
   */
  private projectOf(path: string): gitctx.ProjectDesc {
    const directory = gitctx.nearestDir(path)
    if (directory === null) return { ...gitctx.UNKNOWN_PROJECT, cwd: path }
    const row = this.db.get(
      'SELECT is_git, repo_root, remote_url, checked_at FROM git_dir_cache WHERE dir=?',
      [directory],
    )
    if (
      row !== undefined &&
      (row.is_git || isFresh(row.checked_at as string | null, DIR_CACHE_TTL_S))
    ) {
      const root = (row.repo_root as string | null) ?? null
      const remote = (row.remote_url as string | null) ?? null
      return {
        project_id: gitctx.projectId(root, remote, directory),
        project_name: gitctx.projectName(root, remote, directory),
        repo_root: root,
        remote_url: remote,
        remote_normalized: gitctx.normalizeRemote(remote),
        is_git: row.is_git ? 1 : 0,
        cwd: directory,
      }
    }
    const desc = gitctx.describe(path)
    this.db.run(
      'INSERT OR REPLACE INTO git_dir_cache(dir, is_git, repo_root, remote_url, checked_at) VALUES (?,?,?,?,?)',
      [
        directory,
        desc.is_git ?? 0,
        desc.repo_root ?? null,
        desc.remote_url ?? null,
        utcNow(),
      ],
    )
    return desc
  }

  /** When git first saw this path, through a cache.
   *
   * The subprocess behind this is the slowest thing in an incremental analyse,
   * and shell-derived rows are rebuilt every run, so the same paths would be
   * probed over and over. A found commit is cached for good; "not committed
   * yet" is re-checked after a few hours.
   */
  private firstAdded(root: string, path: string): string | null {
    const hit = this.db.get(
      'SELECT added_at, checked_at FROM git_first_added WHERE repo_root=? AND path=?',
      [root, path],
    )
    if (
      hit &&
      (hit.added_at ||
        isFresh(hit.checked_at as string | null, ADDED_CACHE_TTL_S))
    ) {
      return (hit.added_at as string | null) ?? null
    }
    const addedAt = gitctx.fileFirstAdded(root, path)
    this.db.run(
      'INSERT OR REPLACE INTO git_first_added(repo_root, path, added_at, checked_at) VALUES (?,?,?,?)',
      [root, path, addedAt, utcNow()],
    )
    return addedAt
  }

  // -- derived: work streams ----------------------------------------------

  /** Parse conventional-commit type and scope out of commit subjects.
   *
   * These used to group commits into work streams spanning sessions. The
   * session is the unit now, so the parsed values are kept as what they are -
   * a label on a commit, and the fallback name for a session that has no
   * description yet.
   */
  private classifyCommits(): void {
    for (const r of this.db.all(
      'SELECT commit_sha, subject FROM git_activity',
    )) {
      const subject = String(r.subject ?? '').trim()
      const m = CONVENTIONAL.exec(subject)
      const ctype = m ? m.groups!.type!.toLowerCase() : null
      const scope =
        m && m.groups!.scope ? m.groups!.scope!.trim().toLowerCase() : null
      this.db.run(
        'UPDATE git_activity SET commit_type=?, commit_scope=? WHERE commit_sha=?',
        [ctype, scope, r.commit_sha],
      )
    }
    this.detectReverts()
    this.db.commit()
  }

  /** A revert is the clearest evidence that work had to be undone. */
  private detectReverts(): void {
    const bySubject = new Map<string, string>()
    for (const r of this.db.all(
      'SELECT commit_sha, subject, project_id FROM git_activity',
    )) {
      const key = `${r.project_id}\u0000${String(r.subject ?? '').trim()}`
      if (!bySubject.has(key)) bySubject.set(key, String(r.commit_sha))
    }
    for (const r of this.db.all(
      'SELECT commit_sha, subject, project_id, committed_at FROM git_activity',
    )) {
      const m = REVERT.exec(String(r.subject ?? '').trim())
      if (!m) continue
      const target =
        bySubject.get(`${r.project_id}\u0000${m.groups!.subject!.trim()}`) ??
        null
      db.insertIgnore(this.db, 'reverts', {
        revert_sha: r.commit_sha,
        project_id: r.project_id,
        reverted_sha: target,
        detected_at: r.committed_at,
        method: 'commit_subject',
      })
    }
  }

  // -- derived: human effort per turn --------------------------------------

  /** Return [isSteering, isCorrection, cue].
   *
   * The two labels are mutually exclusive and correction wins, so the counts,
   * the wall-clock totals and the strip colours all agree. A slash command
   * (`/exit`, `/compact`) is a dispatch, not a typed instruction, so it is
   * never a steering nudge.
   */
  private classifyPrompt(
    text: string | null,
    length: number | null,
    commandName: string | null = null,
  ): [number, number, string | null] {
    const short =
      length !== null && length <= STEERING_MAX_CHARS && !commandName
    if (text) {
      const low = text.trim().toLowerCase()
      for (const cue of CORRECTION_CUES) {
        if (low.startsWith(cue) || low.slice(0, 160).includes(` ${cue}`)) {
          return [0, 1, cue.trim()]
        }
      }
    }
    return [short ? 1 : 0, 0, null]
  }

  /** Build one row per prompt.id with the work it caused.
   *
   * prompt.id is stamped on every event a turn produces, which makes it the
   * natural join for "what did this one human instruction cost".
   */
  private deriveTurns(): void {
    this.db.run('DELETE FROM turns')
    const prompts = this.db
      .all(`SELECT p.prompt_id, p.session_id, p.project_id, p.ts, p.prompt_length,
              p.prompt_text, p.command_name
         FROM prompts p
        WHERE p.prompt_id IS NOT NULL
        ORDER BY p.session_id, p.ts`)
    const prevEnd = new Map<string, string>()
    const seq = new Map<string, number>()

    for (const r of prompts) {
      const tid = String(r.prompt_id)
      const sid = String(r.session_id)
      seq.set(sid, (seq.get(sid) ?? 0) + 1)

      const agg = this.db.get(
        `SELECT MAX(ts) AS ended,
                SUM(CASE WHEN event_name='api_request' THEN 1 ELSE 0 END) AS api
           FROM events WHERE prompt_id=?`,
        [tid],
      )
      const cost = db.scalar<number>(
        this.db,
        "SELECT COALESCE(SUM(cost_usd),0) FROM api_calls WHERE prompt_id=? AND outcome='ok'",
        [tid],
        0,
      )
      const tools = this.db.get(
        `SELECT COUNT(*) AS n,
                SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN decision='reject' THEN 1 ELSE 0 END) AS rejects,
                SUM(CASE WHEN decision_source IN ('user_reject','user_abort')
                         THEN 1 ELSE 0 END) AS overrides,
                COUNT(DISTINCT file_path) AS files
           FROM tool_calls WHERE prompt_id=?`,
        [tid],
      )

      const ended = (agg?.ended as string | null) ?? null
      const started = (r.ts as string | null) ?? null
      const dur = secondsBetween(started, ended)
      const gap = secondsBetween(prevEnd.get(sid) ?? null, started)
      if (ended) prevEnd.set(sid, ended)

      const text = (r.prompt_text as string | null) ?? null
      const isSystem = text && SYSTEM_PROMPT_RE.test(text) ? 1 : 0
      let [steering, correction, cue] = this.classifyPrompt(
        text,
        (r.prompt_length as number | null) ?? null,
        (r.command_name as string | null) ?? null,
      )
      if (isSystem) {
        steering = 0
        correction = 0
        cue = null
      }

      db.insertIgnore(this.db, 'turns', {
        turn_id: tid,
        session_id: sid,
        project_id: r.project_id,
        seq: seq.get(sid)!,
        started_at: started,
        ended_at: ended,
        duration_s: dur,
        gap_before_s: gap,
        prompt_length: r.prompt_length,
        prompt_text: r.prompt_text,
        is_steering: steering,
        is_correction: correction,
        correction_cue: cue,
        is_system: isSystem,
        api_calls: (agg?.api as number) || 0,
        cost_usd: cost,
        tool_calls: (tools?.n as number) || 0,
        tool_failures: (tools?.failed as number) || 0,
        rejects: (tools?.rejects as number) || 0,
        user_overrides: (tools?.overrides as number) || 0,
        files_touched: (tools?.files as number) || 0,
      })
    }

    this.deriveRework()
    this.db.checkpoint()
  }

  /** Files returned to across separate turns within a session. */
  private deriveRework(): void {
    this.db.run('DELETE FROM file_rework')
    this.db.run(`
      INSERT INTO file_rework
        (session_id, project_id, path, repo_relative_path, turns, edits,
         first_ts, last_ts)
      SELECT t.session_id, MAX(f.project_id), f.path,
             MAX(f.repo_relative_path),
             COUNT(DISTINCT t.prompt_id), COUNT(*),
             MIN(f.ts), MAX(f.ts)
        FROM file_activity f
        JOIN tool_calls t ON t.tool_use_id = f.tool_use_id
                         AND t.session_id = f.session_id
       WHERE f.path IS NOT NULL AND t.prompt_id IS NOT NULL
       GROUP BY t.session_id, f.path
      HAVING COUNT(DISTINCT t.prompt_id) > 1
    `)
    this.db.commit()
  }

  /** Which session produced this commit?
   *
   * Time alone is a poor signal: sessions run concurrently, so "the one that
   * started most recently" credited a commit to a session that never opened
   * either changed file. Prefer the session that actually touched the files
   * the commit changed, and fall back to the time window only when no file
   * evidence exists.
   *
   * Takes paths rather than a git log record, because this is recomputed from
   * `git_commit_files` long after the commit was first seen.
   */
  private attributeCommit(
    projectId: string,
    paths: string[],
    committedAt: string | null,
  ): [string | null, string] {
    if (paths.length) {
      const union =
        'SELECT ? AS p' + ' UNION ALL SELECT ?'.repeat(paths.length - 1)
      const row = this.db.get(
        `SELECT f.session_id, COUNT(*) touches
           FROM file_activity f
          WHERE f.session_id IS NOT NULL
            AND EXISTS (SELECT 1 FROM (${union})
                         WHERE f.path LIKE '%/' || p)
          GROUP BY f.session_id ORDER BY touches DESC LIMIT 1`,
        paths,
      )
      if (row && row.touches) return [String(row.session_id), 'changed_files']
    }

    const row = this.db.get(
      'SELECT session_id FROM sessions WHERE project_id=?' +
        ' AND first_seen <= ? AND (last_seen >= ? OR last_seen IS NULL)' +
        ' ORDER BY first_seen DESC LIMIT 1',
      [projectId, committedAt, committedAt],
    )
    return row
      ? [String(row.session_id), 'session_time_window']
      : [null, 'none']
  }

  /** Record commits made in observed repositories during observed sessions.
   *
   * Deliberately two passes, because the two halves have different lifetimes.
   *
   * A commit's sha, timestamp, author and file list are **immutable**. Once
   * recorded there is nothing to learn by asking git again - and asking costs
   * a `git show` subprocess per commit, which over a week of history is most
   * of what this pass used to spend.
   *
   * Which session *made* it is **not** immutable. Attribution prefers the
   * session that touched the changed files, and that evidence keeps arriving:
   * a session still running, or one whose spans land in a later batch, can
   * turn an unattributed commit into an attributed one. So it is recomputed
   * for every commit on every analyse - from stored rows, with no subprocess
   * at all.
   */
  private reconcileGit(): void {
    const projects = this.db.all(
      'SELECT project_id, repo_root FROM projects WHERE is_git=1 AND repo_root IS NOT NULL',
    )
    for (const p of projects) {
      const window = this.db.get(
        'SELECT MIN(first_seen) AS a, MAX(last_seen) AS b FROM sessions WHERE project_id=?',
        [p.project_id],
      )
      if (!window || !window.a) continue
      // A commit counts as recorded only when its file rows are there too; a
      // run interrupted between the two inserts is repaired rather than left
      // half-written.
      const known = new Set(
        this.db
          .all(
            'SELECT g.commit_sha FROM git_activity g WHERE g.project_id=?' +
              '  AND EXISTS (SELECT 1 FROM git_commit_files f' +
              '               WHERE f.commit_sha = g.commit_sha)',
            [p.project_id],
          )
          .map((r) => String(r.commit_sha)),
      )
      for (const c of gitctx.commitsSince(
        String(p.repo_root),
        String(window.a),
        null,
      )) {
        if (known.has(c.commit_sha)) continue
        db.insertIgnore(this.db, 'git_activity', {
          dedupe_key: `git|${p.project_id}|${c.commit_sha}`,
          project_id: p.project_id,
          commit_sha: c.commit_sha,
          committed_at: c.committed_at,
          author_name: c.author_name,
          author_email: c.author_email,
          subject: scrub(c.subject),
          files_changed: c.files_changed,
          insertions: c.insertions,
          deletions: c.deletions,
          attribution: 'pending',
          source: 'local_git_reconcile',
        })
        const types = gitctx.commitChangeTypes(
          String(p.repo_root),
          c.commit_sha,
        )
        for (const f of c.files) {
          db.insertIgnore(this.db, 'git_commit_files', {
            commit_sha: c.commit_sha,
            project_id: p.project_id,
            path: f.path,
            change_type: types[f.path] ?? null,
            insertions: f.insertions,
            deletions: f.deletions,
          })
        }
      }
    }
    this.db.commit()
    this.attributeCommits(projects)
  }

  /** Re-decide which session made each commit, from stored rows only.
   *
   * Runs over every observed commit, not just new ones: the whole point is
   * that a commit recorded before its session's file activity arrived can be
   * corrected once it does.
   */
  private attributeCommits(projects: Row[]): void {
    for (const p of projects) {
      for (const c of this.db.all(
        'SELECT commit_sha, committed_at, session_id, attribution FROM git_activity WHERE project_id=?',
        [p.project_id],
      )) {
        // Matched on sha alone. A sha identifies a commit globally, and the
        // same repository can hold more than one project row - a `root:` id
        // from before it had a remote, and a `remote:` one after - so the
        // files of a commit are not always filed under the id its
        // git_activity row carries.
        const paths = this.db
          .all('SELECT path FROM git_commit_files WHERE commit_sha=?', [
            c.commit_sha,
          ])
          .map((r) => String(r.path))
        const [sess, how] = this.attributeCommit(
          String(p.project_id),
          paths,
          (c.committed_at as string | null) ?? null,
        )
        if (sess !== (c.session_id ?? null) || how !== c.attribution) {
          this.db.run(
            'UPDATE git_activity SET session_id=?, attribution=? WHERE commit_sha=? AND project_id=?',
            [sess, how, c.commit_sha, p.project_id],
          )
        }
      }
    }
    this.db.commit()
  }
}

function isFresh(checkedAt: string | null, ttlSeconds: number): boolean {
  if (!checkedAt) return false
  const when = Date.parse(String(checkedAt).replace('Z', '+00:00'))
  if (Number.isNaN(when)) return false
  return (Date.now() - when) / 1000 < ttlSeconds
}

function secondsBetween(a: string | null, b: string | null): number | null {
  if (!a || !b) return null
  const ta = Date.parse(a.replace('Z', '+00:00'))
  const tb = Date.parse(b.replace('Z', '+00:00'))
  if (Number.isNaN(ta) || Number.isNaN(tb)) return null
  const delta = (tb - ta) / 1000
  return delta >= 0 ? delta : null
}

export function analyse(
  conn: Db,
  rawDir?: string,
  progress?: (msg: string) => void,
): Counts & { files: number } {
  return new Ingestor(conn, progress).run(rawDir)
}
