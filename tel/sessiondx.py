"""What went wrong in a session, and what to change so it goes better.

The other model-assisted analyses each answer one narrow question: why did
*this* correction happen, which skill should *this* session have used, which
doc should *this* session have read. None of them look at the shape of the
work itself - that a file was rewritten eleven times, that the same command
ran nine times, that one turn ate a third of the money.

Those are the cheapest signals in the whole database and none of them need a
model to find. So the split here is deliberate:

  1. `signals()` computes friction mechanically. Free, deterministic,
     reproducible, and useful on its own - the mechanical signals alone
     prints it without spending anything.
  2. `diagnose()` hands only those signals to a model and asks what they mean
     and what to change. The model interprets evidence; it never goes hunting
     for it. That keeps the prompt small, the cost flat per session, and the
     output anchored to numbers you can check.

A signal is not a defect. Rewriting a file eleven times is normal when
iterating on a video frame and pathological when editing a config, and only
the second one has a fix. Deciding which is what the model is for.
"""
from __future__ import annotations

import datetime as _dt
import json

from . import config, db
from .skillaudit import _ask

MODEL = "haiku"

FINDING_KINDS = {"rework_loop", "context_loss", "missing_automation",
                 "brittle_tooling", "unclear_target", "expensive_detour",
                 "working_as_intended"}
LOCATIONS = {"claude_md", "memory", "docs", "skill", "hook", "prompt",
             "tooling", "none"}

# Thresholds are set where a pattern stops being ordinary work. They are
# deliberately generous: a false negative costs nothing, a false positive
# costs a model call and the reader's trust.
MIN_WRITES = 4          # rewrites of one file before it reads as a loop
MIN_SWITCHES = 4        # read->write->read flips on one path
MAX_ALTERNATION_ROWS = 4
MIN_REREADS = 6         # reads of a file never written in this session
MIN_REPEATS = 4         # identical shell commands
MIN_FAILURES = 3        # failures of one program, or one tool error type
MIN_SEARCHES = 10       # search calls inside a single turn
COST_SHARE = 0.30       # share of session cost that makes one turn a spike
COST_FLOOR = 1.00       # ...and the dollars below which it is not worth saying

SYSTEM = """You review the mechanical friction signals from one session of a
developer working with an AI coding agent, and say what is worth changing.

You are given measured facts, not guesses: how many times each file was
written, which commands repeated, which tools failed, where the money went.
You are NOT given the code. Do not speculate about content you cannot see.

The hard part is telling friction from normal work. Apply this test to every
signal: would a reasonable engineer, told this number, change something? A
video frame refined eleven times is iteration and there is no fix. The same
config file rewritten eleven times is a loop and there is. A README both read
and written repeatedly usually means the agent is re-deriving context it just
wrote down, which is fixable. Signals that pass the test become findings.
Signals that do not are left out entirely - do not pad.

For each finding choose one kind:
- "rework_loop": the same artefact was redone repeatedly because each attempt
  missed something knowable in advance.
- "context_loss": the agent re-read what it already knew, or re-derived facts
  it had earlier in the session. Usually fixed by writing the fact down.
- "missing_automation": a command or sequence repeated enough that it should
  be a script, a hook, or a skill.
- "brittle_tooling": a tool or command failed repeatedly for an environmental
  reason - wrong path, missing dependency, bad invocation.
- "unclear_target": work was aimed at the wrong thing before being redirected.
- "expensive_detour": disproportionate cost or time went somewhere that did
  not end up mattering.
- "working_as_intended": a signal that LOOKS like friction and is not. Use it
  sparingly, and only when the number is striking enough that a reader would
  otherwise flag it themselves.

Then:
- evidence: quote the actual numbers you are reasoning from, in one line.
- finding: what is happening, one sentence, concrete and specific to this
  session. Name the file, command or tool.
- fix: the change to make, phrased so it could be acted on today. It must name
  something concrete. "Improve context retention" is not a fix. If you cannot
  name a concrete change, drop the finding. For "working_as_intended" leave the
  fix empty.
- fix_location: where that change lives. Choose it from the NATURE of the fix,
  not by habit - each of these is the right answer for some findings:
  - "claude_md": a standing working rule for this repo. Only when the fix is
    genuinely a rule the agent should follow every time.
  - "skill": the capability already exists or should - the agent did not reach
    for it, or reached for the wrong one. Anything of the form "it should have
    used X" is a skill fix, not a rule.
  - "hook": it should happen automatically, without anyone remembering.
  - "tooling": a script, a config, a dependency, an environment fix. Repeated
    identical commands and environmental failures land here.
  - "docs": a fact about the system that belongs in the knowledge base.
  - "memory": a durable fact about how this person works.
  - "prompt": the work was asked for in a way that invited the problem.
  - "none": only for working_as_intended.

Choosing "claude_md" for everything is the most common way to get this wrong.
Before you write it, check whether the fix is really a rule, or whether it is a
missing skill, a missing hook, or a broken tool.
- severity: high | medium | low. High means it cost real money or real time
  and will recur.
- confidence: high | medium | low.

Rules:
- At most 5 findings. Fewer is better. A session with no real friction gets an
  empty array, and that is a good answer.
- Findings within one session should not all share a fix_location unless they
  genuinely share one.
- Never invent a signal you were not given, and never compute a rate whose
  denominator you were not given. `totals` holds the real denominators: 6 shell
  failures out of 200 commands is not "46% of turns".
- Do not restate a signal as a finding. "README.md was written 11 times" is a
  signal; "the agent rewrote README.md after each change instead of once at
  the end, because no rule says when to update it" is a finding.
- working_as_intended must have an empty fix and fix_location "none".
- Write for someone who has never seen these instructions. Do not mention them,
  quote them, or refer to "the pattern described above". State what you found.

Output ONLY a JSON array, no prose, no code fences:
[{"kind":"...","evidence":"...","finding":"...","fix":"...",
  "fix_location":"...","severity":"...","confidence":"..."}]"""


def _now() -> str:
    return (_dt.datetime.now(_dt.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _guess_location(kind: str | None, fix: str) -> str:
    """Last-resort location when the model gave one that is not a location.

    Deliberately not a constant: every finding falling into one bucket makes
    the grouped view look decisive when it is only defaulting.
    """
    f = (fix or "").lower()
    if "skill" in f:
        return "skill"
    if "hook" in f or "automatically" in f:
        return "hook"
    if any(w in f for w in ("script", "config", "install", "dependency",
                            "environment", "alias", "makefile")):
        return "tooling"
    if "document" in f or "docs/" in f or "knowledge base" in f:
        return "docs"
    return {"brittle_tooling": "tooling", "missing_automation": "hook",
            "unclear_target": "prompt"}.get(kind or "", "claude_md")


def _sig(kind, subject, n, detail):
    return {"kind": kind, "subject": subject, "n": n, "detail": detail}


def fold_paths(paths) -> dict:
    """Map every path to a canonical form, merging the same file recorded under
    two roots.

    Shell-derived rows resolve relative paths against a best-guess working
    directory, and when that guess is one level off the same file lands under
    two repo-relative paths - `src/a/B.tsx` and `atlas-video/src/a/B.tsx`. Left
    alone they read as two files each edited half as often, which is exactly
    the number a diagnosis would reason from. Fold on the suffix relation: it
    is a heuristic, but the alternative is knowingly wrong counts.
    """
    uniq = sorted(set(paths), key=len)
    canon = {}
    for p in uniq:
        target = p
        for shorter in uniq:
            if shorter is p or len(shorter) >= len(p):
                continue
            if p.endswith("/" + shorter):
                target = canon.get(shorter, shorter)
                break
        canon[p] = target
    return canon


def signals(conn, session_id: str) -> list[dict]:
    """Mechanical friction in one session. No model, no network, no cost."""
    out: list[dict] = []

    # --- file rework -------------------------------------------------------
    # `search` (a grep) and `delete` are excluded so these counts match the
    # lanes the TUI and report already show; low-confidence shell inferences
    # are excluded so a misparsed command line cannot invent a rewrite loop.
    rows = db.q(conn, """
            SELECT COALESCE(repo_relative_path, path) AS p, operation, ts
              FROM file_activity
             WHERE session_id = ? AND operation IN ('read','write','edit',
                                                    'notebook_edit')
               AND COALESCE(op_confidence,'high') <> 'low'
             ORDER BY ts""", (session_id,))
    canon = fold_paths([r["p"] for r in rows])
    ops: dict[str, list[str]] = {}
    for r in rows:
        ops.setdefault(canon[r["p"]], []).append(
            "r" if r["operation"] == "read" else "w")

    writes = sorted(((p, s.count("w")) for p, s in ops.items()),
                    key=lambda kv: -kv[1])
    for path, n in writes[:5]:
        if n >= MIN_WRITES:
            out.append(_sig("file_written_repeatedly", path, n,
                            f"{n} writes/edits"))

    # --- read/write ping-pong ---------------------------------------------
    alts = []
    for path, seq in ops.items():
        runs = [seq[0]]
        for o in seq[1:]:
            if o != runs[-1]:
                runs.append(o)
        switches = len(runs) - 1
        if switches >= MIN_SWITCHES and "w" in runs:
            reads = seq.count("r")
            alts.append(_sig("read_write_alternation", path, switches,
                             f"{switches} switches between reading and writing "
                             f"it ({reads} reads, {len(seq) - reads} writes)"))
    alts.sort(key=lambda s: -s["n"])
    out += alts[:MAX_ALTERNATION_ROWS]

    # --- re-reading something never written -------------------------------
    for r in db.q(conn, """
            SELECT COALESCE(repo_relative_path, path) AS p, COUNT(*) n
              FROM file_activity
             WHERE session_id = ? AND operation = 'read'
             GROUP BY p HAVING n >= ?
                AND p NOT IN (SELECT COALESCE(repo_relative_path, path)
                                FROM file_activity
                               WHERE session_id = ? AND operation <> 'read')
             ORDER BY n DESC LIMIT 5""",
            (session_id, MIN_REREADS, session_id)):
        out.append(_sig("file_read_repeatedly", r["p"], r["n"],
                        f"read {r['n']} times, never written"))

    # --- repeated shell commands ------------------------------------------
    for r in db.q(conn, """
            SELECT substr(command, 1, 120) AS c, COUNT(*) n
              FROM bash_activity WHERE session_id = ? AND command IS NOT NULL
             GROUP BY command_hash HAVING n >= ? ORDER BY n DESC LIMIT 5""",
            (session_id, MIN_REPEATS)):
        out.append(_sig("command_repeated", r["c"], r["n"],
                        f"run {r['n']} times unchanged"))

    for r in db.q(conn, """
            SELECT program, COUNT(*) n FROM bash_activity
             WHERE session_id = ? AND success = 0 AND program IS NOT NULL
             GROUP BY program HAVING n >= ? ORDER BY n DESC LIMIT 5""",
            (session_id, MIN_FAILURES)):
        out.append(_sig("command_failing", r["program"], r["n"],
                        f"{r['n']} failed runs"))

    # --- tool errors -------------------------------------------------------
    for r in db.q(conn, """
            SELECT tool_name, COALESCE(error_type,'error') AS e, COUNT(*) n
              FROM tool_calls
             WHERE session_id = ? AND success = 0
             GROUP BY tool_name, e HAVING n >= ? ORDER BY n DESC LIMIT 5""",
            (session_id, MIN_FAILURES)):
        out.append(_sig("tool_failing", r["tool_name"], r["n"],
                        f"{r['n']} failures ({r['e']})"))

    # --- search storms -----------------------------------------------------
    for r in db.q(conn, """
            SELECT c.prompt_id, COUNT(*) n, t.seq
              FROM tool_calls c LEFT JOIN turns t ON t.turn_id = c.prompt_id
             WHERE c.session_id = ? AND c.tool_category = 'search'
             GROUP BY c.prompt_id HAVING n >= ? ORDER BY n DESC LIMIT 3""",
            (session_id, MIN_SEARCHES)):
        out.append(_sig("search_storm", f"turn {r['seq']}", r["n"],
                        f"{r['n']} searches inside one turn"))

    # --- where the money went ---------------------------------------------
    total = db.scalar(conn, "SELECT COALESCE(SUM(cost_usd),0) FROM turns "
                            "WHERE session_id = ?", (session_id,))
    if total:
        for r in db.q(conn, """
                SELECT seq, cost_usd, duration_s,
                       substr(COALESCE(prompt_text,''),1,160) AS p
                  FROM turns WHERE session_id = ?
                   AND cost_usd >= ? AND cost_usd >= ?
                 ORDER BY cost_usd DESC LIMIT 2""",
                (session_id, total * COST_SHARE, COST_FLOOR)):
            share = int(round((r["cost_usd"] or 0) / total * 100))
            out.append(_sig("cost_spike", f"turn {r['seq']}",
                            round(r["cost_usd"] or 0, 2),
                            f"${r['cost_usd']:,.2f} - {share}% of the session"
                            + (f' - "{r["p"]}"' if r["p"] else "")))

    # --- work the human threw away ----------------------------------------
    n_rej = db.scalar(conn, """
        SELECT COALESCE(SUM(rejects),0) + COALESCE(SUM(user_overrides),0)
          FROM turns WHERE session_id = ?""", (session_id,))
    if n_rej >= 3:
        out.append(_sig("work_rejected", "tool calls", n_rej,
                        f"{n_rej} tool calls rejected or aborted by the human"))
    return out


def context_for(conn, session_id: str) -> dict | None:
    """The signals plus just enough about the session to read them against."""
    sigs = signals(conn, session_id)
    if not sigs:
        return None
    s = conn.execute("""
        SELECT session_id, project_name, cost_usd, tool_calls, bash_commands,
               files_read, files_changed,
               (SELECT COUNT(*) FROM turns t
                 WHERE t.session_id = session_summary.session_id
                   AND COALESCE(t.is_system,0) = 0) AS turns
          FROM session_summary WHERE session_id = ?""",
        (session_id,)).fetchone()
    if not s:
        return None
    from . import narrate
    causes = [dict(r) for r in db.q(conn, """
        SELECT cause, what_was_missing FROM correction_cause
         WHERE session_id = ? AND cause NOT IN ('design_iteration')""",
        (session_id,))]
    missed = [r["skill_name"] for r in db.q(conn, """
        SELECT skill_name FROM skill_audit
         WHERE session_id = ? AND verdict = 'should_have_fired'""",
        (session_id,))]
    return {
        "session_id": session_id,
        "project": s["project_name"],
        "what_it_was_doing": narrate.get(conn, "session", session_id),
        "turns": s["turns"],
        "cost_usd": round(s["cost_usd"] or 0, 2),
        # Denominators, so a rate never has to be guessed from a count.
        "totals": {
            "tool_calls": s["tool_calls"],
            "shell_commands": s["bash_commands"],
            "files_read": s["files_read"],
            "files_changed": s["files_changed"],
        },
        "signals": sigs,
        "corrections_already_diagnosed": causes,
        "skills_that_should_have_fired": missed,
    }


def diagnose_session(conn, session_id: str, model: str = MODEL) -> list | None:
    ctx = context_for(conn, session_id)
    if ctx is None:
        return []
    prompt = (f"{SYSTEM}\n\n## Session\n"
              f"{json.dumps(ctx, ensure_ascii=False, indent=1)}")
    return _ask(prompt, model) or _ask(prompt, model)   # one retry


def _store(conn, r: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO session_diagnosis(session_id, seq, kind,"
        " evidence, finding, fix, fix_location, severity, confidence, model,"
        " diagnosed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (r["session_id"], r["seq"], r["kind"], r.get("evidence"),
         r.get("finding"), r.get("fix"), r.get("fix_location"),
         r.get("severity"), r.get("confidence"), r.get("model"),
         r.get("diagnosed_at")))


def _append(r: dict) -> None:
    config.ensure_dirs()
    with config.SESSION_DX_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(r) + "\n")


def import_cached(conn) -> int:
    path = config.SESSION_DX_FILE
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
        if r.get("session_id") and r.get("kind") in FINDING_KINDS:
            _store(conn, r)
            n += 1
    conn.commit()
    return n


def pending(conn, limit: int | None = None) -> list[str]:
    rows = db.q(conn, """
        SELECT session_id FROM session_summary
         WHERE first_seen IS NOT NULL
           AND session_id NOT IN (SELECT session_id FROM session_diagnosis)
           AND session_id NOT IN (SELECT session_id FROM session_dx_clean)
         ORDER BY cost_usd DESC""")
    ids = [r["session_id"] for r in rows]
    return ids[:limit] if limit else ids


def forget(conn, session_ids: list[str] | None = None) -> int:
    """Drop findings so a prompt or threshold change is not outlived by
    verdicts produced under the old rules."""
    path = config.SESSION_DX_FILE
    if session_ids is None:
        n = conn.execute("DELETE FROM session_diagnosis").rowcount
        conn.execute("DELETE FROM session_dx_clean")
        conn.commit()
        if path.exists():
            path.unlink()
        return n
    marks = ",".join("?" for _ in session_ids)
    n = conn.execute(
        f"DELETE FROM session_diagnosis WHERE session_id IN ({marks})",
        session_ids).rowcount
    conn.execute(f"DELETE FROM session_dx_clean WHERE session_id IN ({marks})",
                 session_ids)
    conn.commit()
    if path.exists():
        keep = [l for l in path.read_text().splitlines()
                if l.strip() and json.loads(l).get("session_id") not in session_ids]
        path.write_text("\n".join(keep) + ("\n" if keep else ""))
    return n


def diagnose(conn, model: str = MODEL, limit: int | None = None,
             progress=None, redo: bool = False,
             session_id: str | None = None) -> dict:
    if session_id:
        forget(conn, [session_id])
        todo = [session_id]
    else:
        if redo:
            forget(conn)
        todo = pending(conn, limit)
    stats = {"pending": len(todo), "sessions": 0, "findings": 0,
             "clean": 0, "failed": 0}
    for sid in todo:
        if progress:
            progress(sid)
        try:
            got = diagnose_session(conn, sid, model)
        except Exception:
            got = None
        if got is None:
            stats["failed"] += 1
            continue
        stats["sessions"] += 1
        rows = [x for x in got if isinstance(x, dict)
                and x.get("kind") in FINDING_KINDS]
        if not rows:
            # Record the absence too, or every re-run pays again for the
            # sessions that are genuinely fine.
            conn.execute("INSERT OR REPLACE INTO session_dx_clean"
                         "(session_id, model, diagnosed_at) VALUES (?,?,?)",
                         (sid, model, _now()))
            conn.commit()
            stats["clean"] += 1
            continue
        for i, x in enumerate(rows[:5]):
            loc = x.get("fix_location")
            fix = (x.get("fix") or "").strip()
            if x["kind"] == "working_as_intended":
                fix, loc = "", "none"
            elif loc not in LOCATIONS or (fix and loc == "none"):
                # A fix written next to "nowhere to put it" is contradictory.
                # Guess from the finding rather than defaulting to one bucket:
                # a default here becomes indistinguishable from a real answer
                # once the results are grouped by location.
                loc = _guess_location(x.get("kind"), fix) if fix else "none"
            rec = {"session_id": sid, "seq": i, "kind": x["kind"],
                   "evidence": x.get("evidence"), "finding": x.get("finding"),
                   "fix": fix or None, "fix_location": loc,
                   "severity": x.get("severity"),
                   "confidence": x.get("confidence"),
                   "model": model, "diagnosed_at": _now()}
            _store(conn, rec)
            _append(rec)
            stats["findings"] += 1
        conn.commit()
    return stats


def for_session(conn, session_id: str) -> list:
    return db.q(conn, """
        SELECT kind, evidence, finding, fix, fix_location, severity, confidence
          FROM session_diagnosis WHERE session_id = ? ORDER BY seq""",
        (session_id,))


def by_kind(conn) -> list:
    return db.q(conn, """
        SELECT kind, COUNT(*) n, COUNT(DISTINCT session_id) sessions
          FROM session_diagnosis GROUP BY kind ORDER BY n DESC""")


def top_findings(conn, limit: int = 20) -> list:
    return db.q(conn, """
        SELECT d.session_id, substr(d.session_id,1,8) sess, s.project_name,
               d.kind, d.severity, d.finding, d.fix, d.fix_location,
               d.evidence, s.cost_usd AS total_cost_usd
          FROM session_diagnosis d
          LEFT JOIN session_summary s ON s.session_id = d.session_id
         WHERE d.kind <> 'working_as_intended'
         ORDER BY CASE d.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1
                                  ELSE 2 END,
                  s.cost_usd DESC LIMIT ?""", (limit,))


def by_location(conn) -> list:
    """Fixes grouped by where they would live, with the spend behind each.

    A finding on one session is an anecdote. The same fix landing in the same
    place across four sessions is a backlog item, and the cost of those
    sessions is the argument for doing it.
    """
    return db.q(conn, """
        SELECT d.fix_location, COUNT(*) n,
               COUNT(DISTINCT d.session_id) sessions,
               COALESCE(SUM(s.cost_usd), 0) cost,
               SUM(d.severity = 'high') high
          FROM session_diagnosis d
          LEFT JOIN session_summary s ON s.session_id = d.session_id
         WHERE d.kind <> 'working_as_intended' AND d.fix IS NOT NULL
         GROUP BY d.fix_location ORDER BY cost DESC""")


def coverage(conn) -> dict:
    """How much of the spend has actually been looked at."""
    total = db.scalar(conn, "SELECT COALESCE(SUM(cost_usd),0) FROM"
                            " session_summary WHERE first_seen IS NOT NULL")
    seen = db.scalar(conn, """
        SELECT COALESCE(SUM(cost_usd),0) FROM session_summary
         WHERE session_id IN (SELECT session_id FROM session_diagnosis
                              UNION SELECT session_id FROM session_dx_clean)""")
    return {
        "sessions": db.scalar(conn, "SELECT COUNT(*) FROM session_summary"
                                    " WHERE first_seen IS NOT NULL"),
        "reviewed": db.scalar(conn, """
            SELECT COUNT(*) FROM (SELECT session_id FROM session_diagnosis
                                  UNION SELECT session_id FROM session_dx_clean)"""),
        "with_findings": db.scalar(conn, "SELECT COUNT(DISTINCT session_id)"
                                         " FROM session_diagnosis"),
        "clean": db.scalar(conn, "SELECT COUNT(*) FROM session_dx_clean"),
        "cost_total": total, "cost_reviewed": seen,
    }
