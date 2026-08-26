"""Why did a correction happen, and what would have prevented it?

Counting corrections tells you how often the agent missed. It does not tell
you what to do about it. This asks the more useful question: what did the
agent not know, and where should that have been written down?

The taxonomy is deliberately action-shaped. Every value maps to a fix, or
honestly to "no fix exists" — design iteration has no right answer knowable in
advance, and pretending otherwise would turn taste into a defect.
"""
from __future__ import annotations

import datetime as _dt
import json

from . import config, db
from .classify import _extract_json
from .skillaudit import _ask

MODEL = "haiku"

CAUSES = {"missing_context", "stale_context", "ambiguous_request",
          "wrong_approach", "design_iteration", "tool_failure"}
LOCATIONS = {"claude_md", "memory", "docs", "skill", "prompt", "none"}

SYSTEM = """You diagnose why a developer had to correct an AI coding agent.

You get the correcting message, the instruction that preceded it, and what the
agent actually did in between. Choose exactly one cause:

- "missing_context": the agent lacked a project fact it could not infer — which
  app or service is meant, a naming convention, where something lives, how this
  codebase does a thing. THE MOST ACTIONABLE: it means something should be
  written down.
- "stale_context": the agent used information that existed but was out of date
  or wrong.
- Terminology counts as context. If the correction reveals that the developer
  uses a name differently from the codebase — calling one repo or service by a
  name that belongs to another — that is "missing_context", and the fix is a
  glossary line. Do not call this ambiguity: the developer was being consistent
  with their own vocabulary, which simply was not written down.
- "ambiguous_request": the instruction genuinely permitted the reading the agent
  took, AND no project fact would have resolved it. Apply the test first: if a
  sentence could be written down that would stop this recurring — which of two
  similarly named things is meant, what a term refers to here — then it is
  "missing_context", not ambiguity. Ambiguity is the residue left when no fact
  would have helped.
- "wrong_approach": the agent had what it needed and chose a poor method.
- "design_iteration": subjective refinement of creative work — visual tweaks,
  wording, layout. There is no right answer knowable in advance. NOT a defect.
- "tool_failure": something broke technically; not a knowledge problem.

Then, only when a fix would actually help:
- what_was_missing: the specific fact, in one line, concretely. Not "more
  context about the app" but "atlas-app is the Checkout app; atlas-sandbox
  is the harness".
- suggested_fix: the sentence that should be written down, phrased so it could
  be pasted in as-is.
- fix_location: claude_md | memory | docs | skill | prompt | none

Rules that must hold:
- design_iteration and tool_failure: fix_location "none", and leave
  what_was_missing and suggested_fix empty. Do not invent a fix where none
  would have helped.
- Any other cause: if you supply a suggested_fix you must give a real
  fix_location. "none" with a fix written next to it is contradictory — either
  the fix is worth recording somewhere, or there is no fix.
- wrong_approach usually belongs in a skill or in claude_md as a working rule.

Output ONLY a JSON array, no prose, no code fences:
[{"turn_id":"...","cause":"...","confidence":"high|medium|low",
  "what_was_missing":"...","suggested_fix":"...","fix_location":"..."}]"""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def context_for(conn, turn_id: str) -> dict | None:
    """The correction, what preceded it, and what the agent did in between."""
    t = conn.execute("""
        SELECT t.turn_id, t.session_id, t.seq, t.prompt_text, t.cost_usd,
               t.gap_before_s, p.project_name
          FROM turns t
          LEFT JOIN sessions s ON s.session_id = t.session_id
          LEFT JOIN projects p ON p.project_id = s.project_id
         WHERE t.turn_id = ?""", (turn_id,)).fetchone()
    if not t or not t["prompt_text"]:
        return None

    prev = conn.execute("""
        SELECT turn_id, prompt_text FROM turns
         WHERE session_id = ? AND seq = ? - 1""",
        (t["session_id"], t["seq"])).fetchone()

    def did(tid):
        if not tid:
            return {}
        tools = [dict(r) for r in conn.execute("""
            SELECT tool_name n, COUNT(*) c FROM tool_calls
             WHERE prompt_id=? GROUP BY 1 ORDER BY c DESC LIMIT 8""", (tid,))]
        files = [r["n"] for r in conn.execute("""
            SELECT DISTINCT COALESCE(repo_relative_path, path) n
              FROM file_activity f JOIN tool_calls c ON c.tool_use_id=f.tool_use_id
             WHERE c.prompt_id=? LIMIT 10""", (tid,))]
        cmds = [r["n"] for r in conn.execute("""
            SELECT substr(b.command,1,110) n FROM bash_activity b
              JOIN tool_calls c ON c.tool_use_id=b.tool_use_id
             WHERE c.prompt_id=? LIMIT 6""", (tid,))]
        return {"tools": {x["n"]: x["c"] for x in tools if x["n"]},
                "files": files, "commands": cmds}

    return {
        "turn_id": t["turn_id"],
        "project": t["project_name"],
        "correction_message": (t["prompt_text"] or "")[:400],
        "cost_usd_of_this_turn": round(t["cost_usd"] or 0, 2),
        "seconds_since_previous_turn": int(t["gap_before_s"] or 0),
        "instruction_that_preceded_it": ((prev["prompt_text"] or "")[:400]
                                         if prev else None),
        "what_the_agent_did_in_between": did(prev["turn_id"] if prev else None),
    }


def pending(conn, limit: int | None = None) -> list[str]:
    rows = db.q(conn, """
        SELECT t.turn_id FROM turns t
         WHERE t.is_correction = 1 AND COALESCE(t.is_system,0) = 0
           AND t.prompt_text IS NOT NULL
           AND t.turn_id NOT IN (SELECT turn_id FROM correction_cause)
         ORDER BY t.started_at DESC""")
    ids = [r["turn_id"] for r in rows]
    return ids[:limit] if limit else ids


def _store(conn, r: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO correction_cause(turn_id, session_id, cause,"
        " confidence, what_was_missing, suggested_fix, fix_location, model,"
        " diagnosed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (r["turn_id"], r.get("session_id"), r["cause"], r.get("confidence"),
         r.get("what_was_missing"), r.get("suggested_fix"),
         r.get("fix_location"), r.get("model"), r.get("diagnosed_at")))


def _append(r: dict) -> None:
    config.ensure_dirs()
    with config.CORRECTION_CAUSE_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(r) + "\n")


def import_cached(conn) -> int:
    path = config.CORRECTION_CAUSE_FILE
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
        if r.get("turn_id") and r.get("cause") in CAUSES:
            _store(conn, r)
            n += 1
    conn.commit()
    return n


def forget(conn, turn_ids: list[str] | None = None) -> int:
    """Drop diagnoses from the database and the sidecar, so a logic change is
    not outlived by verdicts produced under the old rules."""
    path = config.CORRECTION_CAUSE_FILE
    if turn_ids is None:
        n = conn.execute("DELETE FROM correction_cause").rowcount
        conn.commit()
        if path.exists():
            path.unlink()
        return n
    marks = ",".join("?" for _ in turn_ids)
    n = conn.execute(f"DELETE FROM correction_cause WHERE turn_id IN ({marks})",
                     turn_ids).rowcount
    conn.commit()
    if path.exists():
        keep = [l for l in path.read_text().splitlines()
                if l.strip() and json.loads(l).get("turn_id") not in turn_ids]
        path.write_text("\n".join(keep) + ("\n" if keep else ""))
    return n


def diagnose(conn, model: str = MODEL, limit: int | None = None,
             batch: int = 5, progress=None, redo: bool = False) -> dict:
    if redo:
        forget(conn)
    todo = pending(conn, limit)
    stats = {"pending": len(todo), "diagnosed": 0, "failed": 0}
    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        if progress:
            progress(start + len(chunk), len(todo))
        contexts = [c for c in (context_for(conn, t) for t in chunk) if c]
        if not contexts:
            continue
        prompt = (f"{SYSTEM}\n\nDiagnose these {len(contexts)} corrections:\n\n"
                  f"{json.dumps(contexts, ensure_ascii=False, indent=1)}")
        result = _ask(prompt, model) or _ask(prompt, model)
        if not result:
            stats["failed"] += len(contexts)
            continue
        by_id = {r.get("turn_id"): r for r in result if isinstance(r, dict)}
        for c in contexts:
            item = by_id.get(c["turn_id"])
            if not item or item.get("cause") not in CAUSES:
                stats["failed"] += 1
                continue
            loc = item.get("fix_location")
            fix = (item.get("suggested_fix") or "").strip()
            cause = item["cause"]
            if cause in ("design_iteration", "tool_failure"):
                loc, fix = "none", ""       # no fix exists for these
            elif fix and loc not in LOCATIONS - {"none"}:
                # A fix with nowhere to put it is not actionable; default it to
                # the project's working notes rather than dropping it.
                loc = "claude_md"
            record = {
                "turn_id": c["turn_id"],
                "session_id": db.scalar(conn, "SELECT session_id FROM turns"
                                              " WHERE turn_id=?", (c["turn_id"],),
                                        default=None),
                "cause": cause,
                "confidence": (item.get("confidence") or "")[:10],
                "what_was_missing": ((item.get("what_was_missing") or "")[:300]
                                     if fix else None) or None,
                "suggested_fix": fix[:400] or None,
                "fix_location": loc if loc in LOCATIONS else "none",
                "model": model, "diagnosed_at": _now(),
            }
            _store(conn, record)
            _append(record)
            stats["diagnosed"] += 1
        conn.commit()
    return stats


def by_cause(conn) -> list:
    return db.q(conn, """
        SELECT c.cause, COUNT(*) n,
               ROUND(SUM(COALESCE(t.cost_usd,0)),2) cost,
               ROUND(SUM(COALESCE(t.duration_s,0))/60.0,1) minutes
          FROM correction_cause c JOIN turns t ON t.turn_id=c.turn_id
         GROUP BY 1 ORDER BY cost DESC""")


def proposed_knowledge(conn) -> list:
    """Fixes worth making, ranked by the cost of the corrections they address."""
    return db.q(conn, """
        SELECT c.fix_location, c.what_was_missing, c.suggested_fix, c.confidence,
               COUNT(*) corrections,
               ROUND(SUM(COALESCE(t.cost_usd,0)),2) cost,
               GROUP_CONCAT(DISTINCT p.project_name) projects
          FROM correction_cause c
          JOIN turns t ON t.turn_id = c.turn_id
          LEFT JOIN sessions s ON s.session_id = c.session_id
          LEFT JOIN projects p ON p.project_id = s.project_id
         WHERE c.fix_location <> 'none' AND c.suggested_fix IS NOT NULL
         GROUP BY c.suggested_fix ORDER BY cost DESC""")


def for_turn(conn, turn_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM correction_cause WHERE turn_id=?",
                     (turn_id,)).fetchone()
    return dict(r) if r else None
