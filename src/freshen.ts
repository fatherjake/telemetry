/** Bring the database up to date, at the moment somebody asks a question.
 *
 * `analyse` used to be a thing you had to remember. That was backwards: the
 * only moment freshness matters is when a question is being asked, and by then
 * it is too late to remember. So the MCP server calls this before answering.
 *
 * Three things keep it from being a nuisance:
 *
 *   * **It costs nothing when there is nothing to do.** The check is a `stat`
 *     per raw file against a byte cursor the ingester already maintains - no
 *     parsing, no reading. The overwhelmingly common case is "nothing new".
 *   * **One analyse per question, not per tool call.** An answer that calls six
 *     tools should ingest once, so a successful run is debounced.
 *   * **It never blocks and never fails a query.** Concurrent Claude Code
 *     sessions each run their own MCP server; the first to take the lock does
 *     the work and the rest answer from what is already there. Anything that
 *     goes wrong is reported to stderr and the question is still answered - a
 *     slightly stale answer beats an error.
 */
import { existsSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import * as config from './config.js'
import { connect, connectReadOnly } from './db.js'
import { analyse } from './ingest.js'
import { tryAcquire } from './util/lock.js'
import { group } from './util/text.js'

/** A burst of tool calls inside one answer must not each pay for an analyse.
 * Long enough to cover an answer, short enough that the next question is
 * fresh. */
const MIN_INTERVAL_S = 20

let lastRun = 0

/** Raw bytes on disk that no analyse has consumed yet.
 *
 * Deliberately bytes rather than lines: a line count means reading every file,
 * and the archive runs to gigabytes. `raw_files.bytes_consumed` is maintained
 * by the ingester and tracks file size exactly, so subtracting is enough to
 * know whether there is work.
 */
export function pendingBytes(): number {
  let onDisk = 0
  try {
    for (const name of readdirSync(config.RAW_DIR)) {
      if (name.endsWith('.jsonl'))
        onDisk += statSync(join(config.RAW_DIR, name)).size
    }
  } catch {
    return 0
  }
  if (!existsSync(config.DB_PATH)) return onDisk
  try {
    const db = connectReadOnly()
    try {
      const row = db.get(
        'SELECT COALESCE(SUM(bytes_consumed), 0) AS n FROM raw_files',
      )
      return Math.max(0, onDisk - Number(row?.n ?? 0))
    } finally {
      db.close()
    }
  } catch {
    // No raw_files table yet, or the database is mid-write. Either way, not a
    // reason to hold up the caller.
    return 0
  }
}

export interface FreshenResult {
  ran: boolean
  why?: string
  seconds?: number
  pending_bytes?: number
  logs?: number
  metrics?: number
  traces?: number
  skipped?: number
  files?: number
}

/** Ingest anything new, if there is anything new and nobody else is on it.
 *
 * Returns what happened, for the caller to log. Never throws.
 */
export function freshen(force = false): FreshenResult {
  const now = performance.now() / 1000
  if (!force && now - lastRun < MIN_INTERVAL_S)
    return { ran: false, why: 'debounced' }

  const pending = pendingBytes()
  if (!pending) {
    lastRun = now
    return { ran: false, why: 'up to date' }
  }

  // A lock whose owner is dead is taken over, not waited on, so a crashed
  // analyse cannot wedge every future one behind a stale lock.
  config.ensureDirs()
  const lock = tryAcquire(config.ANALYSE_LOCK)
  if (!lock) {
    // Another MCP server is already doing it. Its work lands in the same
    // database; answering from slightly older rows is correct behaviour, not a
    // failure.
    return { ran: false, why: 'another analyse is running' }
  }

  try {
    const started = performance.now() / 1000
    const db = connect()
    let counts
    try {
      counts = analyse(db)
    } finally {
      db.close()
    }
    lastRun = performance.now() / 1000
    return {
      ran: true,
      seconds: Math.round((lastRun - started) * 100) / 100,
      pending_bytes: pending,
      ...counts,
    }
  } catch (exc) {
    // A failed analyse must not turn into a failed question.
    lastRun = performance.now() / 1000
    return {
      ran: false,
      why: `${(exc as Error).name}: ${(exc as Error).message}`,
    }
  } finally {
    lock.release()
  }
}

/** `freshen`, with the outcome on stderr where MCP logging belongs. */
export function freshenAndLog(): void {
  const r = freshen()
  if (r.ran) {
    process.stderr.write(
      `analysed ${group(r.pending_bytes ?? 0)} new raw bytes in ${r.seconds}s ` +
        `(${r.logs ?? 0} logs, ${r.metrics ?? 0} metrics, ${r.traces ?? 0} spans)\n`,
    )
  } else if (r.why !== 'up to date' && r.why !== 'debounced') {
    process.stderr.write(`analyse skipped: ${r.why}\n`)
  }
}
