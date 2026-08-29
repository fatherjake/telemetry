/** Reusable read queries shared by the CLI and the MCP server. */
import { existsSync, readdirSync } from 'node:fs'
import { join, matchesGlob } from 'node:path'
import { type Db, type Row, scalar } from './db.js'

export function observationPeriod(db: Db): [string | null, string | null] {
  const row = db.get(
    'SELECT MIN(ts) a, MAX(ts) b FROM (' +
      ' SELECT ts FROM events WHERE ts IS NOT NULL' +
      ' UNION ALL SELECT ts FROM metric_points WHERE ts IS NOT NULL)',
  )
  return row
    ? [(row.a as string) ?? null, (row.b as string) ?? null]
    : [null, null]
}

export function overview(db: Db): Record<string, number | string | null> {
  const [a, b] = observationPeriod(db)
  return {
    period_start: a,
    period_end: b,
    // Hooks create a row for every session that starts, including ones that
    // never exported telemetry. Counting those as observed sessions overstates
    // coverage badly - 93 rows for 16 real sessions.
    sessions: scalar(
      db,
      'SELECT COUNT(*) FROM sessions WHERE first_seen IS NOT NULL',
    ),
    sessions_hook_only: scalar(
      db,
      'SELECT COUNT(*) FROM sessions WHERE first_seen IS NULL',
    ),
    projects: scalar(
      db,
      "SELECT COUNT(*) FROM projects WHERE project_id NOT LIKE 'unknown:%'",
    ),
    cost_usd: scalar(
      db,
      "SELECT COALESCE(SUM(cost_usd),0) FROM api_calls WHERE outcome='ok'",
      [],
      0,
    ),
    api_calls: scalar(db, "SELECT COUNT(*) FROM api_calls WHERE outcome='ok'"),
    api_failures: scalar(
      db,
      "SELECT COUNT(*) FROM api_calls WHERE outcome<>'ok'",
    ),
    input_tokens: scalar(
      db,
      'SELECT COALESCE(SUM(input_tokens),0) FROM api_calls',
    ),
    output_tokens: scalar(
      db,
      'SELECT COALESCE(SUM(output_tokens),0) FROM api_calls',
    ),
    cache_read_tokens: scalar(
      db,
      'SELECT COALESCE(SUM(cache_read_tokens),0) FROM api_calls',
    ),
    cache_creation_tokens: scalar(
      db,
      'SELECT COALESCE(SUM(cache_creation_tokens),0) FROM api_calls',
    ),
    tool_calls: scalar(db, 'SELECT COUNT(*) FROM tool_calls'),
    skill_calls: scalar(db, 'SELECT COUNT(*) FROM skill_calls'),
    files_read: scalar(
      db,
      "SELECT COUNT(*) FROM file_activity WHERE operation='read'",
    ),
    distinct_files_read: scalar(
      db,
      "SELECT COUNT(DISTINCT path) FROM file_activity WHERE operation='read'",
    ),
    files_changed: scalar(
      db,
      "SELECT COUNT(*) FROM file_activity WHERE operation IN ('edit','write','notebook_edit')",
    ),
    distinct_files_changed: scalar(
      db,
      "SELECT COUNT(DISTINCT path) FROM file_activity WHERE operation IN ('edit','write','notebook_edit')",
    ),
    files_created: scalar(
      db,
      'SELECT COUNT(DISTINCT path) FROM file_activity WHERE created=1',
    ),
    bash_commands: scalar(db, 'SELECT COUNT(*) FROM bash_activity'),
    subagents: scalar(db, 'SELECT COUNT(*) FROM subagent_activity'),
    errors: scalar(db, 'SELECT COUNT(*) FROM errors'),
    commits: scalar(db, 'SELECT COUNT(*) FROM git_activity'),
    events: scalar(db, 'SELECT COUNT(*) FROM events'),
    metric_points: scalar(db, 'SELECT COUNT(*) FROM metric_points'),
    spans: scalar(db, 'SELECT COUNT(*) FROM spans'),
    prompts: scalar(db, 'SELECT COUNT(*) FROM prompts'),
  }
}

export function sessions(db: Db, limit = 50): Row[] {
  return db.all(
    "SELECT * FROM session_summary ORDER BY COALESCE(first_seen,'') DESC LIMIT ?",
    [limit],
  )
}

export function projects(db: Db): Row[] {
  return db.all(
    'SELECT * FROM project_summary ORDER BY cost_usd DESC, sessions DESC',
  )
}

export function mcpServers(db: Db): Row[] {
  return db.all(`SELECT COALESCE(mcp_server_name,'(unnamed)') AS server,
            COUNT(*) AS calls,
            COUNT(DISTINCT mcp_tool_name) AS distinct_tools,
            COUNT(DISTINCT session_id) AS sessions,
            COUNT(DISTINCT project_id) AS projects,
            SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS failed,
            ROUND(AVG(duration_ms),1) AS avg_ms,
            ROUND(SUM(COALESCE(duration_ms,0))/1000.0,1) AS total_s
       FROM tool_calls WHERE tool_category='mcp'
      GROUP BY server ORDER BY calls DESC`)
}

export function skills(db: Db): Row[] {
  return db.all(`SELECT k.skill_name,
            COUNT(*) AS invocations,
            COUNT(DISTINCT k.session_id) AS sessions,
            COUNT(DISTINCT k.project_id) AS projects,
            (SELECT COALESCE(SUM(a.cost_usd),0) FROM api_calls a
              WHERE a.skill_name = k.skill_name AND a.outcome='ok') AS attributed_cost_usd,
            (SELECT COUNT(*) FROM api_calls a WHERE a.skill_name = k.skill_name) AS attributed_api_calls
       FROM skill_calls k WHERE k.skill_name IS NOT NULL
      GROUP BY k.skill_name ORDER BY invocations DESC`)
}

export function hotFiles(db: Db, limit = 25): Row[] {
  return db.all(
    `SELECT f.path, f.repo_relative_path,
            COALESCE(p.project_name, '—') AS project_name,
            COUNT(*) AS touches,
            SUM(CASE WHEN f.operation='read' THEN 1 ELSE 0 END) AS reads,
            SUM(CASE WHEN f.operation IN ('edit','write','notebook_edit') THEN 1 ELSE 0 END) AS writes,
            MAX(f.created) AS created,
            COUNT(DISTINCT f.session_id) AS sessions
       FROM file_activity f
       LEFT JOIN projects p ON p.project_id = f.project_id
      GROUP BY f.path ORDER BY touches DESC LIMIT ?`,
    [limit],
  )
}

export function createdFiles(db: Db, limit = 50): Row[] {
  return db.all(
    `SELECT path, MIN(ts) AS ts, create_method, create_confidence, session_id
       FROM file_activity WHERE created=1
      GROUP BY path ORDER BY ts DESC LIMIT ?`,
    [limit],
  )
}

export function hotDirs(db: Db, limit = 15): Row[] {
  return db.all(
    `SELECT COALESCE(p.project_name, f.project_id) AS project_name,
            CASE WHEN f.repo_relative_path IS NULL OR instr(f.repo_relative_path,'/')=0
                 THEN '(repo root)'
                 ELSE substr(f.repo_relative_path, 1, instr(f.repo_relative_path,'/')-1) END AS area,
            COUNT(*) AS touches, COUNT(DISTINCT f.path) AS files
       FROM file_activity f
       LEFT JOIN projects p ON p.project_id = f.project_id
      WHERE f.project_id IS NOT NULL
      GROUP BY f.project_id, area ORDER BY touches DESC LIMIT ?`,
    [limit],
  )
}

export function errors(db: Db, limit = 40): Row[] {
  return db.all(
    `SELECT ts, kind, source_event, tool_name, model, error_name, error_code,
            status_code, substr(COALESCE(message,''),1,200) AS message, session_id
       FROM errors ORDER BY ts DESC LIMIT ?`,
    [limit],
  )
}

export function subagents(db: Db): Row[] {
  return db.all(`SELECT COALESCE(subagent_type, agent_name, '(unnamed)') AS agent,
            COUNT(*) AS invocations, COUNT(DISTINCT session_id) AS sessions,
            ROUND(AVG(duration_ms)/1000.0,1) AS avg_s
       FROM subagent_activity GROUP BY agent ORDER BY invocations DESC`)
}

// --- the session: the unit of work ------------------------------------------

/** Sessions that actually did something, and said what it was.
 *
 * Hooks record a row for every session that starts, including ones that
 * exported no telemetry at all. A session also survives with tool activity but
 * no prompt text - the storage policy in force when it was ingested may have
 * dropped the text, or it may be a synthetic test session. Either way there is
 * nothing to name it by and nothing to read, so a session earns its place by
 * having produced at least one prompt of its own.
 */
export function activeSessions(db: Db): Row[] {
  return db.all(`SELECT s.session_id, s.project_id, p.project_name, s.first_seen,
            s.last_seen, s.duration_s,
            COALESCE((SELECT SUM(a.cost_usd) FROM api_calls a
                       WHERE a.session_id=s.session_id AND a.outcome='ok'),0) AS cost,
            (SELECT COUNT(*) FROM turns t WHERE t.session_id=s.session_id
              AND COALESCE(t.is_system,0)=0) AS turns,
            (SELECT COUNT(*) FROM turns t WHERE t.session_id=s.session_id
              AND COALESCE(t.is_system,0)=0
              AND t.prompt_text IS NOT NULL) AS prompt_turns,
            (SELECT COUNT(*) FROM git_activity g
              WHERE g.session_id=s.session_id) AS commits
       FROM sessions s
       LEFT JOIN projects p ON p.project_id = s.project_id
      WHERE s.first_seen IS NOT NULL
        AND EXISTS (SELECT 1 FROM turns t
                     WHERE t.session_id = s.session_id
                       AND t.prompt_text IS NOT NULL
                       AND COALESCE(t.is_system,0)=0)
      ORDER BY COALESCE(s.last_seen, s.first_seen) DESC`)
}

export interface SessionDetail {
  session: Record<string, any>
  description: string | null
  strip: Record<string, any> | undefined
  turns: Row[]
  lanes: Lane[]
  corrections: Row[]
  effort: Record<string, any>
  skills: Row[]
  mcps: Row[]
  commits: Row[]
}

/** Everything about one session, in one row, newest first.
 *
 * One session is one thing to look at. Nothing here is apportioned or shared:
 * the cost is what this session's own API calls cost, the commits are the ones
 * attributed to it, and the effort is its own turns.
 */
export function sessionDetail(db: Db, sessionIds?: string[]): SessionDetail[] {
  let rows = activeSessions(db)
  if (sessionIds !== undefined) {
    const wanted = new Set(sessionIds)
    rows = rows.filter((r) => wanted.has(String(r.session_id)))
  }
  const ids = rows.map((r) => String(r.session_id))
  const strips = new Map<string, Record<string, any>>()
  if (ids.length) {
    for (const entry of sessionStrips(db, 12, ids)) {
      strips.set(String(entry.session.session_id), entry)
    }
  }

  const out: SessionDetail[] = []
  for (const r of rows) {
    const sid = String(r.session_id)
    const strip = strips.get(sid)
    const effort = db.get(
      `SELECT COUNT(*) turns, COALESCE(SUM(is_correction),0) corrections,
              COALESCE(SUM(is_steering),0) steers,
              COALESCE(SUM(user_overrides),0) overrides,
              COALESCE(SUM(rejects),0) rejects,
              COALESCE(SUM(tool_failures),0) tool_failures,
              COALESCE(SUM(CASE WHEN is_correction=1 THEN cost_usd END),0) ccost,
              AVG(gap_before_s) gap
         FROM turns WHERE session_id=? AND COALESCE(is_system,0)=0`,
      [sid],
    )!
    const commits = db.all(
      `SELECT commit_sha, committed_at, subject, insertions, deletions,
              files_changed, commit_type, commit_scope
         FROM git_activity WHERE session_id=?
        ORDER BY committed_at DESC`,
      [sid],
    )
    const reverted = scalar(
      db,
      `SELECT COUNT(*) FROM reverts WHERE reverted_sha IN
         (SELECT commit_sha FROM git_activity WHERE session_id=?)`,
      [sid],
    )
    const usedSkills = db.all(
      `SELECT skill_name, COUNT(*) n FROM skill_calls
        WHERE session_id=? AND skill_name IS NOT NULL
        GROUP BY 1 ORDER BY n DESC`,
      [sid],
    )
    const mcps = db.all(
      `SELECT mcp_server_name srv, COUNT(*) n FROM tool_calls
        WHERE session_id=? AND mcp_server_name IS NOT NULL
        GROUP BY 1 ORDER BY n DESC`,
      [sid],
    )
    const session: Record<string, any> = { ...r }
    session.insertions = commits.reduce(
      (s, c) => s + (Number(c.insertions) || 0),
      0,
    )
    session.deletions = commits.reduce(
      (s, c) => s + (Number(c.deletions) || 0),
      0,
    )
    session.reverted = reverted
    session.title = sessionTitle(db, session)
    out.push({
      session,
      description: sessionDescription(db, sid),
      strip,
      turns: (strip?.turns as Row[]) ?? [],
      lanes: (strip?.lanes as Lane[]) ?? [],
      corrections: (strip?.corrections as Row[]) ?? [],
      effort: {
        turns: effort.turns,
        corrections: effort.corrections,
        steers: effort.steers,
        overrides: effort.overrides,
        rejects: effort.rejects,
        tool_failures: effort.tool_failures,
        correction_cost: effort.ccost,
        gap: effort.gap,
        rework: scalar(
          db,
          'SELECT COUNT(*) FROM file_rework WHERE session_id=?',
          [sid],
        ),
      },
      skills: usedSkills,
      mcps,
      commits,
    })
  }
  return out
}

/** What to call a session in a list.
 *
 * Its description if one has been written, since that is the only thing that
 * says what the session was for. Otherwise the commit scope it worked in, and
 * failing that the short id - never a made-up label.
 */
export function sessionTitle(db: Db, session: Record<string, any>): string {
  const desc = sessionDescription(db, String(session.session_id))
  if (desc) return desc.replace(/\.+$/, '')
  const scope = db.get(
    'SELECT commit_scope FROM git_activity WHERE session_id=? AND commit_scope IS NOT NULL LIMIT 1',
    [session.session_id],
  )
  if (scope) return String(scope.commit_scope)
  return String(session.session_id).slice(0, 8)
}

export function skillUsage(db: Db): Row[] {
  return db.all(`SELECT i.name, i.scope, i.project_id, i.description,
            COALESCE(u.invocations,0) AS invocations,
            COALESCE(u.sessions,0)    AS sessions,
            u.last_used
       FROM skill_inventory i
       LEFT JOIN (
           SELECT skill_name, COUNT(*) invocations,
                  COUNT(DISTINCT session_id) sessions, MAX(ts) last_used
             FROM skill_calls WHERE skill_name IS NOT NULL
            GROUP BY skill_name
       ) u ON lower(u.skill_name) = lower(i.name)
      ORDER BY invocations DESC, i.scope, i.name`)
}

/** Skills seen in telemetry with no matching inventory entry. */
export function skillsUsedNotInstalled(db: Db): Row[] {
  return db.all(`SELECT k.skill_name, COUNT(*) invocations, MAX(k.ts) last_used
       FROM skill_calls k
      WHERE k.skill_name IS NOT NULL
        AND lower(k.skill_name) NOT IN (SELECT lower(name) FROM skill_inventory)
      GROUP BY k.skill_name ORDER BY invocations DESC`)
}

export function mcpUsage(db: Db): Row[] {
  return db.all(`SELECT i.name,
            CASE WHEN (SELECT COUNT(*) FROM mcp_inventory d
                        WHERE lower(d.name)=lower(i.name)) > 1
                 THEN i.scope || '*' ELSE i.scope END AS scope,
            i.transport, i.command, i.project_id,
            COALESCE(c.calls,0)          AS calls,
            COALESCE(c.distinct_tools,0) AS distinct_tools,
            COALESCE(c.sessions,0)       AS sessions,
            c.last_used,
            COALESCE(e.connects,0)       AS connects,
            COALESCE(e.failures,0)       AS failures
       FROM mcp_inventory i
       LEFT JOIN (
           SELECT mcp_server_name, COUNT(*) calls,
                  COUNT(DISTINCT mcp_tool_name) distinct_tools,
                  COUNT(DISTINCT session_id) sessions, MAX(ts) last_used
             FROM tool_calls WHERE mcp_server_name IS NOT NULL
            GROUP BY mcp_server_name
       ) c ON lower(c.mcp_server_name) = lower(i.name)
       LEFT JOIN (
           SELECT json_extract(attrs_json,'$.server_name') AS srv,
                  COUNT(*) connects,
                  SUM(CASE WHEN json_extract(attrs_json,'$.status')='failed'
                           THEN 1 ELSE 0 END) failures
             FROM events WHERE event_name='mcp_server_connection'
            GROUP BY srv
       ) e ON lower(e.srv) = lower(i.name)
      ORDER BY calls DESC, failures DESC, i.name`)
}

// --- human effort / friction -------------------------------------------------

export function frictionBySession(db: Db, limit = 25): Row[] {
  return db.all(
    `SELECT t.session_id, p.project_name,
            COUNT(*) AS turns,
            SUM(t.is_correction) AS corrections,
            SUM(t.is_steering)   AS steers,
            SUM(t.user_overrides) AS overrides,
            SUM(t.rejects) AS rejects,
            SUM(t.tool_failures) AS tool_failures,
            ROUND(SUM(t.cost_usd),2) AS cost_usd,
            ROUND(AVG(t.gap_before_s),0) AS avg_gap_s,
            (SELECT COUNT(*) FROM file_rework r WHERE r.session_id=t.session_id) AS rework_files,
            (SELECT MAX(r.turns) FROM file_rework r WHERE r.session_id=t.session_id) AS worst_file_turns
       FROM turns t
       LEFT JOIN sessions s ON s.session_id=t.session_id
       LEFT JOIN projects p ON p.project_id=s.project_id
      WHERE COALESCE(t.is_system,0)=0
      GROUP BY t.session_id ORDER BY turns DESC LIMIT ?`,
    [limit],
  )
}

export function reworkFiles(db: Db, limit = 20): Row[] {
  return db.all(
    `SELECT COALESCE(repo_relative_path, path) AS path, turns, edits,
            substr(session_id,1,8) AS sess, first_ts, last_ts
       FROM file_rework ORDER BY turns DESC, edits DESC LIMIT ?`,
    [limit],
  )
}

export function corrections(db: Db, limit = 20): Row[] {
  return db.all(
    `SELECT substr(session_id,1,8) AS sess, started_at, correction_cue,
            substr(COALESCE(prompt_text,''),1,110) AS prompt,
            ROUND(cost_usd,2) AS cost_usd
       FROM turns WHERE is_correction=1
      ORDER BY started_at DESC LIMIT ?`,
    [limit],
  )
}

export function frictionTotals(db: Db): Record<string, number> {
  return {
    turns: scalar(
      db,
      'SELECT COUNT(*) FROM turns WHERE COALESCE(is_system,0)=0',
    ),
    corrections: scalar(
      db,
      'SELECT COALESCE(SUM(is_correction),0) FROM turns WHERE COALESCE(is_system,0)=0',
    ),
    steers: scalar(
      db,
      'SELECT COALESCE(SUM(is_steering),0) FROM turns WHERE COALESCE(is_system,0)=0',
    ),
    overrides: scalar(
      db,
      'SELECT COALESCE(SUM(user_overrides),0) FROM turns WHERE COALESCE(is_system,0)=0',
    ),
    rejects: scalar(
      db,
      'SELECT COALESCE(SUM(rejects),0) FROM turns WHERE COALESCE(is_system,0)=0',
    ),
    rework_files: scalar(db, 'SELECT COUNT(*) FROM file_rework'),
    correction_cost: scalar(
      db,
      'SELECT COALESCE(SUM(cost_usd),0) FROM turns WHERE is_correction=1',
      [],
      0,
    ),
    total_cost: scalar(
      db,
      'SELECT COALESCE(SUM(cost_usd),0) FROM turns',
      [],
      0,
    ),
    with_text: scalar(
      db,
      'SELECT COUNT(*) FROM turns WHERE prompt_text IS NOT NULL AND COALESCE(is_system,0)=0',
    ),
    system_turns: scalar(db, 'SELECT COUNT(*) FROM turns WHERE is_system=1'),
    system_cost: scalar(
      db,
      'SELECT COALESCE(SUM(cost_usd),0) FROM turns WHERE is_system=1',
      [],
      0,
    ),
    model_labelled: scalar(
      db,
      "SELECT COUNT(*) FROM turns WHERE label_source='model'",
    ),
  }
}

export function outputSummary(db: Db): Record<string, number> {
  return {
    commits: scalar(db, 'SELECT COUNT(*) FROM git_activity'),
    insertions: scalar(
      db,
      'SELECT COALESCE(SUM(insertions),0) FROM git_activity',
    ),
    deletions: scalar(
      db,
      'SELECT COALESCE(SUM(deletions),0) FROM git_activity',
    ),
    sessions_with_commits: scalar(
      db,
      'SELECT COUNT(DISTINCT session_id) FROM git_activity WHERE session_id IS NOT NULL',
    ),
    reverts: scalar(db, 'SELECT COUNT(*) FROM reverts'),
  }
}

// --- time -------------------------------------------------------------------

export interface Lane {
  kind: string
  name: string | null
  events: Record<string, any>[]
  detail: string | null
}

/** Per session: the ordered turns, for a segmented activity strip.
 *
 * Given explicit ids the sessions come back newest first. The unfiltered form
 * takes the longest `limit` sessions, since there it is picking which sessions
 * are worth showing at all.
 */
export function sessionStrips(
  db: Db,
  limit = 12,
  sessionIds?: string[],
): Record<string, any>[] {
  if (sessionIds !== undefined) {
    if (!sessionIds.length) return []
    const marks = sessionIds.map(() => '?').join(',')
    const rows = db.all(
      `SELECT t.session_id, p.project_name,
              COUNT(*) turns,
              COALESCE(SUM(t.duration_s),0) active_s,
              COALESCE(SUM(t.gap_before_s),0) gap_s,
              MIN(t.started_at) started, MAX(t.ended_at) ended,
              COALESCE(SUM(t.cost_usd),0) cost
         FROM turns t
         LEFT JOIN sessions s ON s.session_id=t.session_id
         LEFT JOIN projects p ON p.project_id=s.project_id
        WHERE t.session_id IN (${marks})
        GROUP BY t.session_id ORDER BY ended DESC, started DESC`,
      sessionIds,
    )
    return stripRows(db, rows)
  }
  const rows = db.all(
    `SELECT t.session_id, p.project_name,
            COUNT(*) turns,
            COALESCE(SUM(t.duration_s),0) active_s,
            COALESCE(SUM(t.gap_before_s),0) gap_s,
            MIN(t.started_at) started, MAX(t.ended_at) ended,
            COALESCE(SUM(t.cost_usd),0) cost
       FROM turns t
       LEFT JOIN sessions s ON s.session_id=t.session_id
       LEFT JOIN projects p ON p.project_id=s.project_id
      GROUP BY t.session_id
      ORDER BY active_s DESC LIMIT ?`,
    [limit],
  )
  return stripRows(db, rows)
}

function stripRows(db: Db, rows: Row[]): Record<string, any>[] {
  const out: Record<string, any>[] = []
  for (const s of rows) {
    const sid = String(s.session_id)
    const turns = db.all(
      `SELECT t.turn_id, t.seq, t.started_at, t.duration_s, t.gap_before_s,
              t.cost_usd, t.is_correction, t.is_steering, t.correction_cue,
              t.tool_calls, t.prompt_length, t.is_system,
              substr(COALESCE(t.prompt_text,''),1,150) AS prompt
         FROM turns t
        WHERE t.session_id=? ORDER BY t.seq`,
      [sid],
    )
    const lanes = [
      ...sessionToolEvents(db, sid, 5),
      ...sessionFileEvents(db, sid),
    ]
    out.push({
      session: { ...s },
      turns,
      lanes,
      corrections: sessionCorrections(db, sid),
      description: sessionDescription(db, sid),
    })
  }
  return out
}

/** Lanes for file access: two aggregates plus the session's hottest files.
 *
 * A lane per file would mean hundreds of rows, so reads and writes get one
 * aggregate lane each, and the few files touched most often in this session get
 * their own so repeat visits are visible.
 */
export function sessionFileEvents(
  db: Db,
  sessionId: string,
  topFiles = 3,
): Lane[] {
  const rows = db.all(
    `SELECT path, operation, ts FROM file_activity
      WHERE session_id = ? AND ts IS NOT NULL
        AND operation IN ('read','write','edit','notebook_edit')
      ORDER BY ts`,
    [sessionId],
  )
  if (!rows.length) return []
  const ev = (r: Row) => ({ ts: r.ts, path: r.path, op: r.operation })

  // Markdown reads are split out: for a knowledge base, "was the doc opened"
  // is a different question from "was the code opened", and mixing them hides
  // both.
  const isMarkdown = (path: unknown) =>
    ['.md', '.mdx', '.markdown'].some((ext) =>
      String(path ?? '')
        .toLowerCase()
        .endsWith(ext),
    )

  const mdReads = rows
    .filter((r) => r.operation === 'read' && isMarkdown(r.path))
    .map(ev)
  const otherReads = rows
    .filter((r) => r.operation === 'read' && !isMarkdown(r.path))
    .map(ev)
  const writes = rows.filter((r) => r.operation !== 'read').map(ev)

  const counts = new Map<string, Record<string, any>[]>()
  for (const r of rows) {
    const key = String(r.path)
    if (!counts.has(key)) counts.set(key, [])
    counts.get(key)!.push(ev(r))
  }
  const hottest = [...counts.entries()]
    .sort((a, b) => b[1].length - a[1].length)
    .slice(0, topFiles)

  const distinct = (events: Record<string, any>[]) =>
    new Set(events.map((e) => e.path)).size

  const lanes: Lane[] = []
  if (mdReads.length) {
    lanes.push({
      kind: 'docs',
      name: 'markdown read',
      events: mdReads,
      detail: `${distinct(mdReads)} distinct .md files`,
    })
  }
  if (otherReads.length) {
    lanes.push({
      kind: 'files',
      name: 'other files read',
      events: otherReads,
      detail: `${distinct(otherReads)} distinct paths`,
    })
  }
  if (writes.length) {
    lanes.push({
      kind: 'files',
      name: 'files written',
      events: writes,
      detail: `${distinct(writes)} distinct paths`,
    })
  }
  const seen = new Set<string>()
  for (const [path, times] of hottest) {
    if (times.length < 2) continue
    // Two different paths can share a basename; add the parent directory
    // rather than showing the same label twice.
    const parts = path.replace(/\/+$/, '').split('/')
    let label = parts[parts.length - 1]!
    if (seen.has(label) && parts.length > 1) label = parts.slice(-2).join('/')
    seen.add(label)
    lanes.push({
      kind: 'hot',
      name: label.slice(0, 28),
      events: times,
      detail: `${path} · also counted in the lanes above`,
    })
  }
  return lanes
}

/** Every skill and MCP call in a session, grouped into lanes by name.
 *
 * Skills are matched from skill_calls (which covers both the Skill tool and
 * api-level `skill.name` attribution); MCP calls come from tool_calls. The
 * first event in a lane is the load; the rest are calls.
 */
export function sessionToolEvents(
  db: Db,
  sessionId: string,
  maxLanes = 8,
): Lane[] {
  // Each event carries what it was, not just when: a dot that cannot say what
  // it represents is just decoration.
  const rows = db.all(
    `SELECT 'skill' AS kind, skill_name AS name, ts,
            COALESCE(invocation_source,'') AS what
       FROM skill_calls
      WHERE session_id = ? AND skill_name IS NOT NULL AND ts IS NOT NULL
     UNION ALL
     SELECT 'mcp' AS kind, mcp_server_name AS name, ts,
            COALESCE(mcp_tool_name, tool_name, '') AS what
       FROM tool_calls
      WHERE session_id = ? AND mcp_server_name IS NOT NULL AND ts IS NOT NULL
      ORDER BY ts`,
    [sessionId, sessionId],
  )
  const lanes = new Map<string, Lane>()
  for (const r of rows) {
    const key = `${r.kind} ${r.name}`
    let lane = lanes.get(key)
    if (!lane) {
      lane = {
        kind: String(r.kind),
        name: (r.name as string) ?? null,
        events: [],
        detail: null,
      }
      lanes.set(key, lane)
    }
    lane.events.push({ ts: r.ts, what: r.what })
  }
  return [...lanes.values()]
    .sort(
      (a, b) =>
        b.events.length - a.events.length ||
        String(a.name).localeCompare(String(b.name)),
    )
    .slice(0, maxLanes)
}

// --- file / knowledge-base access -------------------------------------------

/** Every path touched, with how and how often. */
export function fileAccess(db: Db, prefix?: string | null, limit = 60): Row[] {
  let where = 'WHERE 1=1'
  const params: (string | number)[] = []
  if (prefix) {
    where += ' AND path LIKE ?'
    params.push(`${prefix}%`)
  }
  params.push(limit)
  return db.all(
    `SELECT path,
            COUNT(*) touches,
            SUM(operation='read')   reads,
            SUM(operation='write')  writes,
            SUM(operation='search') searches,
            SUM(operation='delete') deletes,
            SUM(via='shell')        via_shell,
            SUM(via='tool')         via_tool,
            COUNT(DISTINCT session_id) sessions,
            MIN(ts) first_ts, MAX(ts) last_ts
       FROM file_activity ${where}
      GROUP BY path ORDER BY touches DESC, path LIMIT ?`,
    params,
  )
}

/** Every file under `root` matching `pattern`, at any depth. */
function rglob(root: string, pattern: string): string[] {
  const out: string[] = []
  const walk = (dir: string, depth: number) => {
    if (depth > 32) return
    let entries: import('node:fs').Dirent[]
    try {
      entries = readdirSync(dir, { withFileTypes: true })
    } catch {
      return
    }
    for (const entry of entries) {
      const full = join(dir, entry.name)
      if (entry.isDirectory()) {
        walk(full, depth + 1)
      } else if (entry.isFile() && matchesGlob(entry.name, pattern)) {
        out.push(full)
      }
    }
  }
  walk(root, 0)
  return out.sort()
}

/** Files that exist on disk under `root` but were never read.
 *
 * The point of a knowledge base is being consulted. This is the gap: what is
 * written down and never opened.
 */
export function unreadFiles(
  db: Db,
  root: string,
  pattern = '*.md',
): {
  root: string
  exists: boolean
  on_disk: string[]
  read: string[]
  unread: string[]
} {
  if (!existsSync(root)) {
    return { root, exists: false, on_disk: [], read: [], unread: [] }
  }
  const onDisk = rglob(root, pattern)
  const touched = new Set(
    db
      .all(
        "SELECT DISTINCT path FROM file_activity WHERE path LIKE ? AND operation IN ('read','write')",
        [`${root}%`],
      )
      .map((r) => String(r.path)),
  )
  // A search of a directory implies its files were scanned, not opened; keep
  // that distinction rather than crediting every file under a grepped dir.
  return {
    root,
    exists: true,
    on_disk: onDisk,
    read: onDisk.filter((f) => touched.has(f)),
    unread: onDisk.filter((f) => !touched.has(f)),
  }
}

/** Skills installed and visible to this session that it never invoked.
 *
 * Deterministic and complete: every candidate, not a verdict. Whether one of
 * them *should* have fired is a judgement about what the session was doing, and
 * the caller is better placed to make it than a stored answer is - it can read
 * the prompts and the file activity at the same time.
 */
export function sessionUnusedSkills(db: Db, sessionId: string): Row[] {
  return db.all(
    `SELECT i.name, i.scope, substr(i.description, 1, 300) AS description
       FROM skill_inventory i
      WHERE i.name NOT IN (SELECT COALESCE(skill_name,'')
                             FROM skill_calls WHERE session_id = ?)
        -- A project skill only exists inside its own repository. Offering one
        -- that was never on this session's disk would invite a "should have
        -- fired" verdict about a skill it could not see.
        AND (i.scope <> 'project' OR i.project_id = (
                SELECT project_id FROM sessions WHERE session_id = ?))
      ORDER BY i.scope, i.name`,
    [sessionId, sessionId],
  )
}

/** The turns where the human said the agent had got it wrong.
 *
 * The prompt itself, at length, because *why* a correction happened is readable
 * in what was typed - and the caller can read it. There is no stored cause: a
 * taxonomy assigned in advance is a guess frozen into a column, and the same
 * words are right here.
 */
export function sessionCorrections(db: Db, sessionId: string): Row[] {
  return db.all(
    `SELECT seq, started_at, cost_usd, duration_s, gap_before_s,
            correction_cue, label_source, label_confidence,
            substr(prompt_text, 1, 400) AS prompt
       FROM turns
      WHERE session_id = ? AND is_correction = 1
      ORDER BY seq`,
    [sessionId],
  )
}

/** What the session was asked to do, in its own words.
 *
 * The first prompt a human typed, verbatim and truncated. Nothing is generated:
 * a summary would cost a model call per session, and the caller reading this
 * over MCP is already a model - it can summarise a hundred of these in the same
 * breath as answering the actual question, and it can read the exact words
 * rather than someone else's paraphrase.
 */
export function sessionDescription(db: Db, sessionId: string): string | null {
  const row = db.get(
    'SELECT substr(prompt_text, 1, 300) AS p FROM turns' +
      ' WHERE session_id = ? AND COALESCE(is_system,0) = 0' +
      "   AND prompt_text IS NOT NULL AND trim(prompt_text) <> ''" +
      ' ORDER BY seq LIMIT 1',
    [sessionId],
  )
  if (!row || !row.p) return null
  return String(row.p).split(/\s+/).filter(Boolean).join(' ')
}
