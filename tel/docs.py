"""Knowledge-base coverage: which documents are cold, and which of those matter.

"150 files, 4 read" is a statistic, not a finding. A contacts directory going
unread by a coding agent is correct behaviour; a service specification going
unread while that service is being worked on is a real gap. The difference is
who the document is for, so every document is profiled once for audience and
agent relevance, and coldness is only reported against documents that were
meant to be consulted.

Profiling sends a path, a title and a short excerpt to the model. Knowledge
bases hold business-sensitive material, so the excerpt is short by default and
`--titles-only` sends no content at all.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path

from . import config, db
from .skillaudit import _ask

MODEL = "haiku"
BATCH = 12
EXCERPT_CHARS = 240
AUDIENCES = {"agent", "human", "both"}
RELEVANCE = {"high", "medium", "low"}

SYSTEM = """You profile documents in a software team's knowledge base.

For each document decide who it is for and how useful it would be to an AI
coding agent working in this repository.

- audience: "agent" (technical reference an agent would consult while working —
  service specs, integration notes, architecture, conventions, runbooks),
  "human" (for people, not for doing engineering work — contacts, meeting
  notes, brand and marketing copy, personal planning, CRM records), or "both".
- agent_relevance: "high" if an agent working in this codebase would likely
  need it; "medium" if occasionally; "low" if essentially never.
- topic: one short line describing what it covers. Be concrete and factual.

A document being unread is NOT evidence about its audience. Judge only from
the path, title and excerpt.

Output ONLY a JSON array, no prose or code fences:
[{"path":"...","audience":"agent","agent_relevance":"high","topic":"..."}]"""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _title_and_excerpt(path: Path) -> tuple[str | None, str, int]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, "", 0
    words = len(text.split())
    title = None
    body_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not title and stripped.startswith("#"):
            title = stripped.lstrip("#").strip()[:160]
            continue
        if stripped and not stripped.startswith(("---", "|", "```")):
            body_lines.append(stripped)
        if len(" ".join(body_lines)) > EXCERPT_CHARS * 2:
            break
    return title, " ".join(body_lines)[:EXCERPT_CHARS], words


def scan(conn, root: str, pattern: str = "*.md",
         exclude: list[str] | None = None) -> int:
    """Record every document under `root`. Local only; sends nothing."""
    base = Path(root).expanduser()
    if not base.exists():
        return 0
    exclude = exclude or []
    n = 0
    for f in sorted(base.rglob(pattern)):
        if not f.is_file():
            continue
        rel = str(f.relative_to(base))
        if any(f.match(pat) or re.search(pat, rel) for pat in exclude):
            continue
        title, excerpt, words = _title_and_excerpt(f)
        st = f.stat()
        conn.execute("""
            INSERT INTO doc_inventory(path, root, rel_path, title, size_bytes,
                                      word_count, modified_at, excerpt)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
                title=excluded.title, size_bytes=excluded.size_bytes,
                word_count=excluded.word_count, modified_at=excluded.modified_at,
                excerpt=excluded.excerpt""",
            (str(f), str(base), rel, title, st.st_size, words,
             _dt.datetime.fromtimestamp(st.st_mtime, tz=_dt.timezone.utc)
                 .isoformat(timespec="seconds").replace("+00:00", "Z"),
             excerpt))
        n += 1
    conn.commit()
    return n


def unprofiled(conn, root: str | None = None, limit: int | None = None) -> list[dict]:
    where = "WHERE audience IS NULL"
    params: tuple = ()
    if root:
        where += " AND root = ?"
        params = (str(Path(root).expanduser()),)
    rows = db.q(conn, f"SELECT path, rel_path, title, excerpt, word_count"
                      f" FROM doc_inventory {where} ORDER BY rel_path", params)
    out = [dict(r) for r in rows]
    return out[:limit] if limit else out


def profile(conn, root: str | None = None, model: str = MODEL,
            limit: int | None = None, titles_only: bool = False,
            progress=None) -> dict:
    todo = unprofiled(conn, root, limit)
    stats = {"pending": len(todo), "profiled": 0, "failed": 0}
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        if progress:
            progress(start + len(chunk), len(todo))
        payload = [{"path": d["rel_path"], "title": d["title"],
                    **({} if titles_only else {"excerpt": d["excerpt"]}),
                    "words": d["word_count"]} for d in chunk]
        prompt = (f"{SYSTEM}\n\nProfile these {len(payload)} documents:\n\n"
                  f"{json.dumps(payload, ensure_ascii=False, indent=1)}")
        result = _ask(prompt, model) or _ask(prompt, model)
        if not result:
            stats["failed"] += len(chunk)
            continue
        by_rel = {r.get("path"): r for r in result if isinstance(r, dict)}
        for d in chunk:
            item = by_rel.get(d["rel_path"])
            if not item:
                stats["failed"] += 1
                continue
            aud = item.get("audience")
            rel = item.get("agent_relevance")
            if aud not in AUDIENCES or rel not in RELEVANCE:
                stats["failed"] += 1
                continue
            record = {"path": d["path"], "audience": aud, "agent_relevance": rel,
                      "topic": (item.get("topic") or "")[:220],
                      "model": model, "profiled_at": _now()}
            _store(conn, record)
            _append(record)
            stats["profiled"] += 1
        conn.commit()
    return stats


def _store(conn, r: dict) -> None:
    conn.execute("""UPDATE doc_inventory SET audience=?, agent_relevance=?,
                           topic=?, model=?, profiled_at=? WHERE path=?""",
                 (r["audience"], r["agent_relevance"], r.get("topic"),
                  r.get("model"), r.get("profiled_at"), r["path"]))


def _append(r: dict) -> None:
    config.ensure_dirs()
    with config.DOC_PROFILE_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(r) + "\n")


def import_cached(conn) -> int:
    path = config.DOC_PROFILE_FILE
    if not path.exists():
        return 0
    n = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("path") and r.get("audience") in AUDIENCES:
            _store(conn, r)
            n += 1
    conn.commit()
    return n


def coverage(conn, root: str | None = None) -> list:
    """Every document with its profile and its observed read activity."""
    where, params = "", []
    if root:
        where = "WHERE d.root = ?"
        params = [str(Path(root).expanduser())]
    return db.q(conn, f"""
        SELECT d.rel_path, d.title, d.topic, d.audience, d.agent_relevance,
               d.word_count, d.modified_at,
               COALESCE(r.reads, 0) AS reads,
               r.last_read, COALESCE(r.sessions, 0) AS sessions
          FROM doc_inventory d
          LEFT JOIN (
              SELECT path, COUNT(*) reads, MAX(ts) last_read,
                     COUNT(DISTINCT session_id) sessions
                FROM file_activity WHERE operation='read' GROUP BY path
          ) r ON r.path = d.path
          {where}
         ORDER BY (d.agent_relevance='high') DESC, reads ASC, d.rel_path""",
        tuple(params))


def cold_spots(conn, root: str | None = None) -> list:
    """Documents an agent would plausibly need that nothing has ever opened."""
    return [r for r in coverage(conn, root)
            if r["reads"] == 0 and r["audience"] in ("agent", "both")
            and r["agent_relevance"] in ("high", "medium")]


def summary(conn, root: str | None = None) -> dict:
    rows = coverage(conn, root)
    agentish = [r for r in rows if r["audience"] in ("agent", "both")]
    return {
        "total": len(rows),
        "profiled": len([r for r in rows if r["audience"]]),
        "agent_facing": len(agentish),
        "human_only": len([r for r in rows if r["audience"] == "human"]),
        "read": len([r for r in rows if r["reads"]]),
        "agent_facing_read": len([r for r in agentish if r["reads"]]),
        "cold_spots": len(cold_spots(conn, root)),
    }


def cold_against_work(conn, root: str | None = None) -> list:
    """Cold documents describing something the database saw being worked on.

    Joined on *file activity* rather than on commits: the most expensive work
    in this dataset produced no commit at all, so a commit-based join would
    miss exactly the sessions worth flagging. The subject is taken from the
    document's own name (`Services/atlas-api.md` -> `atlas-api`) and matched
    against the paths that were actually touched, so a match is evidence rather than a
    guess.
    """
    out = []
    for doc in cold_spots(conn, root):
        stem = Path(doc["rel_path"]).stem
        subject = re.sub(r"^(uv[- ]?)", "uv-", stem.lower()).replace(" ", "-")
        if len(subject) < 5:
            continue
        row = conn.execute("""
            SELECT COUNT(DISTINCT f.session_id) sessions,
                   COUNT(*) touches,
                   COALESCE((SELECT SUM(a.cost_usd) FROM api_calls a
                              WHERE a.outcome='ok' AND a.session_id IN (
                                  SELECT DISTINCT session_id FROM file_activity
                                   WHERE lower(path) LIKE '%/' || ? || '/%')), 0) cost
              FROM file_activity f
             WHERE lower(f.path) LIKE '%/' || ? || '/%'""",
            (subject, subject)).fetchone()
        if row and row["touches"]:
            out.append({**dict(doc), "subject": subject,
                        "sessions": row["sessions"], "touches": row["touches"],
                        "work_cost": row["cost"]})
    out.sort(key=lambda d: (-(d["work_cost"] or 0), -(d["touches"] or 0)))
    return out


# --- per-session document gaps ----------------------------------------------

GAP_SYSTEM = """You judge whether a document would have helped a piece of work.

You get what one session actually did — what was asked for, the files it
changed, its commits and the tools it used — and a list of documents from the
team's knowledge base that were NOT opened during that session.

For each document decide:
- "should_have_read": the work would plausibly have gone better with it. The
  document covers the service, schema, contract, flow or convention being
  changed. Be strict: topical adjacency is not enough, it must bear on what was
  actually done.
- "not_relevant": it does not bear on this work.

Judge from the document's stated topic. Do not assume content the topic does
not claim.

Output ONLY a JSON array, no prose or code fences:
[{"path":"...","verdict":"should_have_read","confidence":"high","reason":"under 15 words"}]
Include an entry for every document you were given."""

GAP_VERDICTS = {"should_have_read", "not_relevant"}
GAP_CANDIDATES = 22

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set:
    return {t for t in _TOKEN.findall((text or "").lower()) if len(t) > 2}


def session_work(conn, session_id: str) -> dict:
    """What one session did, in the shape the judge needs.

    Nothing is shared or apportioned here: one session, its own prompts, its
    own files, its own commits.
    """
    row = conn.execute("""
        SELECT s.session_id, p.project_name FROM sessions s
          LEFT JOIN projects p ON p.project_id = s.project_id
         WHERE s.session_id=?""", (session_id,)).fetchone()
    if not row:
        return {}
    subjects = [r["subject"] for r in conn.execute(
        "SELECT subject FROM git_activity WHERE session_id=? LIMIT 12",
        (session_id,))]
    files = [r["p"] for r in conn.execute("""
        SELECT COALESCE(repo_relative_path, path) p, COUNT(*) c
          FROM file_activity WHERE session_id=?
         GROUP BY 1 ORDER BY c DESC LIMIT 25""", (session_id,))]
    prompts = [r["t"] for r in conn.execute("""
        SELECT substr(prompt_text,1,180) t FROM turns
         WHERE session_id=? AND prompt_text IS NOT NULL
           AND COALESCE(is_system,0)=0 ORDER BY seq LIMIT 8""", (session_id,))]
    tools = [r["n"] for r in conn.execute("""
        SELECT DISTINCT COALESCE(mcp_server_name, tool_name) n
          FROM tool_calls WHERE session_id=? LIMIT 12""", (session_id,))]
    lines = conn.execute("""
        SELECT COALESCE(SUM(insertions),0) i, COALESCE(SUM(deletions),0) d
          FROM git_activity WHERE session_id=?""", (session_id,)).fetchone()
    out = {"project": row["project_name"], "asked_for": prompts,
           "files_touched": files, "commits": subjects,
           "tools_used": tools,
           "lines_changed": f'+{lines["i"]}/-{lines["d"]}'}
    from .narrate import get as _narr
    desc = _narr(conn, "session", session_id)
    if desc:
        out["goal"] = desc
    return out


def gap_candidates(conn, session_id: str, limit: int = GAP_CANDIDATES) -> list[dict]:
    """Agent-facing documents not opened during this session, ranked by overlap.

    Pre-filtered locally so a judgement is only paid for on plausible
    candidates; the ranking is token overlap between the document and what the
    session actually touched.
    """
    summary = session_work(conn, session_id)
    if not summary:
        return []
    read_here = {r["path"] for r in conn.execute(
        """SELECT DISTINCT path FROM file_activity
            WHERE operation='read' AND session_id=?""", (session_id,))}

    context = _tokens(" ".join(
        [summary.get("goal") or ""] + summary["commits"]
        + summary["files_touched"] + summary["asked_for"]))
    scored = []
    for d in db.q(conn, """SELECT path, rel_path, topic, agent_relevance, word_count
                             FROM doc_inventory
                            WHERE audience IN ('agent','both')
                              AND agent_relevance IN ('high','medium')"""):
        if d["path"] in read_here:
            continue
        overlap = len(context & _tokens(f'{d["rel_path"]} {d["topic"] or ""}'))
        if overlap:
            scored.append((overlap, dict(d)))
    scored.sort(key=lambda x: (-x[0], -(x[1]["word_count"] or 0)))
    return [d for _, d in scored[:limit]]


def judge_gaps(conn, model: str = MODEL, limit: int | None = None,
               progress=None, redo: bool = False) -> dict:
    if redo:
        conn.execute("DELETE FROM doc_gap")
        conn.commit()
        if config.DOC_GAP_FILE.exists():
            config.DOC_GAP_FILE.unlink()
    sessions = [r["session_id"] for r in db.q(conn, """
        SELECT s.session_id FROM sessions s
         WHERE s.first_seen IS NOT NULL
           AND EXISTS (SELECT 1 FROM tool_calls t WHERE t.session_id=s.session_id)
           AND s.session_id NOT IN (SELECT session_id FROM doc_gap)
         ORDER BY s.first_seen DESC""")]
    if limit:
        sessions = sessions[:limit]
    stats = {"sessions": len(sessions), "judged": 0, "gaps": 0, "failed": 0}
    for i, sid in enumerate(sessions, start=1):
        if progress:
            progress(i, len(sessions), sid)
        cands = gap_candidates(conn, sid)
        summary = session_work(conn, sid)
        if not cands or not summary:
            continue
        payload = [{"path": c["rel_path"], "topic": c["topic"],
                    "relevance": c["agent_relevance"], "words": c["word_count"]}
                   for c in cands]
        prompt = (f"{GAP_SYSTEM}\n\n## What the session did\n"
                  f"{json.dumps(summary, ensure_ascii=False, indent=1)}\n\n"
                  f"## Documents not opened during it ({len(payload)})\n"
                  f"{json.dumps(payload, ensure_ascii=False, indent=1)}")
        result = _ask(prompt, model) or _ask(prompt, model)
        if not result:
            stats["failed"] += 1
            continue
        by_rel = {r.get("path"): r for r in result if isinstance(r, dict)}
        for c in cands:
            item = by_rel.get(c["rel_path"])
            if not item or item.get("verdict") not in GAP_VERDICTS:
                continue
            record = {"session_id": sid, "doc_path": c["path"],
                      "verdict": item["verdict"],
                      "confidence": (item.get("confidence") or "")[:10],
                      "reason": (item.get("reason") or "")[:200],
                      "model": model, "judged_at": _now()}
            _store_gap(conn, record)
            _append_gap(record)
            stats["judged"] += 1
            if record["verdict"] == "should_have_read":
                stats["gaps"] += 1
        conn.commit()
    return stats


def _store_gap(conn, r: dict) -> None:
    conn.execute("""INSERT OR REPLACE INTO doc_gap(session_id, doc_path,
                        verdict, confidence, reason, model, judged_at)
                    VALUES (?,?,?,?,?,?,?)""",
                 (r["session_id"], r["doc_path"], r["verdict"],
                  r.get("confidence"), r.get("reason"), r.get("model"),
                  r.get("judged_at")))


def _append_gap(r: dict) -> None:
    config.ensure_dirs()
    with config.DOC_GAP_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(r) + "\n")


def _sessions_for_legacy_stream(conn, work_stream_id: str) -> list[str]:
    """Sessions behind a pre-session-model work-stream id.

    Judgements written before the session became the unit are keyed
    `project_id|scope` or `session:<id>`. Commits still carry their project,
    scope and session, so the mapping is recoverable without the tables that
    are gone.
    """
    if work_stream_id.startswith("session:"):
        return [work_stream_id.split(":", 1)[1]]
    project_id, _, key = work_stream_id.rpartition("|")
    if not project_id:
        return []
    return [r["session_id"] for r in db.q(conn, """
        SELECT DISTINCT session_id FROM git_activity
         WHERE project_id = ?
           AND COALESCE(commit_scope, commit_type, 'unscoped') = ?
           AND session_id IS NOT NULL""", (project_id, key))]


def import_gaps(conn) -> int:
    path = config.DOC_GAP_FILE
    if not path.exists():
        return 0
    n = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("verdict") not in GAP_VERDICTS:
            continue
        if r.get("session_id"):
            _store_gap(conn, r)
            n += 1
        elif r.get("work_stream_id"):
            # A stream that spanned two sessions lands on both: the document
            # bore on that work, and both sessions did it.
            for sid in _sessions_for_legacy_stream(conn, r["work_stream_id"]):
                _store_gap(conn, dict(r, session_id=sid))
                n += 1
    conn.commit()
    return n


def gaps_for_session(conn, session_id: str) -> list[dict]:
    return [dict(r) for r in db.q(conn, """
        SELECT g.doc_path, d.rel_path, d.topic, d.word_count, d.agent_relevance,
               g.confidence, g.reason
          FROM doc_gap g JOIN doc_inventory d ON d.path = g.doc_path
         WHERE g.session_id = ? AND g.verdict = 'should_have_read'
         ORDER BY g.confidence='high' DESC, d.word_count DESC""",
        (session_id,))]


def gap_summary(conn) -> list:
    return db.q(conn, """
        SELECT d.rel_path, d.word_count, d.agent_relevance,
               COUNT(*) sessions,
               SUM(g.confidence='high') high,
               MAX(g.reason) example
          FROM doc_gap g
          JOIN doc_inventory d ON d.path = g.doc_path
         WHERE g.verdict = 'should_have_read'
         GROUP BY d.path ORDER BY high DESC, sessions DESC, d.word_count DESC""")


def docs_read_by_sessions(conn, session_ids: list[str]) -> list[dict]:
    """Knowledge-base documents opened during these sessions."""
    if not session_ids:
        return []
    marks = ",".join("?" for _ in session_ids)
    return [dict(r) for r in db.q(conn, f"""
        SELECT d.rel_path, d.topic, d.word_count, d.audience, d.agent_relevance,
               COUNT(*) reads, MAX(f.ts) last_read
          FROM file_activity f
          JOIN doc_inventory d ON d.path = f.path
         WHERE f.operation='read' AND f.session_id IN ({marks})
         GROUP BY d.path ORDER BY reads DESC""", tuple(session_ids))]
