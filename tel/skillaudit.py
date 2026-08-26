"""Did a skill that was available fail to fire when it should have?

The skill inventory answers "what is never used", which is vague: a skill can be
unused because nothing called for it. The useful question is narrower - given
what a session actually did, should a particular installed skill have fired?

A skill whose SKILL.md was *read* and then not invoked is the strongest case:
the agent considered it and declined, which points at the description rather
than at discoverability.
"""
from __future__ import annotations

import datetime as _dt
import json

from . import config, db
from .classify import _extract_json

MODEL = "haiku"
MAX_SKILLS = 30
MAX_PROMPTS = 12

SYSTEM = """You audit whether a coding agent missed a skill it had available.

You are given what one session actually did, and a list of skills that were
installed and available but never invoked. For each skill decide:

- "should_have_fired": the session did work this skill explicitly covers, by
  hand or with lower-level tools. Be strict - the work must clearly match the
  skill's stated purpose, not merely touch the same area.
- "not_applicable": the session did not call for it.

Weigh "skill_md_was_read: true" heavily. It means the agent opened the skill,
considered it, and chose not to use it — so if the work matched, this is a
strong miss.

Judge only against the described purpose. Do not invent capability the
description does not claim.

Output ONLY a JSON array, no prose and no code fences:
[{"skill":"name","verdict":"should_have_fired","confidence":"high","reason":"under 15 words"}]
Include an entry for every skill you were given."""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def session_summary(conn, session_id: str) -> dict:
    """A compact, faithful picture of one session."""
    s = conn.execute("""
        SELECT s.session_id, p.project_name, s.first_seen, s.duration_s,
               (SELECT COALESCE(SUM(cost_usd),0) FROM api_calls a
                 WHERE a.session_id=s.session_id AND a.outcome='ok') cost
          FROM sessions s LEFT JOIN projects p ON p.project_id=s.project_id
         WHERE s.session_id=?""", (session_id,)).fetchone()
    if not s:
        return {}

    def col(sql, params=(session_id,)):
        return [dict(r) for r in conn.execute(sql, params)]

    prompts = col("""SELECT substr(prompt_text,1,240) t FROM turns
                      WHERE session_id=? AND prompt_text IS NOT NULL
                        AND COALESCE(is_system,0)=0
                      ORDER BY seq LIMIT ?""", (session_id, MAX_PROMPTS))
    tools = col("""SELECT tool_name n, COUNT(*) c FROM tool_calls
                    WHERE session_id=? GROUP BY 1 ORDER BY c DESC LIMIT 12""")
    progs = col("""SELECT program n, COUNT(*) c FROM bash_activity
                    WHERE session_id=? AND program IS NOT NULL
                    GROUP BY 1 ORDER BY c DESC LIMIT 12""")
    mcps = col("""SELECT mcp_server_name n, COUNT(*) c FROM tool_calls
                   WHERE session_id=? AND mcp_server_name IS NOT NULL
                   GROUP BY 1 ORDER BY c DESC LIMIT 8""")
    files = col("""SELECT COALESCE(repo_relative_path, path) n, COUNT(*) c
                     FROM file_activity WHERE session_id=?
                    GROUP BY 1 ORDER BY c DESC LIMIT 15""")
    used = col("""SELECT skill_name n, COUNT(*) c FROM skill_calls
                   WHERE session_id=? AND skill_name IS NOT NULL
                   GROUP BY 1 ORDER BY c DESC""")
    commits = col("""SELECT substr(subject,1,90) n FROM git_activity
                      WHERE session_id=? LIMIT 8""")
    return {
        "project": s["project_name"],
        "started": (s["first_seen"] or "")[:16],
        "minutes": round((s["duration_s"] or 0) / 60, 1),
        "cost_usd": round(s["cost"] or 0, 2),
        "prompts": [p["t"] for p in prompts],
        "tools": {t["n"]: t["c"] for t in tools if t["n"]},
        "bash_programs": {p["n"]: p["c"] for p in progs},
        "mcp_servers": {m["n"]: m["c"] for m in mcps},
        "files_touched": [f["n"] for f in files],
        "skills_actually_used": {u["n"]: u["c"] for u in used},
        "commits": [c["n"] for c in commits],
    }


def candidates(conn, session_id: str) -> list[dict]:
    """Skills that were installed and available but not invoked here.

    Project-scoped skills only apply to their own project; user and plugin
    skills apply everywhere.
    """
    project_id = db.scalar(conn, "SELECT project_id FROM sessions WHERE session_id=?",
                           (session_id,), default=None)
    rows = db.q(conn, """
        SELECT i.name, i.scope, i.description
          FROM skill_inventory i
         WHERE (i.scope <> 'project' OR i.project_id = ?)
           AND i.description IS NOT NULL AND TRIM(i.description) <> ''
           AND lower(i.name) NOT IN (
               SELECT lower(skill_name) FROM skill_calls
                WHERE session_id = ? AND skill_name IS NOT NULL)
         ORDER BY i.scope='project' DESC, i.name""", (project_id, session_id))
    def skill_files(op):
        return {r["n"].lower() for r in conn.execute(f"""
            SELECT DISTINCT replace(replace(substr(path, instr(path,'skills/')+7),
                                            '/SKILL.md',''),'/','') n
              FROM file_activity
             WHERE session_id=? AND operation='{op}'
               AND path LIKE '%skills/%SKILL.md'""", (session_id,))}

    read = skill_files("read")
    # A skill whose SKILL.md was written during the session was being authored
    # then, so it did not exist to be used. The inventory is current state and
    # has no history, so this is the only signal that a skill is newer than the
    # work it is being judged against.
    authored = skill_files("write") | skill_files("edit")

    out = []
    for r in rows[:MAX_SKILLS + len(authored)]:
        if r["name"].lower() in authored:
            continue
        out.append({"skill": r["name"], "scope": r["scope"],
                    "purpose": (r["description"] or "")[:200],
                    "skill_md_was_read": r["name"].lower() in read})
    # A skill the agent actually opened is the most interesting case, so make
    # sure those survive the cap.
    out.sort(key=lambda c: not c["skill_md_was_read"])
    return out[:MAX_SKILLS]


def audit_session(conn, session_id: str, model: str = MODEL) -> list[dict] | None:
    summary = session_summary(conn, session_id)
    cands = candidates(conn, session_id)
    if not summary or not cands:
        return []
    prompt = (f"{SYSTEM}\n\n## What the session did\n"
              f"{json.dumps(summary, ensure_ascii=False, indent=1)}\n\n"
              f"## Skills available but never invoked ({len(cands)})\n"
              f"{json.dumps(cands, ensure_ascii=False, indent=1)}")
    return _ask(prompt, model) or _ask(prompt, model)   # one retry


def _ask(prompt: str, model: str):
    import os
    import subprocess
    env = {**os.environ, "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
           "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "0"}
    cmd = ["claude", "-p", prompt, "--model", model,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--settings", json.dumps({"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "0"},
                                     "hooks": {}})]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return _extract_json(r.stdout) if r.returncode == 0 else None


VERDICTS = {"should_have_fired", "not_applicable"}


def _store(conn, r: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO skill_audit(session_id, skill_name, verdict,"
        " confidence, reason, was_read, model, audited_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (r["session_id"], r["skill_name"], r["verdict"], r.get("confidence"),
         r.get("reason"), int(bool(r.get("was_read"))), r.get("model"),
         r.get("audited_at")))


def _append(r: dict) -> None:
    config.ensure_dirs()
    with config.SKILL_AUDIT_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(r) + "\n")


def import_cached(conn) -> int:
    path = config.SKILL_AUDIT_FILE
    if not path.exists():
        return 0
    n = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("session_id") and r.get("verdict") in VERDICTS:
                _store(conn, r)
                n += 1
    conn.commit()
    return n


def pending_sessions(conn, limit: int | None = None) -> list[str]:
    """Sessions with real activity that have not been audited yet."""
    rows = db.q(conn, """
        SELECT s.session_id FROM sessions s
         WHERE s.first_seen IS NOT NULL
           AND EXISTS (SELECT 1 FROM tool_calls t WHERE t.session_id=s.session_id)
           AND s.session_id NOT IN (SELECT session_id FROM skill_audit)
         ORDER BY s.first_seen DESC""")
    ids = [r["session_id"] for r in rows]
    return ids[:limit] if limit else ids


def forget(conn, session_ids: list[str]) -> int:
    """Drop prior verdicts for these sessions, from the database and the sidecar.

    Needed when the audit logic changes: a cached verdict produced by older
    logic would otherwise be re-imported on the next analyse and outlive the
    fix.
    """
    if not session_ids:
        return 0
    marks = ",".join("?" for _ in session_ids)
    n = conn.execute(f"DELETE FROM skill_audit WHERE session_id IN ({marks})",
                     session_ids).rowcount
    conn.commit()
    path = config.SKILL_AUDIT_FILE
    if path.exists():
        keep = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                if json.loads(line).get("session_id") in session_ids:
                    continue
            except ValueError:
                pass
            keep.append(line)
        path.write_text("\n".join(keep) + ("\n" if keep else ""))
    return n


def audit(conn, model: str = MODEL, limit: int | None = None,
          progress=None, session_ids: list[str] | None = None) -> dict:
    if session_ids:
        forget(conn, session_ids)
        sessions = session_ids
    else:
        sessions = pending_sessions(conn, limit)
    stats = {"sessions": len(sessions), "judged": 0, "flagged": 0, "failed": 0}
    for i, sid in enumerate(sessions, start=1):
        if progress:
            progress(i, len(sessions), sid)
        read_map = {c["skill"].lower(): c["skill_md_was_read"]
                    for c in candidates(conn, sid)}
        result = audit_session(conn, sid, model)
        if result is None:
            stats["failed"] += 1
            continue
        for item in result:
            if not isinstance(item, dict):
                continue
            name, verdict = item.get("skill"), item.get("verdict")
            if not name or verdict not in VERDICTS:
                continue
            record = {
                "session_id": sid, "skill_name": name, "verdict": verdict,
                "confidence": (item.get("confidence") or "")[:10],
                "reason": (item.get("reason") or "")[:220],
                "was_read": read_map.get(name.lower(), False),
                "model": model, "audited_at": _now(),
            }
            _store(conn, record)
            _append(record)
            stats["judged"] += 1
            if verdict == "should_have_fired":
                stats["flagged"] += 1
        conn.commit()
    return stats


def misses(conn) -> list:
    """Skills ranked by how often they should have fired and did not."""
    return db.q(conn, """
        SELECT a.skill_name,
               COUNT(*)                              AS sessions_missed,
               SUM(a.was_read)                       AS times_read_not_used,
               SUM(a.confidence='high')              AS high_confidence,
               (SELECT COUNT(*) FROM skill_calls k
                 WHERE lower(k.skill_name)=lower(a.skill_name)) AS invocations,
               (SELECT COALESCE(SUM(c.cost_usd),0) FROM api_calls c
                 WHERE c.session_id IN (SELECT session_id FROM skill_audit b
                                         WHERE b.skill_name=a.skill_name
                                           AND b.verdict='should_have_fired')
                   AND c.outcome='ok')              AS cost_in_those_sessions,
               (SELECT a2.reason FROM skill_audit a2
                 WHERE a2.skill_name=a.skill_name AND a2.verdict='should_have_fired'
                 ORDER BY a2.confidence='high' DESC LIMIT 1) AS example_reason
          FROM skill_audit a
         WHERE a.verdict='should_have_fired'
         GROUP BY a.skill_name
         ORDER BY high_confidence DESC, sessions_missed DESC""")


def session_misses(conn, limit: int = 20) -> list:
    return db.q(conn, """
        SELECT a.session_id, p.project_name, COUNT(*) missed,
               GROUP_CONCAT(a.skill_name, ', ') skills,
               (SELECT COALESCE(SUM(c.cost_usd),0) FROM api_calls c
                 WHERE c.session_id=a.session_id AND c.outcome='ok') cost
          FROM skill_audit a
          LEFT JOIN sessions s ON s.session_id=a.session_id
          LEFT JOIN projects p ON p.project_id=s.project_id
         WHERE a.verdict='should_have_fired'
         GROUP BY a.session_id ORDER BY cost DESC LIMIT ?""", (limit,))
