/** SQLite access helpers.
 *
 * Two things here are not a straight transcription of the Python this
 * replaces, because `node:sqlite` differs from `sqlite3` in ways that would
 * otherwise be silent bugs:
 *
 *  * **Integers.** node:sqlite throws rather than lose precision on an
 *    integer past 2^53, and nanosecond timestamps are all past it. Every
 *    statement therefore reads BigInt, and values that fit in a JS number are
 *    narrowed back on the way out - so callers see plain numbers everywhere
 *    except where precision genuinely needs a BigInt.
 *  * **Transactions.** node:sqlite autocommits every statement; Python's
 *    driver held an implicit transaction open until `commit()`. Committing
 *    per row means an fsync per row, which turns a minute of ingest into an
 *    hour, so writes open a transaction lazily and `commit()` closes it.
 */
import { DatabaseSync, type StatementSync } from 'node:sqlite'
import { readFileSync } from 'node:fs'
import * as config from './config.js'
import { dumps, utcNow } from './util/text.js'

export type Row = Record<string, any>
export type Bindable =
  string | number | bigint | boolean | null | undefined | Uint8Array

const MAX_SAFE = BigInt(Number.MAX_SAFE_INTEGER)
const MIN_SAFE = BigInt(Number.MIN_SAFE_INTEGER)

function bindValue(
  value: Bindable,
): string | number | bigint | null | Uint8Array {
  if (value === undefined || value === null) return null
  if (typeof value === 'boolean') return value ? 1 : 0
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'bigint' || typeof value === 'string') return value
  if (value instanceof Uint8Array) return value
  return String(value)
}

function narrow(value: unknown): unknown {
  if (typeof value === 'bigint') {
    return value <= MAX_SAFE && value >= MIN_SAFE ? Number(value) : value
  }
  return value
}

function normalizeRow(row: Record<string, unknown>): Row {
  const out: Row = {}
  for (const key of Object.keys(row)) out[key] = narrow(row[key])
  return out
}

const WRITE_SQL = /^\s*(?:insert|update|delete|replace)\b/i

export class Db {
  readonly raw: DatabaseSync
  readonly path: string
  private readonly cache = new Map<string, StatementSync>()
  private txOpen = false
  private closed = false

  constructor(raw: DatabaseSync, path: string) {
    this.raw = raw
    this.path = path
  }

  private stmt(sql: string): StatementSync {
    let prepared = this.cache.get(sql)
    if (!prepared) {
      prepared = this.raw.prepare(sql)
      prepared.setReadBigInts(true)
      this.cache.set(sql, prepared)
    }
    return prepared
  }

  private begin(): void {
    if (!this.txOpen) {
      this.raw.exec('BEGIN')
      this.txOpen = true
    }
  }

  /** Flush pending writes. A no-op when nothing is open, like `commit()` was. */
  commit(): void {
    if (this.txOpen) {
      this.raw.exec('COMMIT')
      this.txOpen = false
    }
  }

  rollback(): void {
    if (this.txOpen) {
      try {
        this.raw.exec('ROLLBACK')
      } finally {
        this.txOpen = false
      }
    }
  }

  run(
    sql: string,
    params: Bindable[] = [],
  ): { changes: number; lastInsertRowid: number } {
    if (WRITE_SQL.test(sql)) this.begin()
    const result = this.stmt(sql).run(...params.map(bindValue))
    return {
      changes: Number(result.changes),
      lastInsertRowid: Number(result.lastInsertRowid),
    }
  }

  all(sql: string, params: Bindable[] = []): Row[] {
    return (
      this.stmt(sql).all(...params.map(bindValue)) as Record<string, unknown>[]
    ).map(normalizeRow)
  }

  get(sql: string, params: Bindable[] = []): Row | undefined {
    const row = this.stmt(sql).get(...params.map(bindValue)) as
      Record<string, unknown> | undefined
    return row === undefined ? undefined : normalizeRow(row)
  }

  /** Stream rows, so a caller can stop early instead of materialising
   * everything an open-ended query would return.
   *
   * The `finally` is not tidiness. A statement abandoned part-way holds a read
   * transaction open, and SQLite cannot checkpoint the WAL while one exists -
   * left alone, the write-ahead log grows without bound and the database file
   * never does. Closing the iterator resets the statement and releases it.
   */
  *iterate(sql: string, params: Bindable[] = []): Generator<Row> {
    const iterator = this.stmt(sql).iterate(...params.map(bindValue))
    try {
      for (const row of iterator)
        yield normalizeRow(row as Record<string, unknown>)
    } finally {
      iterator.return?.(undefined as never)
    }
  }

  /** Fold the write-ahead log back into the database file.
   *
   * A long ingest writes in a handful of very large transactions, and every
   * page of them lives in the WAL until it is checkpointed. Left to the
   * automatic checkpointer that is fine in steady state but leaves the log
   * gigabytes wide part-way through a first full analyse, so the commit
   * points ask for it explicitly.
   */
  checkpoint(): void {
    this.commit()
    try {
      this.raw.exec('PRAGMA wal_checkpoint(PASSIVE)')
    } catch {
      /* another connection is reading; the next commit will try again */
    }
  }

  /** Multiple statements at once. Commits first: DDL and an open write
   * transaction do not mix well, and `executescript` behaved the same way. */
  exec(sql: string): void {
    this.commit()
    this.raw.exec(sql)
  }

  close(): void {
    if (this.closed) return
    this.closed = true
    try {
      this.commit()
    } finally {
      this.cache.clear()
      this.raw.close()
    }
  }
}

/** Columns added after the first release. Applied idempotently on every
 * connect so an existing database picks up schema changes without being
 * rebuilt. */
const MIGRATIONS: [string, string, string][] = [
  ['bash_activity', 'programs', 'TEXT'],
  ['spans', 'span_events', 'TEXT'],
  ['raw_files', 'inode', 'INTEGER'],
  ['turns', 'label_source', 'TEXT'],
  ['turns', 'is_system', 'INTEGER'],
  ['file_activity', 'via', 'TEXT'],
  ['file_activity', 'op_confidence', 'TEXT'],
  ['turns', 'label_confidence', 'TEXT'],
  ['git_activity', 'commit_type', 'TEXT'],
  ['git_activity', 'commit_scope', 'TEXT'],
  ['sessions', 'project_detection_method', 'TEXT'],
]

function migrate(db: Db): void {
  for (const [table, column, coltype] of MIGRATIONS) {
    let columns: Set<string>
    try {
      columns = new Set(
        db.all(`PRAGMA table_info(${table})`).map((r) => String(r.name)),
      )
    } catch {
      continue // table does not exist yet; schema.sql will create it
    }
    if (columns.size && !columns.has(column)) {
      db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${coltype}`)
    }
  }
  db.commit()
}

export function connect(path?: string, create = true): Db {
  config.ensureDirs()
  const target = path || config.DB_PATH
  const db = new Db(new DatabaseSync(target), target)
  db.raw.exec('PRAGMA foreign_keys = ON')
  // A long analyse and a background job can want the write lock at the same
  // time. Without a busy timeout SQLite fails immediately, which killed a
  // judging run mid-way. Wait rather than crash.
  db.raw.exec('PRAGMA busy_timeout = 60000')
  // Migrations run first: schema.sql indexes columns that an older database
  // has not grown yet, and CREATE INDEX on a missing column fails the whole
  // script.
  migrate(db)
  if (create) {
    db.exec(readFileSync(config.SCHEMA_PATH, 'utf8'))
    db.commit()
  }
  return db
}

/** Read-only connection, for anything that must not be able to write. */
export function connectReadOnly(path?: string): Db {
  const target = path || config.DB_PATH
  return new Db(new DatabaseSync(target, { readOnly: true }), target)
}

/** Every table keyed by session. Purging a session means clearing all of them,
 * and `turns` being missing from this list is how a purged session's turns
 * kept skewing the friction totals after the session itself was gone. */
export const SESSION_TABLES = [
  'api_calls',
  'tool_calls',
  'skill_calls',
  'file_activity',
  'bash_activity',
  'subagent_activity',
  'prompts',
  'responses',
  'errors',
  'metric_points',
  'spans',
  'events',
  'turns',
  'file_rework',
  'local_session_git_context',
  'sessions',
]

/** Delete sessions and everything keyed to them, permanently.
 *
 * The id is remembered because the collector batches: events for a session can
 * land after it was deleted, and the next analyse would otherwise rebuild it
 * from them.
 */
export function purgeSessions(
  db: Db,
  sessionIds: string[],
  reason: string,
): void {
  if (!sessionIds.length) return
  const marks = sessionIds.map(() => '?').join(',')
  db.run('PRAGMA defer_foreign_keys = ON')
  for (const table of SESSION_TABLES) {
    db.run(`DELETE FROM ${table} WHERE session_id IN (${marks})`, sessionIds)
  }
  for (const sid of sessionIds) {
    db.run(
      'INSERT OR REPLACE INTO purged_sessions(session_id, reason, purged_at) VALUES (?,?,?)',
      [sid, reason, utcNow()],
    )
  }
  db.commit()
}

export function setMeta(db: Db, key: string, value: unknown): void {
  db.run(
    'INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
    [key, typeof value === 'string' ? value : dumps(value)],
  )
}

export function getMeta(
  db: Db,
  key: string,
  fallback: string | null = null,
): string | null {
  const row = db.get('SELECT value FROM meta WHERE key=?', [key])
  return row ? (row.value as string) : fallback
}

/** INSERT OR IGNORE; returns the rowid when a row was actually inserted. */
export function insertIgnore(
  db: Db,
  table: string,
  row: Record<string, Bindable>,
): number | null {
  const keys = Object.keys(row)
  const cols = keys.join(', ')
  const marks = keys.map(() => '?').join(', ')
  const result = db.run(
    `INSERT OR IGNORE INTO ${table} (${cols}) VALUES (${marks})`,
    keys.map((k) => row[k]),
  )
  return result.changes ? result.lastInsertRowid : null
}

/** Insert a row, or fill in NULL columns of an existing row with the same key.
 *
 * Used to merge the several telemetry signals that describe one tool call: the
 * decision event, the result event and (when tracing is on) the span. COALESCE
 * keeps whichever signal arrived first and only fills gaps.
 */
export function upsertMerge(
  db: Db,
  table: string,
  keyCol: string,
  row: Record<string, Bindable>,
): void {
  const keys = Object.keys(row)
  const cols = keys.join(', ')
  const marks = keys.map(() => '?').join(', ')
  const updates = keys
    .filter((c) => c !== keyCol)
    .map((c) => `${c}=COALESCE(${table}.${c}, excluded.${c})`)
    .join(', ')
  let sql = `INSERT INTO ${table} (${cols}) VALUES (${marks})`
  sql += updates
    ? ` ON CONFLICT(${keyCol}) DO UPDATE SET ${updates}`
    : ` ON CONFLICT(${keyCol}) DO NOTHING`
  db.run(
    sql,
    keys.map((k) => row[k]),
  )
}

export function scalar<T = number>(
  db: Db,
  sql: string,
  params: Bindable[] = [],
  fallback: T = 0 as unknown as T,
): T {
  const row = db.get(sql, params)
  if (!row) return fallback
  const first = Object.values(row)[0]
  return (first === null || first === undefined ? fallback : first) as T
}
