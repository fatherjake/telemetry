"""One line saying what each session was trying to get done.

Every other table in this project is counts, paths and identifiers. None of it
tells you at a glance that `32baff1a` was "find out why declined users see no
reason". That sentence is what makes the rest readable, and it is the name a
session goes by everywhere in this project.

It is written from the prompts. What the agent ended up touching is evidence
of how the session went, not of what it was for - a session that spent an hour
failing to fix a build still had "fix the build" as its goal.
"""
from __future__ import annotations

import datetime as _dt
import json

from . import config, db
from .skillaudit import _ask, session_summary

MODEL = "haiku"
BATCH = 6

# Sessions described per analyse. A backlog is worked through over successive
# runs rather than stalling one of them behind a dozen model calls; newest
# first, so the session you are in is always the one that gets named.
AUTO_BATCH = 12

SYSTEM = """You say what someone was trying to get done in a working session.

For each session you get the prompts they typed, in order, and — as
supporting evidence only — the files, tools and commits that resulted.

Write a single sentence, at most 20 words, saying what the session was for.
Plain past tense, concrete, no filler. Lead with the goal, not the activity.

Good:  "Find out why declined users see no reason on the home screen."
Good:  "Re-cut the CreditLine social video until the hero device tilt looked right."
Bad:   "Worked on various tasks." (says nothing)
Bad:   "A session involving multiple tool calls." (describes telemetry, not work)

The first prompt usually states the goal and later ones refine it. If the
session changed direction, describe where it ended up. If it genuinely did
several unrelated things, lead with the largest and say "and other work".

Name the actual subject — the service, feature or artefact.

Output ONLY a JSON array, no prose or code fences:
[{"id":"...","description":"..."}]"""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def naming_note(session: dict) -> str:
    """Why a session has no name yet, in one clause."""
    if not session.get("prompt_turns"):
        return "no prompts recorded, so there is nothing to name it from"
    if not config.AUTO_DESCRIBE:
        return "run ./telemetry enrich --what describe to name it from its prompts"
    return "named on the next analyse"


def pending(conn, limit: int | None = None) -> list[tuple[str, str]]:
    """(kind, id) pairs worth describing, newest session first.

    A session is named as soon as it has said anything, from whatever prompts
    exist at that moment - a best guess on a session in progress is worth more
    than an empty row - and named **again** whenever it has done more since.
    The sentence follows the session rather than freezing at whatever it was
    doing in its first ten minutes.

    A session with no prompt text cannot be described from its prompts, so it
    is left alone rather than guessed at from tool traffic.
    """
    out = []
    for r in db.q(conn, """
        SELECT s.session_id id,
               n.turns described_turns,
               (SELECT COUNT(*) FROM turns t
                 WHERE t.session_id = s.session_id
                   AND t.prompt_text IS NOT NULL
                   AND COALESCE(t.is_system,0)=0) prompt_turns
          FROM sessions s
          LEFT JOIN narrative n
                 ON n.kind='session' AND n.subject_id = s.session_id
         WHERE s.first_seen IS NOT NULL
         ORDER BY s.last_seen DESC, s.first_seen DESC"""):
        # A description written before turn counts were recorded compares as
        # zero, so those get one refresh and then follow the same rule.
        if r["prompt_turns"] > (r["described_turns"] or 0):
            out.append(("session", r["id"]))
    return out[:limit] if limit else out


def _payload(conn, kind: str, subject_id: str) -> dict | None:
    """What the session was asked to do, with the rest as supporting evidence.

    Prompts lead and are given in full order; files and commits follow, and
    are there to name the subject the prompts refer to obliquely ("fix it",
    "try again") rather than to describe the work themselves.
    """
    s = session_summary(conn, subject_id)
    if not s or not s.get("prompts"):
        return None
    return {"id": subject_id, "project": s.get("project"),
            "prompts_in_order": s["prompts"],
            "evidence": {k: s[k] for k in ("files_touched", "commits", "tools")
                         if s.get(k)}}


def generate(conn, model: str = MODEL, limit: int | None = None,
             progress=None, redo: bool = False) -> dict:
    if redo:
        conn.execute("DELETE FROM narrative")
        conn.commit()
        if config.NARRATIVE_FILE.exists():
            config.NARRATIVE_FILE.unlink()
    todo = pending(conn, limit)
    stats = {"pending": len(todo), "written": 0, "failed": 0}
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        if progress:
            progress(start + len(chunk), len(todo))
        payloads = [p for p in (_payload(conn, k, i) for k, i in chunk) if p]
        if not payloads:
            continue
        prompt = (f"{SYSTEM}\n\nDescribe these {len(payloads)} sessions:\n\n"
                  f"{json.dumps(payloads, ensure_ascii=False, indent=1)}")
        result = _ask(prompt, model) or _ask(prompt, model)
        if not result:
            stats["failed"] += len(payloads)
            continue
        by_id = {r.get("id"): r for r in result if isinstance(r, dict)}
        for kind, sid in chunk:
            item = by_id.get(sid)
            desc = (item or {}).get("description")
            if not desc:
                stats["failed"] += 1
                continue
            record = {"kind": kind, "subject_id": sid,
                      "description": desc.strip()[:240], "model": model,
                      "generated_at": _now(),
                      # How much of the session this sentence was written
                      # from, so a session that continues afterwards can be
                      # recognised as needing a new one.
                      "turns": _prompt_turns(conn, sid)}
            _store(conn, record)
            _append(record)
            stats["written"] += 1
        conn.commit()
    return stats


def _prompt_turns(conn, session_id: str) -> int:
    row = conn.execute("""
        SELECT COUNT(*) n FROM turns
         WHERE session_id=? AND prompt_text IS NOT NULL
           AND COALESCE(is_system,0)=0""", (session_id,)).fetchone()
    return row["n"] if row else 0


def auto_generate(conn, model: str = MODEL, limit: int = AUTO_BATCH) -> dict:
    """Name sessions as part of `analyse`.

    This is the only step of the pipeline that talks to anything off this
    machine, and it is here rather than behind its own command because a name
    is only useful if it is already there when you open the database. It is
    switched off with `telemetry config privacy --auto-describe off`.
    """
    if not pending(conn, limit=1):
        return {"pending": 0, "written": 0, "failed": 0}
    return generate(conn, model=model, limit=limit)


def _store(conn, r: dict) -> None:
    conn.execute("""INSERT OR REPLACE INTO narrative(kind, subject_id,
                        description, model, generated_at, turns)
                    VALUES (?,?,?,?,?,?)""",
                 (r["kind"], r["subject_id"], r["description"], r.get("model"),
                  r.get("generated_at"), r.get("turns")))


def _append(r: dict) -> None:
    config.ensure_dirs()
    with config.NARRATIVE_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(r) + "\n")


def import_cached(conn) -> int:
    path = config.NARRATIVE_FILE
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
        # Descriptions of work streams are in this file from before the
        # session became the unit. They describe something that no longer
        # exists, so they are read past rather than re-imported.
        if (r.get("kind") == "session" and r.get("subject_id")
                and r.get("description")):
            _store(conn, r)
            n += 1
    conn.commit()
    return n


def get(conn, kind: str, subject_id: str) -> str | None:
    row = conn.execute("SELECT description FROM narrative WHERE kind=? AND subject_id=?",
                       (kind, subject_id)).fetchone()
    return row["description"] if row else None
