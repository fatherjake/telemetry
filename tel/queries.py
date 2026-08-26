"""Reusable read queries shared by the CLI and the HTML report."""
from __future__ import annotations

import sqlite3

from .db import q, scalar


def observation_period(conn) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT MIN(ts) a, MAX(ts) b FROM ("
        " SELECT ts FROM events WHERE ts IS NOT NULL"
        " UNION ALL SELECT ts FROM metric_points WHERE ts IS NOT NULL)"
    ).fetchone()
    return (row["a"], row["b"]) if row else (None, None)


def overview(conn) -> dict:
    a, b = observation_period(conn)
    return {
        "period_start": a,
        "period_end": b,
        # Hooks create a row for every session that starts, including ones that
        # never exported telemetry. Counting those as observed sessions
        # overstates coverage badly - 93 rows for 16 real sessions.
        "sessions": scalar(conn, "SELECT COUNT(*) FROM sessions"
                                 " WHERE first_seen IS NOT NULL"),
        "sessions_hook_only": scalar(conn, "SELECT COUNT(*) FROM sessions"
                                           " WHERE first_seen IS NULL"),
        "projects": scalar(conn, "SELECT COUNT(*) FROM projects WHERE project_id NOT LIKE 'unknown:%'"),
        "cost_usd": scalar(conn, "SELECT COALESCE(SUM(cost_usd),0) FROM api_calls WHERE outcome='ok'", default=0.0),
        "api_calls": scalar(conn, "SELECT COUNT(*) FROM api_calls WHERE outcome='ok'"),
        "api_failures": scalar(conn, "SELECT COUNT(*) FROM api_calls WHERE outcome<>'ok'"),
        "input_tokens": scalar(conn, "SELECT COALESCE(SUM(input_tokens),0) FROM api_calls"),
        "output_tokens": scalar(conn, "SELECT COALESCE(SUM(output_tokens),0) FROM api_calls"),
        "cache_read_tokens": scalar(conn, "SELECT COALESCE(SUM(cache_read_tokens),0) FROM api_calls"),
        "cache_creation_tokens": scalar(conn, "SELECT COALESCE(SUM(cache_creation_tokens),0) FROM api_calls"),
        "tool_calls": scalar(conn, "SELECT COUNT(*) FROM tool_calls"),
        "skill_calls": scalar(conn, "SELECT COUNT(*) FROM skill_calls"),
        "files_read": scalar(conn, "SELECT COUNT(*) FROM file_activity WHERE operation='read'"),
        "distinct_files_read": scalar(conn, "SELECT COUNT(DISTINCT path) FROM file_activity WHERE operation='read'"),
        "files_changed": scalar(conn, "SELECT COUNT(*) FROM file_activity WHERE operation IN ('edit','write','notebook_edit')"),
        "distinct_files_changed": scalar(conn, "SELECT COUNT(DISTINCT path) FROM file_activity WHERE operation IN ('edit','write','notebook_edit')"),
        "files_created": scalar(conn, "SELECT COUNT(DISTINCT path) FROM file_activity WHERE created=1"),
        "bash_commands": scalar(conn, "SELECT COUNT(*) FROM bash_activity"),
        "subagents": scalar(conn, "SELECT COUNT(*) FROM subagent_activity"),
        "errors": scalar(conn, "SELECT COUNT(*) FROM errors"),
        "commits": scalar(conn, "SELECT COUNT(*) FROM git_activity"),
        "events": scalar(conn, "SELECT COUNT(*) FROM events"),
        "metric_points": scalar(conn, "SELECT COUNT(*) FROM metric_points"),
        "spans": scalar(conn, "SELECT COUNT(*) FROM spans"),
        "prompts": scalar(conn, "SELECT COUNT(*) FROM prompts"),
    }


def sessions(conn, limit: int = 50) -> list[sqlite3.Row]:
    return q(conn, """
        SELECT * FROM session_summary
        ORDER BY COALESCE(first_seen,'') DESC LIMIT ?""", (limit,))


def session_models(conn, session_id: str) -> str:
    rows = q(conn, "SELECT DISTINCT model FROM api_calls WHERE session_id=? AND model IS NOT NULL",
             (session_id,))
    return ", ".join(r["model"] for r in rows) or "—"


def session_commits(conn, session_id: str) -> list[str]:
    return [r["commit_sha"][:10] for r in
            q(conn, "SELECT commit_sha FROM git_activity WHERE session_id=?", (session_id,))]


def projects(conn) -> list[sqlite3.Row]:
    return q(conn, "SELECT * FROM project_summary ORDER BY cost_usd DESC, sessions DESC")


def cost_by(conn, dimension: str, limit: int = 25) -> list[sqlite3.Row]:
    allowed = {"model": "model", "day": "substr(ts,1,10)", "query_source": "query_source",
               "skill": "skill_name", "agent": "agent_name", "project": "project_id"}
    expr = allowed[dimension]
    return q(conn, f"""
        SELECT {expr} AS k, COUNT(*) AS api_calls,
               COALESCE(SUM(cost_usd),0) AS cost_usd,
               COALESCE(SUM(input_tokens),0) AS input_tokens,
               COALESCE(SUM(output_tokens),0) AS output_tokens,
               COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
               COALESCE(SUM(cache_creation_tokens),0) AS cache_creation_tokens
          FROM api_calls WHERE outcome='ok' AND {expr} IS NOT NULL
         GROUP BY k ORDER BY cost_usd DESC LIMIT ?""", (limit,))


def mcp_servers(conn) -> list[sqlite3.Row]:
    """MCP activity by server. Claude Code labels every MCP call `mcp_tool`,
    so the server and tool names must come from tool_parameters."""
    return q(conn, """
        SELECT COALESCE(mcp_server_name,'(unnamed)') AS server,
               COUNT(*) AS calls,
               COUNT(DISTINCT mcp_tool_name) AS distinct_tools,
               COUNT(DISTINCT session_id) AS sessions,
               COUNT(DISTINCT project_id) AS projects,
               SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS failed,
               ROUND(AVG(duration_ms),1) AS avg_ms,
               ROUND(SUM(COALESCE(duration_ms,0))/1000.0,1) AS total_s
          FROM tool_calls WHERE tool_category='mcp'
         GROUP BY server ORDER BY calls DESC""")


def mcp_tools(conn, limit: int = 30) -> list[sqlite3.Row]:
    return q(conn, """
        SELECT COALESCE(mcp_server_name,'(unnamed)') AS server,
               COALESCE(mcp_tool_name,'(unnamed)') AS tool,
               COUNT(*) AS calls,
               SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS failed,
               ROUND(AVG(duration_ms),1) AS avg_ms
          FROM tool_calls WHERE tool_category='mcp'
         GROUP BY server, tool ORDER BY calls DESC LIMIT ?""", (limit,))


def tools_breakdown(conn) -> list[sqlite3.Row]:
    return q(conn, """
        SELECT tool_category,
               CASE WHEN tool_category='mcp'
                    THEN COALESCE(mcp_server_name,'?') || '/' || COALESCE(mcp_tool_name,'?')
                    ELSE tool_name END AS tool_name,
               COUNT(*) AS calls,
               SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS ok,
               SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS failed,
               ROUND(AVG(duration_ms),1) AS avg_ms,
               ROUND(SUM(COALESCE(duration_ms,0))/1000.0,1) AS total_s,
               COUNT(DISTINCT session_id) AS sessions
          FROM tool_calls GROUP BY tool_category, tool_name
         ORDER BY calls DESC""")


def tool_categories(conn) -> list[sqlite3.Row]:
    return q(conn, """
        SELECT tool_category, COUNT(*) AS calls,
               COUNT(DISTINCT session_id) AS sessions,
               ROUND(SUM(COALESCE(duration_ms,0))/1000.0,1) AS total_s
          FROM tool_calls GROUP BY tool_category ORDER BY calls DESC""")


def skills(conn) -> list[sqlite3.Row]:
    return q(conn, """
        SELECT k.skill_name,
               COUNT(*) AS invocations,
               COUNT(DISTINCT k.session_id) AS sessions,
               COUNT(DISTINCT k.project_id) AS projects,
               (SELECT COALESCE(SUM(a.cost_usd),0) FROM api_calls a
                 WHERE a.skill_name = k.skill_name AND a.outcome='ok') AS attributed_cost_usd,
               (SELECT COUNT(*) FROM api_calls a WHERE a.skill_name = k.skill_name) AS attributed_api_calls
          FROM skill_calls k WHERE k.skill_name IS NOT NULL
         GROUP BY k.skill_name ORDER BY invocations DESC""")


def hot_files(conn, limit: int = 25) -> list[sqlite3.Row]:
    return q(conn, """
        SELECT f.path, f.repo_relative_path,
               COALESCE(p.project_name, '—') AS project_name,
               COUNT(*) AS touches,
               SUM(CASE WHEN f.operation='read' THEN 1 ELSE 0 END) AS reads,
               SUM(CASE WHEN f.operation IN ('edit','write','notebook_edit') THEN 1 ELSE 0 END) AS writes,
               MAX(f.created) AS created,
               COUNT(DISTINCT f.session_id) AS sessions
          FROM file_activity f
          LEFT JOIN projects p ON p.project_id = f.project_id
         GROUP BY f.path ORDER BY touches DESC LIMIT ?""", (limit,))


def created_files(conn, limit: int = 50) -> list[sqlite3.Row]:
    return q(conn, """
        SELECT path, MIN(ts) AS ts, create_method, create_confidence, session_id
          FROM file_activity WHERE created=1
         GROUP BY path ORDER BY ts DESC LIMIT ?""", (limit,))


def hot_dirs(conn, limit: int = 15) -> list[sqlite3.Row]:
    return q(conn, """
        SELECT COALESCE(p.project_name, f.project_id) AS project_name,
               CASE WHEN f.repo_relative_path IS NULL OR instr(f.repo_relative_path,'/')=0
                    THEN '(repo root)'
                    ELSE substr(f.repo_relative_path, 1, instr(f.repo_relative_path,'/')-1) END AS area,
               COUNT(*) AS touches, COUNT(DISTINCT f.path) AS files
          FROM file_activity f
          LEFT JOIN projects p ON p.project_id = f.project_id
         WHERE f.project_id IS NOT NULL
         GROUP BY f.project_id, area ORDER BY touches DESC LIMIT ?""", (limit,))


def bash_top(conn, limit: int = 20) -> list[sqlite3.Row]:
    return q(conn, """
        SELECT program, COUNT(*) AS runs,
               SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS failures,
               ROUND(AVG(duration_ms),1) AS avg_ms
          FROM bash_activity WHERE program IS NOT NULL
         GROUP BY program ORDER BY runs DESC LIMIT ?""", (limit,))


def errors(conn, limit: int = 40) -> list[sqlite3.Row]:
    return q(conn, """
        SELECT ts, kind, source_event, tool_name, model, error_name, error_code,
               status_code, substr(COALESCE(message,''),1,200) AS message, session_id
          FROM errors ORDER BY ts DESC LIMIT ?""", (limit,))


def error_summary(conn) -> list[sqlite3.Row]:
    return q(conn, """
        SELECT kind, COALESCE(error_name, error_code, status_code, '—') AS detail,
               COUNT(*) AS n FROM errors GROUP BY kind, detail ORDER BY n DESC""")


def git_commits(conn, limit: int = 40) -> list[sqlite3.Row]:
    return q(conn, """
        SELECT g.commit_sha, g.committed_at, g.subject, g.files_changed,
               g.insertions, g.deletions, g.session_id, g.attribution,
               p.project_name
          FROM git_activity g LEFT JOIN projects p ON p.project_id=g.project_id
         ORDER BY g.committed_at DESC LIMIT ?""", (limit,))


def event_coverage(conn) -> list[sqlite3.Row]:
    return q(conn, """
        SELECT event_name, COUNT(*) AS n, MIN(ts) AS first_seen, MAX(ts) AS last_seen
          FROM events GROUP BY event_name ORDER BY n DESC""")


def metric_coverage(conn) -> list[sqlite3.Row]:
    return q(conn, """
        SELECT metric_name, MAX(COALESCE(NULLIF(unit,''),'')) AS unit,
               COUNT(*) AS points, ROUND(SUM(value),3) AS total
          FROM metric_points GROUP BY metric_name ORDER BY points DESC""")


def span_coverage(conn) -> list[sqlite3.Row]:
    return q(conn, """
        SELECT name, COUNT(*) AS n, ROUND(AVG(duration_ms),1) AS avg_ms
          FROM spans GROUP BY name ORDER BY n DESC""")


def subagents(conn) -> list[sqlite3.Row]:
    return q(conn, """
        SELECT COALESCE(subagent_type, agent_name, '(unnamed)') AS agent,
               COUNT(*) AS invocations, COUNT(DISTINCT session_id) AS sessions,
               ROUND(AVG(duration_ms)/1000.0,1) AS avg_s
          FROM subagent_activity GROUP BY agent ORDER BY invocations DESC""")


# --- the session: the unit of work ------------------------------------------

def active_sessions(conn) -> list[sqlite3.Row]:
    """Sessions that actually did something, and said what it was.

    Hooks record a row for every session that starts, including ones that
    exported no telemetry at all. A session also survives with tool activity
    but no prompt text - the storage policy in force when it was ingested may
    have dropped the text, or it may be a synthetic test session. Either way
    there is nothing to name it by and nothing to read, so a session earns its
    place by having produced at least one prompt of its own.
    """
    return q(conn, """
        SELECT s.session_id, s.project_id, p.project_name, s.first_seen,
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
         ORDER BY COALESCE(s.last_seen, s.first_seen) DESC""")


def session_detail(conn, session_ids: list[str] | None = None) -> list[dict]:
    """Everything about one session, in one row, newest first.

    One session is one thing to look at. Nothing here is apportioned or
    shared: the cost is what this session's own API calls cost, the commits
    are the ones attributed to it, and the effort is its own turns.
    """
    rows = active_sessions(conn)
    if session_ids is not None:
        wanted = set(session_ids)
        rows = [r for r in rows if r["session_id"] in wanted]
    ids = [r["session_id"] for r in rows]
    strips = {e["session"]["session_id"]: e
              for e in session_strips(conn, session_ids=ids)} if ids else {}

    out = []
    for r in rows:
        sid = r["session_id"]
        strip = strips.get(sid)
        effort = conn.execute("""
            SELECT COUNT(*) turns, COALESCE(SUM(is_correction),0) corrections,
                   COALESCE(SUM(is_steering),0) steers,
                   COALESCE(SUM(user_overrides),0) overrides,
                   COALESCE(SUM(rejects),0) rejects,
                   COALESCE(SUM(tool_failures),0) tool_failures,
                   COALESCE(SUM(CASE WHEN is_correction=1 THEN cost_usd END),0) ccost,
                   AVG(gap_before_s) gap
              FROM turns WHERE session_id=? AND COALESCE(is_system,0)=0""",
            (sid,)).fetchone()
        commits = [dict(c) for c in conn.execute("""
            SELECT commit_sha, committed_at, subject, insertions, deletions,
                   files_changed, commit_type, commit_scope
              FROM git_activity WHERE session_id=?
             ORDER BY committed_at DESC""", (sid,))]
        deploys = [dict(d) for d in conn.execute("""
            SELECT provider, environment, created_at, commit_sha FROM deployments
             WHERE commit_sha IN (SELECT commit_sha FROM git_activity
                                   WHERE session_id=?)
             ORDER BY created_at DESC""", (sid,))]
        reverted = scalar(conn, """
            SELECT COUNT(*) FROM reverts WHERE reverted_sha IN
              (SELECT commit_sha FROM git_activity WHERE session_id=?)""", (sid,))
        skills = [dict(x) for x in conn.execute("""
            SELECT skill_name, COUNT(*) n FROM skill_calls
             WHERE session_id=? AND skill_name IS NOT NULL
             GROUP BY 1 ORDER BY n DESC""", (sid,))]
        mcps = [dict(x) for x in conn.execute("""
            SELECT mcp_server_name srv, COUNT(*) n FROM tool_calls
             WHERE session_id=? AND mcp_server_name IS NOT NULL
             GROUP BY 1 ORDER BY n DESC""", (sid,))]
        try:
            from .docs import gaps_for_session, docs_read_by_sessions
            doc_gaps = gaps_for_session(conn, sid)
            docs_read = docs_read_by_sessions(conn, [sid])
        except Exception:
            doc_gaps, docs_read = [], []
        session = dict(r)
        session["insertions"] = sum(c["insertions"] or 0 for c in commits)
        session["deletions"] = sum(c["deletions"] or 0 for c in commits)
        session["prod_deploys"] = sum(1 for d in deploys
                                      if d["environment"] == "production")
        session["reverted"] = reverted
        session["title"] = session_title(conn, session)
        out.append({
            "session": session,
            "description": _session_description(conn, sid),
            "strip": strip,
            "turns": (strip or {}).get("turns") or [],
            "lanes": (strip or {}).get("lanes") or [],
            "corrections": (strip or {}).get("corrections") or [],
            "missed_skills": (strip or {}).get("missed_skills") or [],
            "findings": (strip or {}).get("findings") or [],
            "effort": {
                "turns": effort["turns"], "corrections": effort["corrections"],
                "steers": effort["steers"], "overrides": effort["overrides"],
                "rejects": effort["rejects"],
                "tool_failures": effort["tool_failures"],
                "correction_cost": effort["ccost"], "gap": effort["gap"],
                "rework": scalar(conn, "SELECT COUNT(*) FROM file_rework"
                                       " WHERE session_id=?", (sid,)),
            },
            "skills": skills, "mcps": mcps, "commits": commits,
            "deploys": deploys, "doc_gaps": doc_gaps, "docs_read": docs_read,
        })
    return out


def session_title(conn, session: dict) -> str:
    """What to call a session in a list.

    Its description if one has been written, since that is the only thing that
    says what the session was for. Otherwise the commit scope it worked in, and
    failing that the short id - never a made-up label.
    """
    desc = _session_description(conn, session["session_id"])
    if desc:
        return desc.rstrip(".")
    scope = conn.execute(
        "SELECT commit_scope FROM git_activity WHERE session_id=?"
        " AND commit_scope IS NOT NULL LIMIT 1", (session["session_id"],)).fetchone()
    if scope:
        return scope["commit_scope"]
    return session["session_id"][:8]


def unattributed_cost(conn) -> dict:
    """Cost that could not be tied to a project or to a skill/agent."""
    return {
        "no_project": scalar(conn, "SELECT COALESCE(SUM(cost_usd),0) FROM api_calls"
                                   " WHERE outcome='ok' AND (project_id IS NULL OR project_id LIKE 'unknown:%')",
                             default=0.0),
        "no_skill": scalar(conn, "SELECT COALESCE(SUM(cost_usd),0) FROM api_calls"
                                 " WHERE outcome='ok' AND skill_name IS NULL", default=0.0),
        "no_cost_field": scalar(conn, "SELECT COUNT(*) FROM api_calls WHERE outcome='ok' AND cost_usd IS NULL"),
        "total": scalar(conn, "SELECT COALESCE(SUM(cost_usd),0) FROM api_calls WHERE outcome='ok'", default=0.0),
    }


def deployments(conn, limit: int = 30) -> list:
    return q(conn, """
        SELECT d.provider, d.environment, d.created_at, d.commit_sha, d.branch,
               d.service, g.commit_scope AS scope, g.subject
          FROM deployments d
          LEFT JOIN git_activity g ON g.commit_sha = d.commit_sha
         WHERE d.created_at IS NOT NULL
         ORDER BY d.created_at DESC LIMIT ?""", (limit,))


def deploy_coverage(conn) -> dict:
    return {
        "total": scalar(conn, "SELECT COUNT(*) FROM deployments"),
        "with_commit": scalar(conn, "SELECT COUNT(*) FROM deployments WHERE commit_sha IS NOT NULL"),
        "matched_to_git": scalar(conn, "SELECT COUNT(*) FROM deployments d WHERE EXISTS"
                                       " (SELECT 1 FROM git_activity g WHERE g.commit_sha=d.commit_sha)"),
        "prs": scalar(conn, "SELECT COUNT(*) FROM pull_requests"),
        "reverts": scalar(conn, "SELECT COUNT(*) FROM reverts"),
        "outcomes": scalar(conn, "SELECT COUNT(*) FROM outcomes"),
    }


# --- skill / MCP inventory vs usage -----------------------------------------

def skill_usage(conn) -> list:
    """Installed skills joined to observed use. Rows with 0 invocations are
    the point of this query."""
    return q(conn, """
        SELECT i.name, i.scope, i.project_id, i.description,
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
         ORDER BY invocations DESC, i.scope, i.name""")


def skills_used_not_installed(conn) -> list:
    """Skills seen in telemetry with no matching inventory entry."""
    return q(conn, """
        SELECT k.skill_name, COUNT(*) invocations, MAX(k.ts) last_used
          FROM skill_calls k
         WHERE k.skill_name IS NOT NULL
           AND lower(k.skill_name) NOT IN (SELECT lower(name) FROM skill_inventory)
         GROUP BY k.skill_name ORDER BY invocations DESC""")


def mcp_usage(conn) -> list:
    return q(conn, """
        SELECT i.name,
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
         ORDER BY calls DESC, failures DESC, i.name""")


# --- human effort / friction -------------------------------------------------

def friction_by_session(conn, limit: int = 25) -> list:
    return q(conn, """
        SELECT t.session_id, p.project_name,
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
         GROUP BY t.session_id ORDER BY turns DESC LIMIT ?""", (limit,))


def rework_files(conn, limit: int = 20) -> list:
    return q(conn, """
        SELECT COALESCE(repo_relative_path, path) AS path, turns, edits,
               substr(session_id,1,8) AS sess, first_ts, last_ts
          FROM file_rework ORDER BY turns DESC, edits DESC LIMIT ?""", (limit,))


def corrections(conn, limit: int = 20) -> list:
    return q(conn, """
        SELECT substr(session_id,1,8) AS sess, started_at, correction_cue,
               substr(COALESCE(prompt_text,''),1,110) AS prompt,
               ROUND(cost_usd,2) AS cost_usd
          FROM turns WHERE is_correction=1
         ORDER BY started_at DESC LIMIT ?""", (limit,))


def friction_totals(conn) -> dict:
    return {
        "turns": scalar(conn, "SELECT COUNT(*) FROM turns WHERE COALESCE(is_system,0)=0"),
        "corrections": scalar(conn, "SELECT COALESCE(SUM(is_correction),0) FROM turns WHERE COALESCE(is_system,0)=0"),
        "steers": scalar(conn, "SELECT COALESCE(SUM(is_steering),0) FROM turns WHERE COALESCE(is_system,0)=0"),
        "overrides": scalar(conn, "SELECT COALESCE(SUM(user_overrides),0) FROM turns WHERE COALESCE(is_system,0)=0"),
        "rejects": scalar(conn, "SELECT COALESCE(SUM(rejects),0) FROM turns WHERE COALESCE(is_system,0)=0"),
        "rework_files": scalar(conn, "SELECT COUNT(*) FROM file_rework"),
        "correction_cost": scalar(conn, "SELECT COALESCE(SUM(cost_usd),0) FROM turns"
                                        " WHERE is_correction=1", default=0.0),
        "total_cost": scalar(conn, "SELECT COALESCE(SUM(cost_usd),0) FROM turns", default=0.0),
        "with_text": scalar(conn, "SELECT COUNT(*) FROM turns"
                                  " WHERE prompt_text IS NOT NULL AND COALESCE(is_system,0)=0"),
        "system_turns": scalar(conn, "SELECT COUNT(*) FROM turns WHERE is_system=1"),
        "system_cost": scalar(conn, "SELECT COALESCE(SUM(cost_usd),0) FROM turns"
                                    " WHERE is_system=1", default=0.0),
        "model_labelled": scalar(conn, "SELECT COUNT(*) FROM turns"
                                       " WHERE label_source='model'"),
    }


def output_summary(conn) -> dict:
    return {
        "commits": scalar(conn, "SELECT COUNT(*) FROM git_activity"),
        "insertions": scalar(conn, "SELECT COALESCE(SUM(insertions),0) FROM git_activity"),
        "deletions": scalar(conn, "SELECT COALESCE(SUM(deletions),0) FROM git_activity"),
        "prod_deploys": scalar(conn, "SELECT COUNT(*) FROM deployments WHERE environment='production'"),
        "staging_deploys": scalar(conn, "SELECT COUNT(*) FROM deployments WHERE environment='staging'"),
        "sessions_shipped": scalar(conn, """
            SELECT COUNT(DISTINCT g.session_id) FROM git_activity g
             JOIN deployments d ON d.commit_sha=g.commit_sha
            WHERE d.environment='production' AND g.session_id IS NOT NULL"""),
        "sessions_with_commits": scalar(
            conn, "SELECT COUNT(DISTINCT session_id) FROM git_activity"
                  " WHERE session_id IS NOT NULL"),
        "reverts": scalar(conn, "SELECT COUNT(*) FROM reverts"),
        "prs": scalar(conn, "SELECT COUNT(*) FROM pull_requests"),
    }


# --- time -------------------------------------------------------------------

def time_components(conn) -> dict:
    """Durations by component.

    These OVERLAP: an llm_request happens inside an interaction, and tools can
    run in parallel, so they must not be presented as a stacked composition.
    Only `turn_wall` and `human_gap` are disjoint and together tile elapsed
    time - that pair is the only valid stack here.
    """
    def span_total(name):
        return scalar(conn, "SELECT COALESCE(SUM(duration_ms),0)/1000.0 FROM spans"
                            " WHERE name=?", (name,), default=0.0)
    return {
        "interaction": span_total("claude_code.interaction"),
        "llm_request": span_total("claude_code.llm_request"),
        "tool_span": span_total("claude_code.tool"),
        "tool_execution": span_total("claude_code.tool.execution"),
        "blocked_on_user": span_total("claude_code.tool.blocked_on_user"),
        "turn_wall": scalar(conn, "SELECT COALESCE(SUM(duration_s),0) FROM turns",
                            default=0.0),
        "human_gap": scalar(conn, "SELECT COALESCE(SUM(gap_before_s),0) FROM turns",
                            default=0.0),
        "correction_wall": scalar(conn, "SELECT COALESCE(SUM(duration_s),0) FROM turns"
                                        " WHERE is_correction=1", default=0.0),
        "steering_wall": scalar(conn, "SELECT COALESCE(SUM(duration_s),0) FROM turns"
                                      " WHERE is_steering=1", default=0.0),
        "api_latency": scalar(conn, "SELECT COALESCE(SUM(duration_ms),0)/1000.0"
                                    " FROM api_calls", default=0.0),
    }


def time_by_tool_category(conn) -> list:
    return q(conn, """
        SELECT tool_category, COUNT(*) calls,
               ROUND(SUM(duration_ms)/1000.0,1) total_s,
               ROUND(AVG(duration_ms),0) avg_ms,
               ROUND(MAX(duration_ms)/1000.0,1) max_s
          FROM tool_calls WHERE duration_ms IS NOT NULL
         GROUP BY 1 ORDER BY total_s DESC""")


def time_by_tool(conn, limit: int = 15) -> list:
    return q(conn, """
        SELECT CASE WHEN tool_category='mcp'
                    THEN COALESCE(mcp_server_name,'?')||'/'||COALESCE(mcp_tool_name,'?')
                    ELSE tool_name END AS tool,
               tool_category, COUNT(*) calls,
               ROUND(SUM(duration_ms)/1000.0,1) total_s,
               ROUND(AVG(duration_ms),0) avg_ms
          FROM tool_calls WHERE duration_ms IS NOT NULL
         GROUP BY 1,2 ORDER BY total_s DESC LIMIT ?""", (limit,))


def time_by_skill(conn) -> list:
    """Wall-clock of the turns a skill was active in.

    The Skill tool call itself takes milliseconds - it only loads instructions.
    The meaningful figure is how long the turns that used it ran, so a skill is
    matched to whichever turn contains its timestamp. That is an upper bound on
    the skill's own cost, not a measurement of it: other work happened in those
    turns too.
    """
    return q(conn, """
        WITH skill_turn AS (
            SELECT DISTINCT k.skill_name, t.turn_id
              FROM skill_calls k
              JOIN turns t
                ON t.session_id = k.session_id
               AND k.ts >= t.started_at
               AND (t.ended_at IS NULL OR k.ts <= t.ended_at)
             WHERE k.skill_name IS NOT NULL
        )
        SELECT st.skill_name,
               COUNT(*)                        AS turns,
               ROUND(SUM(t.duration_s),1)      AS total_s,
               ROUND(AVG(t.duration_s),1)      AS avg_s,
               ROUND(SUM(t.cost_usd),2)        AS cost_usd,
               SUM(t.is_correction)            AS corrections,
               (SELECT COUNT(*) FROM skill_calls k2
                 WHERE k2.skill_name = st.skill_name) AS invocations
          FROM skill_turn st
          JOIN turns t ON t.turn_id = st.turn_id
         GROUP BY st.skill_name ORDER BY total_s DESC""")


def session_strips(conn, limit: int = 12,
                   session_ids: list[str] | None = None) -> list[dict]:
    """Per session: the ordered turns, for a segmented activity strip.

    Given explicit ids the sessions come back newest first. The unfiltered form
    takes the longest `limit` sessions, since there it is picking which
    sessions are worth showing at all.
    """
    if session_ids is not None:
        if not session_ids:
            return []
        marks = ",".join("?" for _ in session_ids)
        sessions = q(conn, f"""
            SELECT t.session_id, p.project_name,
                   COUNT(*) turns,
                   COALESCE(SUM(t.duration_s),0) active_s,
                   COALESCE(SUM(t.gap_before_s),0) gap_s,
                   MIN(t.started_at) started, MAX(t.ended_at) ended,
                   COALESCE(SUM(t.cost_usd),0) cost
              FROM turns t
              LEFT JOIN sessions s ON s.session_id=t.session_id
              LEFT JOIN projects p ON p.project_id=s.project_id
             WHERE t.session_id IN ({marks})
             GROUP BY t.session_id ORDER BY ended DESC, started DESC""",
            tuple(session_ids))
        return _strip_rows(conn, sessions)
    sessions = q(conn, """
        SELECT t.session_id, p.project_name,
               COUNT(*) turns,
               COALESCE(SUM(t.duration_s),0) active_s,
               COALESCE(SUM(t.gap_before_s),0) gap_s,
               MIN(t.started_at) started, MAX(t.ended_at) ended,
               COALESCE(SUM(t.cost_usd),0) cost
          FROM turns t
          LEFT JOIN sessions s ON s.session_id=t.session_id
          LEFT JOIN projects p ON p.project_id=s.project_id
         GROUP BY t.session_id
         ORDER BY active_s DESC LIMIT ?""", (limit,))
    return _strip_rows(conn, sessions)


def _strip_rows(conn, sessions) -> list[dict]:
    out = []
    for s in sessions:
        turns = q(conn, """
            SELECT t.turn_id, t.seq, t.started_at, t.duration_s, t.gap_before_s,
                   t.cost_usd, t.is_correction, t.is_steering, t.correction_cue,
                   t.tool_calls, t.prompt_length, t.is_system,
                   substr(COALESCE(t.prompt_text,''),1,150) AS prompt,
                   c.cause, c.fix_location, c.suggested_fix
              FROM turns t
              LEFT JOIN correction_cause c ON c.turn_id = t.turn_id
             WHERE t.session_id=? ORDER BY t.seq""", (s["session_id"],))
        lanes = (session_tool_events(conn, s["session_id"], max_lanes=5)
                 + session_file_events(conn, s["session_id"]))
        out.append({"session": dict(s), "turns": [dict(t) for t in turns],
                    "lanes": lanes,
                    "missed_skills": session_missed_skills(conn, s["session_id"]),
                    "corrections": session_corrections(conn, s["session_id"]),
                    "docs_read": _session_docs(conn, s["session_id"]),
                    "findings": session_findings(conn, s["session_id"]),
                    "description": _session_description(conn, s["session_id"])})
    return out


def session_file_events(conn, session_id: str, top_files: int = 3) -> list[dict]:
    """Lanes for file access: two aggregates plus the session's hottest files.

    A lane per file would mean hundreds of rows, so reads and writes get one
    aggregate lane each, and the few files touched most often in this session
    get their own so repeat visits are visible.
    """
    rows = q(conn, """
        SELECT path, operation, ts FROM file_activity
         WHERE session_id = ? AND ts IS NOT NULL
           AND operation IN ('read','write','edit','notebook_edit')
         ORDER BY ts""", (session_id,))
    if not rows:
        return []
    def ev(r):
        return {"ts": r["ts"], "path": r["path"], "op": r["operation"]}

    # Markdown reads are split out: for a knowledge base, "was the doc opened"
    # is a different question from "was the code opened", and mixing them hides
    # both.
    def is_markdown(path):
        return (path or "").lower().endswith((".md", ".mdx", ".markdown"))

    md_reads = [ev(r) for r in rows
                if r["operation"] == "read" and is_markdown(r["path"])]
    other_reads = [ev(r) for r in rows
                   if r["operation"] == "read" and not is_markdown(r["path"])]
    writes = [ev(r) for r in rows if r["operation"] != "read"]

    counts: dict[str, list] = {}
    for r in rows:
        counts.setdefault(r["path"], []).append(ev(r))
    hottest = sorted(counts.items(), key=lambda kv: -len(kv[1]))[:top_files]

    def distinct(events):
        return len({e["path"] for e in events})

    lanes = []
    if md_reads:
        lanes.append({"kind": "docs", "name": "markdown read", "events": md_reads,
                      "detail": f"{distinct(md_reads)} distinct .md files"})
    if other_reads:
        lanes.append({"kind": "files", "name": "other files read",
                      "events": other_reads,
                      "detail": f"{distinct(other_reads)} distinct paths"})
    if writes:
        lanes.append({"kind": "files", "name": "files written", "events": writes,
                      "detail": f"{distinct(writes)} distinct paths"})
    seen: set[str] = set()
    for path, times in hottest:
        if len(times) < 2:
            continue
        # Two different paths can share a basename; add the parent directory
        # rather than showing the same label twice.
        parts = path.rstrip("/").split("/")
        label = parts[-1]
        if label in seen and len(parts) > 1:
            label = "/".join(parts[-2:])
        seen.add(label)
        lanes.append({"kind": "hot", "name": label[:28], "events": times,
                      "detail": f"{path} · also counted in the lanes above"})
    return lanes


def session_tool_events(conn, session_id: str, max_lanes: int = 8) -> list[dict]:
    """Every skill and MCP call in a session, grouped into lanes by name.

    Skills are matched from skill_calls (which covers both the Skill tool and
    api-level `skill.name` attribution); MCP calls come from tool_calls. The
    first event in a lane is the load; the rest are calls.
    """
    # Each event carries what it was, not just when: the TUI lets you step
    # through the dots, and a dot that cannot say what it represents is just
    # decoration.
    rows = q(conn, """
        SELECT 'skill' AS kind, skill_name AS name, ts,
               COALESCE(invocation_source,'') AS what
          FROM skill_calls
         WHERE session_id = ? AND skill_name IS NOT NULL AND ts IS NOT NULL
        UNION ALL
        SELECT 'mcp' AS kind, mcp_server_name AS name, ts,
               COALESCE(mcp_tool_name, tool_name, '') AS what
          FROM tool_calls
         WHERE session_id = ? AND mcp_server_name IS NOT NULL AND ts IS NOT NULL
         ORDER BY ts""", (session_id, session_id))
    lanes: dict[tuple, dict] = {}
    for r in rows:
        key = (r["kind"], r["name"])
        lane = lanes.setdefault(key, {"kind": r["kind"], "name": r["name"],
                                      "events": [], "detail": None})
        lane["events"].append({"ts": r["ts"], "what": r["what"]})
    out = sorted(lanes.values(), key=lambda l: (-len(l["events"]), l["name"]))
    return out[:max_lanes]


# --- file / knowledge-base access -------------------------------------------

def file_access(conn, prefix: str | None = None, limit: int = 60) -> list:
    """Every path touched, with how and how often."""
    where, params = "WHERE 1=1", []
    if prefix:
        where += " AND path LIKE ?"
        params.append(f"{prefix}%")
    params.append(limit)
    return q(conn, f"""
        SELECT path,
               COUNT(*) touches,
               SUM(operation='read')   reads,
               SUM(operation='write')  writes,
               SUM(operation='search') searches,
               SUM(operation='delete') deletes,
               SUM(via='shell')        via_shell,
               SUM(via='tool')         via_tool,
               COUNT(DISTINCT session_id) sessions,
               MIN(ts) first_ts, MAX(ts) last_ts
          FROM file_activity {where}
         GROUP BY path ORDER BY touches DESC, path LIMIT ?""", tuple(params))


def unread_files(conn, root: str, pattern: str = "*.md") -> dict:
    """Files that exist on disk under `root` but were never read.

    The point of a knowledge base is being consulted. This is the gap: what is
    written down and never opened.
    """
    from pathlib import Path as _P
    base = _P(root)
    if not base.exists():
        return {"root": root, "exists": False, "on_disk": [], "read": [],
                "unread": []}
    on_disk = sorted(str(f) for f in base.rglob(pattern) if f.is_file())
    touched = {r["path"] for r in q(
        conn, "SELECT DISTINCT path FROM file_activity WHERE path LIKE ?"
              " AND operation IN ('read','write')", (f"{root}%",))}
    # A search of a directory implies its files were scanned, not opened; keep
    # that distinction rather than crediting every file under a grepped dir.
    read = [f for f in on_disk if f in touched]
    return {"root": root, "exists": True, "on_disk": on_disk, "read": read,
            "unread": [f for f in on_disk if f not in touched]}


def session_missed_skills(conn, session_id: str) -> list[dict]:
    """Skills judged to have been applicable to this session but never invoked."""
    return [dict(r) for r in q(conn, """
        SELECT skill_name, confidence, reason, was_read
          FROM skill_audit
         WHERE session_id = ? AND verdict = 'should_have_fired'
         ORDER BY was_read DESC, confidence = 'high' DESC, skill_name""",
        (session_id,))]


def session_corrections(conn, session_id: str) -> list[dict]:
    """Diagnosed corrections belonging to one session."""
    return [dict(r) for r in q(conn, """
        SELECT c.cause, c.confidence, c.what_was_missing, c.suggested_fix,
               c.fix_location, t.cost_usd, substr(t.prompt_text,1,120) prompt
          FROM correction_cause c JOIN turns t ON t.turn_id = c.turn_id
         WHERE c.session_id = ?
         ORDER BY t.cost_usd DESC""", (session_id,))]


def _session_docs(conn, session_id: str) -> list[dict]:
    try:
        from .docs import docs_read_by_sessions
        return docs_read_by_sessions(conn, [session_id])
    except Exception:
        return []


def _session_description(conn, session_id: str) -> str | None:
    try:
        from .narrate import get
        return get(conn, "session", session_id)
    except Exception:
        return None


def session_findings(conn, session_id: str) -> list:
    """Model-written findings for one session, worst first. Empty until
    `./telemetry enrich --what diagnose` has run - the analysis costs money, so it is never
    triggered implicitly."""
    return q(conn, """
        SELECT kind, evidence, finding, fix, fix_location, severity, confidence
          FROM session_diagnosis
         WHERE session_id = ? AND kind <> 'working_as_intended'
         ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1
                                ELSE 2 END, seq""", (session_id,))
