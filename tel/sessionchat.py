"""Ask questions about one session, and keep the conversation.

The report and the TUI answer the questions someone thought to build a view
for. This answers the rest: "why did this take two hours", "what was it doing
between 14:00 and 14:30", "did it ever read the Checkout doc".

The agent is given a compact factual brief - what happened in the session,
drawn from the same normalized tables everything else uses - plus the
conversation so far. It is told to answer from the brief and to say when the
brief does not cover something, because a plausible invented answer about your
own telemetry is worse than no answer.

Conversations are stored per session and appended to a sidecar, so they
survive a database rebuild like every other thing here that costs money.
"""
from __future__ import annotations

import datetime as _dt
import json

from . import config, db
from .skillaudit import _ask as _ask_json   # noqa: F401  (kept for parity)

MODEL = "sonnet"
MAX_TURNS_SENT = 60
MAX_HISTORY_SENT = 12

SYSTEM = """You answer questions about a single recorded session of a developer
working with an AI coding agent. You are given a factual brief assembled from
local telemetry: the turns, the files touched, the commands run, the skills and
tools used, the commits, and any diagnosis already made.

Rules:
- Answer from the brief. It is the whole of what was recorded.
- When the brief does not contain the answer, say so plainly and name what
  would be needed. Never fill a gap with a plausible guess - the person asking
  can check, and a wrong answer about their own telemetry is worse than none.
- Timestamps are UTC. Durations in the brief are already computed; do not
  recompute them from timestamps. Each turn carries `at`, and files and
  commands carry `first_at`/`last_at`, so "when did X happen" can usually be
  answered by placing a first_at between two turns' `at` values.
- Be concrete and brief. Quote the numbers and paths you are reasoning from.
  Two or three sentences is usually the right length; use a short list when the
  answer really is a list.
- No preamble, no restating the question, no offers to help further."""


def _now() -> str:
    return (_dt.datetime.now(_dt.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def brief(conn, session_id: str) -> dict:
    """Everything known about one session, small enough to send."""
    from . import narrate, queries as Q, sessiondx

    s = conn.execute("""
        SELECT session_id, project_name, cost_usd, first_seen, last_seen,
               duration_s, tool_calls, bash_commands, files_read, files_changed,
               app_version
          FROM session_summary WHERE session_id = ?""",
        (session_id,)).fetchone()
    if not s:
        return {}

    turns = [{
        "seq": t["seq"], "at": t["started_at"],
        "took_s": int(t["duration_s"] or 0),
        "waited_before_s": int(t["gap_before_s"] or 0),
        "cost_usd": round(t["cost_usd"] or 0, 2),
        "kind": ("system" if t["is_system"] else "correction" if t["is_correction"]
                 else "steering" if t["is_steering"] else "normal"),
        "tool_calls": t["tool_calls"], "tool_failures": t["tool_failures"],
        "prompt": (t["prompt_text"] or "")[:400] or None,
    } for t in db.q(conn, """
        SELECT seq, started_at, duration_s, gap_before_s, cost_usd, is_system,
               is_correction, is_steering, tool_calls, tool_failures, prompt_text
          FROM turns WHERE session_id = ? ORDER BY seq LIMIT ?""",
        (session_id, MAX_TURNS_SENT))]

    files = [{"path": r["p"], "reads": r["reads"], "writes": r["writes"],
              "first_at": r["first_at"], "last_at": r["last_at"]}
             for r in db.q(conn, """
        SELECT COALESCE(repo_relative_path, path) AS p,
               SUM(operation = 'read') AS reads,
               SUM(operation IN ('write','edit','notebook_edit')) AS writes,
               MIN(ts) AS first_at, MAX(ts) AS last_at
          FROM file_activity
         WHERE session_id = ? AND operation IN ('read','write','edit',
                                                'notebook_edit')
         GROUP BY p ORDER BY (reads + writes) DESC LIMIT 40""", (session_id,))]

    commands = [{"command": r["c"], "runs": r["n"], "failures": r["f"],
                 "first_at": r["first_at"], "last_at": r["last_at"]}
                for r in db.q(conn, """
        SELECT substr(command,1,140) AS c, COUNT(*) n,
               SUM(COALESCE(success,1) = 0) AS f,
               MIN(ts) AS first_at, MAX(ts) AS last_at
          FROM bash_activity WHERE session_id = ? AND command IS NOT NULL
         GROUP BY command_hash ORDER BY n DESC LIMIT 25""", (session_id,))]

    tools = {r["tool_name"]: r["n"] for r in db.q(conn, """
        SELECT tool_name, COUNT(*) n FROM tool_calls
         WHERE session_id = ? GROUP BY 1 ORDER BY n DESC LIMIT 20""",
        (session_id,)) if r["tool_name"]}

    skills = {r["skill_name"]: r["n"] for r in db.q(conn, """
        SELECT skill_name, COUNT(*) n FROM skill_calls
         WHERE session_id = ? AND skill_name IS NOT NULL
         GROUP BY 1 ORDER BY n DESC LIMIT 20""", (session_id,))}

    commits = [{"sha": r["commit_sha"][:8], "subject": r["subject"]}
               for r in db.q(conn, """
        SELECT commit_sha, subject FROM git_activity
         WHERE session_id = ? ORDER BY committed_at LIMIT 25""", (session_id,))]

    findings = [{"kind": f["kind"], "severity": f["severity"],
                 "finding": f["finding"], "evidence": f["evidence"],
                 "fix": f["fix"]}
                for f in Q.session_findings(conn, session_id)]

    return {
        "session_id": session_id,
        "project": s["project_name"],
        "description": narrate.get(conn, "session", session_id),
        "started_utc": s["first_seen"], "ended_utc": s["last_seen"],
        "cost_usd": round(s["cost_usd"] or 0, 2),
        "totals": {"turns": len(turns), "tool_calls": s["tool_calls"],
                   "shell_commands": s["bash_commands"],
                   "file_reads": s["files_read"],
                   "file_writes": s["files_changed"]},
        "turns": turns,
        "files_touched": files,
        "commands": commands,
        "tools_used": tools,
        "skills_used": skills,
        "commits": commits,
        "friction_signals": sessiondx.signals(conn, session_id),
        "diagnosis": findings,
    }


def history(conn, session_id: str) -> list[dict]:
    return [dict(r) for r in db.q(conn, """
        SELECT seq, role, text, model, ts FROM session_chat
         WHERE session_id = ? ORDER BY seq""", (session_id,))]


def _store(conn, r: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO session_chat(session_id, seq, role, text,"
        " model, ts) VALUES (?,?,?,?,?,?)",
        (r["session_id"], r["seq"], r["role"], r["text"], r.get("model"),
         r["ts"]))


def _append(r: dict) -> None:
    config.ensure_dirs()
    with config.SESSION_CHAT_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(r) + "\n")


def import_cached(conn) -> int:
    path = config.SESSION_CHAT_FILE
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
        if r.get("session_id") and r.get("role") in ("user", "assistant"):
            _store(conn, r)
            n += 1
    conn.commit()
    return n


def forget(conn, session_id: str) -> int:
    n = conn.execute("DELETE FROM session_chat WHERE session_id = ?",
                     (session_id,)).rowcount
    conn.commit()
    path = config.SESSION_CHAT_FILE
    if path.exists():
        keep = [l for l in path.read_text().splitlines()
                if l.strip() and json.loads(l).get("session_id") != session_id]
        path.write_text("\n".join(keep) + ("\n" if keep else ""))
    return n


def _argv(prompt: str, model: str) -> list[str]:
    return ["claude", "-p", prompt, "--model", model,
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--settings", json.dumps(
                {"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "0"}, "hooks": {}})]


def _env() -> dict:
    import os
    return {**os.environ, "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
            "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "0"}


def spawn(prompt: str, model: str = MODEL):
    """Start the model and hand back a handle to poll.

    Deliberately not `subprocess.run` in a thread: a blocking call inside a
    curses app has to fight the GIL with `getch`, and output goes to temp
    files rather than pipes so a full pipe buffer can never wedge a child that
    nobody is reading from yet. The caller polls, so the UI keeps drawing.
    """
    import subprocess
    import tempfile
    out = tempfile.NamedTemporaryFile("w+", suffix=".out", delete=False)
    err = tempfile.NamedTemporaryFile("w+", suffix=".err", delete=False)
    try:
        proc = subprocess.Popen(
            _argv(prompt, model), stdout=out, stderr=err,
            stdin=subprocess.DEVNULL, env=_env(),
            # No controlling terminal: from inside curses the child would
            # otherwise be free to open /dev/tty and wait there.
            start_new_session=True)
    except (OSError, ValueError) as exc:
        return {"proc": None, "out": out.name, "err": err.name,
                "error": f"{type(exc).__name__}: {exc}"}
    return {"proc": proc, "out": out.name, "err": err.name, "error": None}


def reap(handle) -> str | None:
    """None while still running; the answer (or an `__error__ ...`) once done."""
    import pathlib as _pl
    if handle.get("error"):
        return "__error__ " + handle["error"]
    proc = handle["proc"]
    if proc.poll() is None:
        return None
    def _read(key):
        try:
            return _pl.Path(handle[key]).read_text(errors="replace")
        except OSError:
            return ""
    text, err = _read("out").strip(), _read("err").strip()
    for key in ("out", "err"):
        try:
            _pl.Path(handle[key]).unlink()
        except OSError:
            pass
    if proc.returncode != 0:
        return "__error__ " + (err or f"claude exited {proc.returncode}")[:300]
    return text or "__error__ empty response"


def _call(prompt: str, model: str, timeout: int = 300) -> str | None:
    """Blocking form, for the CLI where there is no UI to keep alive."""
    import time
    h = spawn(prompt, model)
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = reap(h)
        if got is not None:
            return got
        time.sleep(0.2)
    if h.get("proc"):
        h["proc"].kill()
    return f"__error__ timed out after {timeout}s"


def build_prompt(conn, session_id: str, question: str) -> str:
    """The brief, the conversation so far, and the question.

    The whole conversation is re-sent each time rather than resumed. It is a
    handful of short turns about one session, so the simplicity is worth more
    than the tokens saved, and it means a conversation survives anything - a
    restart, a rebuild, a different machine reading the sidecar.
    """
    b = brief(conn, session_id)
    if not b:
        return ""
    past = history(conn, session_id)[-MAX_HISTORY_SENT:]
    convo = "\n\n".join(
        f"{'Question' if h['role'] == 'user' else 'Answer'}: {h['text']}"
        for h in past)
    return (f"{SYSTEM}\n\n## Session brief\n"
            f"{json.dumps(b, ensure_ascii=False, indent=1)}\n\n"
            + (f"## Conversation so far\n{convo}\n\n" if convo else "")
            + f"## Question\n{question}")


def record(conn, session_id: str, role: str, text: str,
           model: str | None = None) -> dict:
    seq = db.scalar(conn, "SELECT COALESCE(MAX(seq),-1) + 1 FROM session_chat"
                          " WHERE session_id = ?", (session_id,))
    rec = {"session_id": session_id, "seq": seq, "role": role, "text": text,
           "model": model, "ts": _now()}
    _store(conn, rec)
    _append(rec)
    conn.commit()
    return rec


def ask(conn, session_id: str, question: str, model: str = MODEL) -> str:
    """Ask and wait. The TUI drives spawn/reap itself so it can keep drawing."""
    prompt = build_prompt(conn, session_id, question)
    if not prompt:
        return "no such session in the database"
    record(conn, session_id, "user", question)
    answer = _call(prompt, model) or "__error__ empty response"
    record(conn, session_id, "assistant", answer, model)
    return answer
