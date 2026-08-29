/** A read-only MCP server over the database, spoken on stdin/stdout.
 *
 * This is the view layer. Everything the old per-view commands printed as a
 * table is here as a tool, so the numbers can be asked about in the place the
 * questions actually occur - inside a Claude Code session - instead of being
 * read out of a terminal and pasted back in.
 *
 * It talks to the database file, not to the receiver. That means it answers
 * whether or not anything is running, needs no port and no auth. Before each
 * tool call it ingests whatever the receiver has written since last time, so
 * there is no `analyse` to remember - see src/freshen.ts.
 *
 * Transport is JSON-RPC 2.0, one object per line. stdout carries protocol
 * traffic and nothing else; anything worth saying goes to stderr.
 *
 * Read-only by construction: the connection is opened read-only, so a bug here
 * cannot corrupt a database, and `telemetry_sql` cannot be talked into writing.
 */
import { existsSync } from 'node:fs'
import * as config from './config.js'
import { type Db, type Row, connectReadOnly } from './db.js'
import { dumps } from './util/text.js'
import { freshenAndLog } from './freshen.js'
import * as F from './friction.js'
import * as Q from './queries.js'

export const SERVER_NAME = config.MCP_SERVER_NAME
export const SERVER_VERSION = '1.0.0'

/** The version whose shape this server implements. A client asking for
 * another is answered with this one rather than refused: the parts of the
 * protocol used here have not changed across the versions in the wild. */
const PROTOCOL_VERSION = '2025-06-18'

const SQL_ROW_CAP = 500

// --------------------------------------------------------------- helpers ---

/** Read-only connection to the database. */
function open(): Db {
  if (!existsSync(config.DB_PATH)) {
    throw new NotFoundError(
      `no database at ${config.DB_PATH} - run \`telemetry analyse\` first`,
    )
  }
  return connectReadOnly()
}

class NotFoundError extends Error {
  override readonly name = 'FileNotFoundError'
}
class BadRequestError extends Error {
  override readonly name = 'ValueError'
}

function round(value: unknown, places = 2): unknown {
  if (typeof value !== 'number' || !Number.isFinite(value)) return value
  const f = 10 ** places
  return Math.round(value * f) / f
}

function roundAll(obj: Record<string, any>, places = 2): Record<string, any> {
  const out: Record<string, any> = {}
  for (const [k, v] of Object.entries(obj)) out[k] = round(v, places)
  return out
}

/** Resolve a session id prefix, the way every other entry point does. */
function resolveSessionId(db: Db, prefix: string): string {
  const exact = db.get('SELECT session_id FROM sessions WHERE session_id=?', [
    prefix,
  ])
  if (exact) return String(exact.session_id)
  const hits = db.all(
    'SELECT session_id FROM sessions WHERE session_id LIKE ? LIMIT 5',
    [prefix + '%'],
  )
  if (!hits.length) throw new BadRequestError(`no session matching '${prefix}'`)
  if (hits.length > 1) {
    throw new BadRequestError(
      `'${prefix}' matches ${hits.length} sessions: ` +
        hits.map((h) => String(h.session_id).slice(0, 8)).join(', '),
    )
  }
  return String(hits[0]!.session_id)
}

// ----------------------------------------------------------------- tools ---

type Args = Record<string, any>

function tOverview(db: Db, _args: Args): Record<string, unknown> {
  const [from, to] = Q.observationPeriod(db)
  return {
    observed_from: from,
    observed_to: to,
    totals: Q.overview(db),
    output: Q.outputSummary(db),
    friction: Q.frictionTotals(db),
    note:
      'the unit here is the session: one session, one piece of work, ' +
      'its own cost and its own commits',
  }
}

function tSessions(db: Db, args: Args): Record<string, unknown> {
  const limit = Number(args.limit ?? 25)
  const project = String(args.project ?? '').toLowerCase()
  const out: Record<string, unknown>[] = []
  for (const d of Q.sessionDetail(db)) {
    const s = d.session
    if (
      project &&
      !`${s.project_name ?? ''}${s.project_id ?? ''}`
        .toLowerCase()
        .includes(project)
    ) {
      continue
    }
    const e = d.effort
    out.push({
      session_id: s.session_id,
      asked_for: d.description,
      project: s.project_name,
      started: s.first_seen,
      ended: s.last_seen,
      duration_s: round(s.duration_s, 0),
      cost_usd: round(s.cost),
      turns: e.turns,
      corrections: e.corrections,
      steers: e.steers,
      commits: s.commits,
      lines: { insertions: s.insertions, deletions: s.deletions },
      reverted: s.reverted,
    })
    if (out.length >= limit) break
  }
  return {
    sessions: out,
    order: 'most recent first',
    note:
      "asked_for is the session's own first human prompt, verbatim and " +
      'truncated. Nothing is generated, so read it as the ask, not as a ' +
      'summary of what happened.',
  }
}

function tSession(db: Db, args: Args): Record<string, unknown> {
  const sid = resolveSessionId(db, String(args.session_id ?? ''))
  const detail = Q.sessionDetail(db, [sid])
  if (!detail.length) {
    return {
      session_id: sid,
      note: 'no turns or tool calls recorded for this session',
    }
  }
  const d = detail[0]!
  const out: Record<string, unknown> = {
    session: roundAll(d.session),
    asked_for: d.description,
    effort: roundAll(d.effort),
    commits: d.commits,
    skills_used: d.skills,
    mcp_servers: d.mcps,
    corrections: d.corrections,
    skills_available_unused: Q.sessionUnusedSkills(db, sid),
    friction_signals: F.signals(db, sid),
  }
  if (args.include_turns ?? true) {
    out.turns = d.turns.map((t) => roundAll(t as Record<string, any>))
  }
  out.note =
    'friction_signals are mechanical counts, not verdicts: a file rewritten ' +
    'eleven times may be careful iteration or a loop, and which one it was ' +
    'is visible in the turns, not in the count. skills_available_unused is ' +
    'every installed skill this session did not call - it is the candidate ' +
    'list, not a claim that any of them should have fired.'
  return out
}

function tFriction(db: Db, args: Args): Record<string, unknown> {
  const limit = Number(args.limit ?? 20)
  return {
    totals: Q.frictionTotals(db),
    by_session: Q.frictionBySession(db, limit),
    rework_files: Q.reworkFiles(db, limit),
    recent_corrections: Q.corrections(db, limit),
    note:
      'corrections and steers are the human paying for a miss; rework is the ' +
      'same file edited across many turns. For the mechanical signals inside ' +
      'one session - rewrite loops, repeated commands, cost spikes - call ' +
      'telemetry_session.',
  }
}

function tInventory(db: Db, _args: Args): Record<string, unknown> {
  const skills = Q.skillUsage(db)
  const mcps = Q.mcpUsage(db)
  return {
    skills,
    skills_never_used: skills.filter((s) => !s.invocations).map((s) => s.name),
    skills_used_but_not_installed: Q.skillsUsedNotInstalled(db),
    mcp_servers: mcps,
    mcp_never_used: mcps.filter((m) => !m.calls).map((m) => m.name),
    subagents: Q.subagents(db),
  }
}

function tFiles(db: Db, args: Args): Record<string, unknown> {
  const limit = Number(args.limit ?? 40)
  const prefix = args.under as string | undefined
  const out: Record<string, unknown> = {
    file_access: Q.fileAccess(db, prefix, limit),
    hot_files: Q.hotFiles(db, limit),
    hot_dirs: Q.hotDirs(db, 15),
    created: Q.createdFiles(db, limit),
  }
  const root = args.unread_under as string | undefined
  if (root)
    out.unread = Q.unreadFiles(db, root, (args.pattern as string) || '*.md')
  return out
}

function tSql(db: Db, args: Args): Record<string, unknown> {
  const query = String(args.query ?? '').trim()
  if (!query) throw new BadRequestError('pass a query')
  if (!/^(select|with|explain|pragma)\b/i.test(query)) {
    throw new BadRequestError(
      'read-only: queries must start with SELECT, WITH, EXPLAIN or PRAGMA',
    )
  }
  const limit = Math.min(Number(args.limit ?? 100), SQL_ROW_CAP)
  const rows: Row[] = []
  let truncated = false
  for (const row of db.iterate(query)) {
    if (rows.length >= limit) {
      truncated = true
      break
    }
    rows.push(row)
  }
  return {
    rows,
    truncated,
    note: truncated ? `stopped at ${limit} rows` : null,
  }
}

/** The table shapes, so telemetry_sql can be written without guessing. */
function tSchema(db: Db, args: Args): Record<string, unknown> {
  const tables = db.all(
    "SELECT name, sql FROM sqlite_master WHERE type='table'" +
      " AND name NOT LIKE 'sqlite_%' ORDER BY name",
  )
  const only = args.table as string | undefined
  const out: Record<string, string[]> = {}
  for (const t of tables) {
    const name = String(t.name)
    if (only && !name.toLowerCase().includes(only.toLowerCase())) continue
    out[name] = db.all(`PRAGMA table_info(${name})`).map((c) => String(c.name))
  }
  return { tables: out }
}

interface ToolDef {
  name: string
  description: string
  handler: (db: Db, args: Args) => Record<string, unknown>
  inputSchema: Record<string, unknown>
}

export const TOOLS: ToolDef[] = [
  {
    name: 'telemetry_overview',
    description:
      'Totals for the whole database: observation period, spend, tokens, tool ' +
      'calls, what shipped, human friction, and how much spend is attributable ' +
      'to a unit of work. Start here.',
    handler: tOverview,
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'telemetry_sessions',
    description:
      'The unit of work: one session, one item. Most recent first, each with ' +
      'the prompt it opened on, what it cost, what it committed, and how much ' +
      'correcting it took.',
    handler: tSessions,
    inputSchema: {
      type: 'object',
      properties: {
        limit: { type: 'integer', description: 'default 25' },
        project: { type: 'string', description: 'substring of a project name' },
      },
    },
  },
  {
    name: 'telemetry_session',
    description:
      'One session in full: its turns, which were corrections or steering, what ' +
      'it cost, its commits, the skills and MCP servers it used, every installed ' +
      'skill it did NOT use, and mechanical friction signals - rewrite loops, ' +
      'repeated commands, cost spikes - computed on the fly.',
    handler: tSession,
    inputSchema: {
      type: 'object',
      properties: {
        session_id: {
          type: 'string',
          description: 'full id or a unique prefix',
        },
        include_turns: {
          type: 'boolean',
          description: 'default true; false for just the summary',
        },
      },
      required: ['session_id'],
    },
  },
  {
    name: 'telemetry_friction',
    description:
      'Where the human had to intervene: corrections, steering, permission ' +
      'rejections, tool failures, and the files edited over and over. The cost ' +
      'of getting it wrong.',
    handler: tFriction,
    inputSchema: {
      type: 'object',
      properties: { limit: { type: 'integer', description: 'default 20' } },
    },
  },
  {
    name: 'telemetry_inventory',
    description:
      'Installed skills and configured MCP servers against what was actually ' +
      'invoked, plus subagent use. The entries with zero invocations are the point.',
    handler: tInventory,
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'telemetry_files',
    description:
      'Which files were read, written and created, including through the shell, ' +
      'and which directories the work concentrated in. Pass unread_under with a ' +
      'directory to list files on disk that were never opened - this is the ' +
      'knowledge-base gap analysis.',
    handler: tFiles,
    inputSchema: {
      type: 'object',
      properties: {
        under: { type: 'string', description: 'path prefix filter' },
        unread_under: {
          type: 'string',
          description: 'directory to check for never-opened files',
        },
        pattern: { type: 'string', description: 'default *.md' },
        limit: { type: 'integer', description: 'default 40' },
      },
    },
  },
  {
    name: 'telemetry_schema',
    description:
      'Tables and columns in the database, for writing telemetry_sql.',
    handler: tSchema,
    inputSchema: {
      type: 'object',
      properties: {
        table: {
          type: 'string',
          description: 'filter to matching table names',
        },
      },
    },
  },
  {
    name: 'telemetry_sql',
    description:
      'Read-only SQL against the database, for anything the other tools do not ' +
      'answer. SELECT/WITH/EXPLAIN/PRAGMA only. Call telemetry_schema first if ' +
      'unsure of the shape.',
    handler: tSql,
    inputSchema: {
      type: 'object',
      properties: {
        query: { type: 'string' },
        limit: {
          type: 'integer',
          description: `rows to return, max ${SQL_ROW_CAP}`,
        },
      },
      required: ['query'],
    },
  },
]

const BY_NAME = new Map(TOOLS.map((t) => [t.name, t]))

// ------------------------------------------------------------- transport ---

function write(obj: Record<string, unknown>): void {
  process.stdout.write(dumps(obj) + '\n')
}

function sendResult(reqId: unknown, result: Record<string, unknown>): void {
  write({ jsonrpc: '2.0', id: reqId as never, result })
}

function sendError(reqId: unknown, code: number, message: string): void {
  write({
    jsonrpc: '2.0',
    id: (reqId ?? null) as never,
    error: { code, message },
  })
}

function callTool(params: Record<string, any>): Record<string, unknown> {
  const name = params.name as string
  const tool = BY_NAME.get(name)
  if (tool === undefined) {
    return {
      content: [{ type: 'text', text: `no such tool: ${name}` }],
      isError: true,
    }
  }
  const args = (params.arguments ?? {}) as Args
  // Bring the database up to date first. The only moment freshness matters is
  // the one where a question is being asked, and this is it. Costs a stat per
  // raw file when there is nothing new, which is nearly always.
  freshenAndLog()
  let db: Db | null = null
  try {
    db = open()
    const payload = tool.handler(db, args)
    // Not JSON.stringify: a `SELECT *` hands back nanosecond timestamps as
    // BigInt, which it refuses to serialise outright.
    return { content: [{ type: 'text', text: dumps(payload) }] }
  } catch (exc) {
    // Asking for a session that does not exist, or writing bad SQL, is a
    // message back to the caller - not something to spill a stack trace over.
    const err = exc as Error
    const expected =
      err instanceof BadRequestError || err instanceof NotFoundError
    process.stderr.write(
      expected
        ? `${name}: ${err.message}\n`
        : `${name} failed: ${err.stack ?? err.message}\n`,
    )
    return {
      content: [{ type: 'text', text: `${err.name}: ${err.message}` }],
      isError: true,
    }
  } finally {
    if (db !== null) db.close()
  }
}

export class MethodNotFound extends Error {}

/** One request in, one response out. null for notifications. */
export function handle(
  message: Record<string, any>,
): Record<string, unknown> | null {
  const method = message.method as string | undefined
  const reqId = message.id
  const params = (message.params ?? {}) as Record<string, any>

  if (method === 'initialize') {
    return {
      protocolVersion: PROTOCOL_VERSION,
      capabilities: { tools: {} },
      serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
    }
  }
  if (method === 'tools/list') {
    return {
      tools: TOOLS.map(({ name, description, inputSchema }) => ({
        name,
        description,
        inputSchema,
      })),
    }
  }
  if (method === 'tools/call') return callTool(params)
  if (method === 'ping') return {}
  if (method && method.startsWith('notifications/')) return null
  if (reqId === undefined || reqId === null) return null
  throw new MethodNotFound(method || '(no method)')
}

function handleLine(line: string): void {
  const trimmed = line.trim()
  if (!trimmed) return
  let message: Record<string, any>
  try {
    message = JSON.parse(trimmed)
  } catch {
    sendError(null, -32700, 'parse error')
    return
  }
  const reqId = message.id
  let result: Record<string, unknown> | null
  try {
    result = handle(message)
  } catch (exc) {
    const err = exc as Error
    if (err instanceof MethodNotFound) {
      sendError(reqId, -32601, `method not found: ${err.message}`)
      return
    }
    process.stderr.write(`${err.stack ?? err.message}\n`)
    sendError(reqId, -32603, `${err.name}: ${err.message}`)
    return
  }
  if (result !== null && reqId !== undefined && reqId !== null)
    sendResult(reqId, result)
}

/** Read newline-delimited JSON-RPC from stdin until it closes. */
export function serve(): Promise<number> {
  return new Promise((resolve) => {
    let buffer = ''
    process.stdin.setEncoding('utf8')
    process.stdin.on('data', (chunk: string) => {
      buffer += chunk
      let idx: number
      while ((idx = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 1)
        handleLine(line)
      }
    })
    process.stdin.on('end', () => {
      if (buffer.trim()) handleLine(buffer)
      resolve(0)
    })
    process.stdin.on('error', () => resolve(0))
  })
}
