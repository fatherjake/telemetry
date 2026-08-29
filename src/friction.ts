/** Mechanical friction signals: the cheap half of "how did this session go".
 *
 * A file rewritten eleven times, the same command run nine times, one turn
 * eating a third of the money. These are the cheapest signals in the database
 * and **none of them need a model** - they are SQL over what already happened.
 *
 * Interpreting them is the expensive half, and it does not live here. A video
 * frame refined eleven times is iteration and has no fix; a config file
 * rewritten eleven times is a loop and does. Telling those apart is a
 * judgement, and the agent reading these signals over MCP is already a model -
 * so it makes the call itself, with the whole database in reach, instead of
 * paying a second one to do it in advance.
 *
 * Thresholds are deliberately generous. A false negative costs nothing; a
 * false positive costs the reader's trust.
 */
import { type Db, scalar } from './db.js'
import { groupFixed } from './util/text.js'

const MIN_WRITES = 4 // rewrites of one file before it reads as a loop
const MIN_SWITCHES = 4 // read->write->read flips on one path
const MAX_ALTERNATION_ROWS = 4
const MIN_REREADS = 6 // reads of a file never written in this session
const MIN_REPEATS = 4 // identical shell commands
const MIN_FAILURES = 3 // failures of one program, or one tool error type
const MIN_SEARCHES = 10 // search calls inside a single turn
const COST_SHARE = 0.3 // share of session cost that makes one turn a spike
const COST_FLOOR = 1.0 // ...and the dollars below which it is not worth saying

export interface Signal {
  kind: string
  subject: string
  n: number
  detail: string
}

function sig(kind: string, subject: string, n: number, detail: string): Signal {
  return { kind, subject, n, detail }
}

/** Map every path to a canonical form, merging the same file recorded under
 * two roots.
 *
 * Shell-derived rows resolve relative paths against a best-guess working
 * directory, and when that guess is one level off the same file lands under
 * two repo-relative paths - `src/a/B.tsx` and `project/src/a/B.tsx`. Left alone
 * they read as two files each edited half as often, which is exactly the
 * number a diagnosis would reason from. Fold on the suffix relation: it is a
 * heuristic, but the alternative is knowingly wrong counts.
 */
export function foldPaths(paths: string[]): Map<string, string> {
  const uniq = [...new Set(paths)].sort((a, b) => a.length - b.length)
  const canon = new Map<string, string>()
  for (const p of uniq) {
    let target = p
    for (const shorter of uniq) {
      if (shorter === p || shorter.length >= p.length) continue
      if (p.endsWith('/' + shorter)) {
        target = canon.get(shorter) ?? shorter
        break
      }
    }
    canon.set(p, target)
  }
  return canon
}

/** Mechanical friction in one session. No model, no network, no cost. */
export function signals(db: Db, sessionId: string): Signal[] {
  const out: Signal[] = []

  // --- file rework -------------------------------------------------------
  // `search` (a grep) and `delete` are excluded so these counts match the
  // lanes already shown elsewhere; low-confidence shell inferences are
  // excluded so a misparsed command line cannot invent a rewrite loop.
  const rows = db.all(
    `SELECT COALESCE(repo_relative_path, path) AS p, operation, ts
       FROM file_activity
      WHERE session_id = ? AND operation IN ('read','write','edit','notebook_edit')
        AND COALESCE(op_confidence,'high') <> 'low'
      ORDER BY ts`,
    [sessionId],
  )
  const canon = foldPaths(rows.map((r) => String(r.p)))
  const ops = new Map<string, string[]>()
  for (const r of rows) {
    const key = canon.get(String(r.p))!
    if (!ops.has(key)) ops.set(key, [])
    ops.get(key)!.push(r.operation === 'read' ? 'r' : 'w')
  }

  const writes = [...ops.entries()]
    .map(([p, s]) => [p, s.filter((o) => o === 'w').length] as [string, number])
    .sort((a, b) => b[1] - a[1])
  for (const [path, n] of writes.slice(0, 5)) {
    if (n >= MIN_WRITES)
      out.push(sig('file_written_repeatedly', path, n, `${n} writes/edits`))
  }

  // --- read/write ping-pong ---------------------------------------------
  const alts: Signal[] = []
  for (const [path, seq] of ops) {
    const runs = [seq[0]!]
    for (const o of seq.slice(1)) if (o !== runs[runs.length - 1]) runs.push(o)
    const switches = runs.length - 1
    if (switches >= MIN_SWITCHES && runs.includes('w')) {
      const reads = seq.filter((o) => o === 'r').length
      alts.push(
        sig(
          'read_write_alternation',
          path,
          switches,
          `${switches} switches between reading and writing it ` +
            `(${reads} reads, ${seq.length - reads} writes)`,
        ),
      )
    }
  }
  alts.sort((a, b) => b.n - a.n)
  out.push(...alts.slice(0, MAX_ALTERNATION_ROWS))

  // --- re-reading something never written -------------------------------
  for (const r of db.all(
    `SELECT COALESCE(repo_relative_path, path) AS p, COUNT(*) n
       FROM file_activity
      WHERE session_id = ? AND operation = 'read'
      GROUP BY p HAVING n >= ?
         AND p NOT IN (SELECT COALESCE(repo_relative_path, path)
                         FROM file_activity
                        WHERE session_id = ? AND operation <> 'read')
      ORDER BY n DESC LIMIT 5`,
    [sessionId, MIN_REREADS, sessionId],
  )) {
    out.push(
      sig(
        'file_read_repeatedly',
        String(r.p),
        Number(r.n),
        `read ${r.n} times, never written`,
      ),
    )
  }

  // --- repeated shell commands ------------------------------------------
  for (const r of db.all(
    `SELECT substr(command, 1, 120) AS c, COUNT(*) n
       FROM bash_activity WHERE session_id = ? AND command IS NOT NULL
      GROUP BY command_hash HAVING n >= ? ORDER BY n DESC LIMIT 5`,
    [sessionId, MIN_REPEATS],
  )) {
    out.push(
      sig(
        'command_repeated',
        String(r.c),
        Number(r.n),
        `run ${r.n} times unchanged`,
      ),
    )
  }

  for (const r of db.all(
    `SELECT program, COUNT(*) n FROM bash_activity
      WHERE session_id = ? AND success = 0 AND program IS NOT NULL
      GROUP BY program HAVING n >= ? ORDER BY n DESC LIMIT 5`,
    [sessionId, MIN_FAILURES],
  )) {
    out.push(
      sig(
        'command_failing',
        String(r.program),
        Number(r.n),
        `${r.n} failed runs`,
      ),
    )
  }

  // --- tool errors -------------------------------------------------------
  for (const r of db.all(
    `SELECT tool_name, COALESCE(error_type,'error') AS e, COUNT(*) n
       FROM tool_calls
      WHERE session_id = ? AND success = 0
      GROUP BY tool_name, e HAVING n >= ? ORDER BY n DESC LIMIT 5`,
    [sessionId, MIN_FAILURES],
  )) {
    out.push(
      sig(
        'tool_failing',
        String(r.tool_name),
        Number(r.n),
        `${r.n} failures (${r.e})`,
      ),
    )
  }

  // --- search storms -----------------------------------------------------
  for (const r of db.all(
    `SELECT c.prompt_id, COUNT(*) n, t.seq
       FROM tool_calls c LEFT JOIN turns t ON t.turn_id = c.prompt_id
      WHERE c.session_id = ? AND c.tool_category = 'search'
      GROUP BY c.prompt_id HAVING n >= ? ORDER BY n DESC LIMIT 3`,
    [sessionId, MIN_SEARCHES],
  )) {
    out.push(
      sig(
        'search_storm',
        `turn ${r.seq}`,
        Number(r.n),
        `${r.n} searches inside one turn`,
      ),
    )
  }

  // --- where the money went ---------------------------------------------
  const total = scalar<number>(
    db,
    'SELECT COALESCE(SUM(cost_usd),0) FROM turns WHERE session_id = ?',
    [sessionId],
    0,
  )
  if (total) {
    for (const r of db.all(
      `SELECT seq, cost_usd, duration_s,
              substr(COALESCE(prompt_text,''),1,160) AS p
         FROM turns WHERE session_id = ?
          AND cost_usd >= ? AND cost_usd >= ?
        ORDER BY cost_usd DESC LIMIT 2`,
      [sessionId, total * COST_SHARE, COST_FLOOR],
    )) {
      const cost = Number(r.cost_usd) || 0
      const share = Math.round((cost / total) * 100)
      out.push(
        sig(
          'cost_spike',
          `turn ${r.seq}`,
          Math.round(cost * 100) / 100,
          `$${groupFixed(cost, 2)} - ${share}% of the session` +
            (r.p ? ` - "${r.p}"` : ''),
        ),
      )
    }
  }

  // --- work the human threw away ----------------------------------------
  const nRejected = scalar<number>(
    db,
    `SELECT COALESCE(SUM(rejects),0) + COALESCE(SUM(user_overrides),0)
       FROM turns WHERE session_id = ?`,
    [sessionId],
    0,
  )
  if (nRejected >= 3) {
    out.push(
      sig(
        'work_rejected',
        'tool calls',
        nRejected,
        `${nRejected} tool calls rejected or aborted by the human`,
      ),
    )
  }
  return out
}
