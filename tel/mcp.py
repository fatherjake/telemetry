"""A read-only MCP server over the database, spoken on stdin/stdout.

This is the view layer. Everything the old per-view commands printed as a
table is here as a tool, so the numbers can be asked about in the place the
questions actually occur - inside a Claude Code session - instead of being
read out of a terminal and pasted back in.

It talks to the database file, not to the collector. That means it answers
whether or not anything is running, needs no port and no auth, and behaves
the same under Docker and the fallback receiver.

Transport is JSON-RPC 2.0, one object per line. stdout carries protocol
traffic and nothing else; anything worth saying goes to stderr.

Read-only by construction: the connection is opened against a URI with
`mode=ro`, so a bug here cannot corrupt a database, and `telemetry_sql` cannot be
talked into writing.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import traceback
from pathlib import Path

from . import config, queries as Q

SERVER_NAME = config.MCP_SERVER_NAME
SERVER_VERSION = "1.0.0"

# Versions whose shape this server implements. A client asking for something
# else is answered with the newest we know rather than refused: the parts of
# the protocol used here have not changed between them.
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = SUPPORTED_PROTOCOLS[0]

SQL_ROW_CAP = 500


# --------------------------------------------------------------- helpers ---

def _open() -> sqlite3.Connection:
    """Read-only connection to the database."""
    path = Path(config.DB_PATH)
    if not path.exists():
        raise FileNotFoundError(
            f"no database at {path} - run `telemetry analyse` first")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _round(value, places: int = 2):
    return round(value, places) if isinstance(value, (int, float)) else value


def _session_id(conn, prefix: str) -> str:
    """Resolve a session id prefix, the way every other entry point does."""
    exact = conn.execute("SELECT session_id FROM sessions WHERE session_id=?",
                         (prefix,)).fetchone()
    if exact:
        return exact["session_id"]
    hits = conn.execute(
        "SELECT session_id FROM sessions WHERE session_id LIKE ? LIMIT 5",
        (prefix + "%",)).fetchall()
    if not hits:
        raise ValueError(f"no session matching {prefix!r}")
    if len(hits) > 1:
        raise ValueError(
            f"{prefix!r} matches {len(hits)} sessions: "
            + ", ".join(h["session_id"][:8] for h in hits))
    return hits[0]["session_id"]


# ----------------------------------------------------------------- tools ---

def t_overview(conn, args: dict) -> dict:
    return {
        "observed_from": Q.observation_period(conn)[0],
        "observed_to": Q.observation_period(conn)[1],
        "totals": dict(Q.overview(conn)),
        "output": Q.output_summary(conn),
        "friction": Q.friction_totals(conn),
        "note": "the unit here is the session: one session, one piece of work, "
                "its own cost and its own commits",
    }


def t_sessions(conn, args: dict) -> dict:
    limit = int(args.get("limit") or 25)
    project = (args.get("project") or "").lower()
    out = []
    for d in Q.session_detail(conn):
        s = d["session"]
        if project and project not in ((s.get("project_name") or "")
                                       + (s.get("project_id") or "")).lower():
            continue
        e = d["effort"]
        out.append({
            "session_id": s["session_id"],
            "title": s["title"],
            "described": bool(d["description"]),
            "project": s["project_name"],
            "started": s["first_seen"], "ended": s["last_seen"],
            "duration_s": _round(s["duration_s"], 0),
            "cost_usd": _round(s["cost"]),
            "turns": e["turns"], "corrections": e["corrections"],
            "steers": e["steers"],
            "commits": s["commits"],
            "lines": {"insertions": s["insertions"], "deletions": s["deletions"]},
            "prod_deploys": s["prod_deploys"], "reverted": s["reverted"],
            "doc_gaps": len(d["doc_gaps"]),
        })
        if len(out) >= limit:
            break
    return {"sessions": out, "order": "most recent first",
            "note": "titles come from `telemetry enrich --what describe`, which "
                    "summarises each session's prompts into its goal"}


def t_session(conn, args: dict) -> dict:
    sid = _session_id(conn, str(args.get("session_id") or ""))
    detail = Q.session_detail(conn, session_ids=[sid])
    if not detail:
        return {"session_id": sid,
                "note": "no turns or tool calls recorded for this session"}
    d = detail[0]
    out = {
        "session": {k: _round(v) for k, v in d["session"].items()},
        "description": d["description"],
        "effort": {k: _round(v) for k, v in d["effort"].items()},
        "commits": d["commits"], "deployments": d["deploys"],
        "skills": d["skills"], "mcp_servers": d["mcps"],
        "corrections": d["corrections"], "missed_skills": d["missed_skills"],
        "docs_read": d["docs_read"], "doc_gaps": d["doc_gaps"],
        "findings": _rows(d["findings"]),
    }
    if args.get("include_turns", True):
        out["turns"] = [{k: _round(v) for k, v in t.items()} for t in d["turns"]]
    if not out["findings"]:
        out["findings_note"] = ("nothing diagnosed yet - `telemetry enrich --what "
                                "diagnose` writes these, and it costs money")
    return out


def t_friction(conn, args: dict) -> dict:
    limit = int(args.get("limit") or 20)
    return {
        "totals": Q.friction_totals(conn),
        "by_session": _rows(Q.friction_by_session(conn, limit)),
        "rework_files": _rows(Q.rework_files(conn, limit)),
        "recent_corrections": _rows(Q.corrections(conn, limit)),
        "note": "corrections and steers are the human paying for a miss; "
                "rework is the same file edited across many turns",
    }


def t_inventory(conn, args: dict) -> dict:
    skills = _rows(Q.skill_usage(conn))
    mcps = _rows(Q.mcp_usage(conn))
    return {
        "skills": skills,
        "skills_never_used": [s["name"] for s in skills if not s["invocations"]],
        "skills_used_but_not_installed": _rows(Q.skills_used_not_installed(conn)),
        "mcp_servers": mcps,
        "mcp_never_used": [m["name"] for m in mcps if not m["calls"]],
        "subagents": _rows(Q.subagents(conn)),
    }


def t_files(conn, args: dict) -> dict:
    limit = int(args.get("limit") or 40)
    prefix = args.get("under")
    out = {
        "file_access": _rows(Q.file_access(conn, prefix, limit)),
        "hot_files": _rows(Q.hot_files(conn, limit)),
        "hot_dirs": _rows(Q.hot_dirs(conn, 15)),
        "created": _rows(Q.created_files(conn, limit)),
    }
    root = args.get("unread_under")
    if root:
        out["unread"] = Q.unread_files(conn, root, args.get("pattern") or "*.md")
    return out


def t_docs(conn, args: dict) -> dict:
    from . import docs as D
    root = args.get("root")
    summary = D.summary(conn, root)
    if not summary.get("total"):
        return {"documents": 0,
                "note": "no knowledge base scanned yet - `telemetry enrich --what "
                        "docs --root <dir>` scans and profiles one"}
    return {
        "summary": summary,
        "cold_spots": _rows(D.cold_spots(conn, root))[:int(args.get("limit") or 30)],
        "consulted": [dict(r) for r in D.coverage(conn, root) if r["reads"]],
        "gaps_by_document": _rows(D.gap_summary(conn)),
        "note": "a cold spot is an agent-facing document nothing has ever opened; "
                "human-only documents are excluded on purpose",
    }


def t_sql(conn, args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("pass a query")
    if not query.lower().startswith(("select", "with", "explain", "pragma")):
        raise ValueError("read-only: queries must start with SELECT, WITH, "
                         "EXPLAIN or PRAGMA")
    limit = min(int(args.get("limit") or 100), SQL_ROW_CAP)
    rows = conn.execute(query).fetchmany(limit + 1)
    truncated = len(rows) > limit
    return {"rows": _rows(rows[:limit]), "truncated": truncated,
            "note": f"stopped at {limit} rows" if truncated else None}


def t_schema(conn, args: dict) -> dict:
    """The table shapes, so telemetry_sql can be written without guessing."""
    tables = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    only = args.get("table")
    out = {}
    for t in tables:
        if only and only.lower() not in t["name"].lower():
            continue
        cols = conn.execute(f"PRAGMA table_info({t['name']})").fetchall()
        out[t["name"]] = [c["name"] for c in cols]
    return {"tables": out}


TOOLS: list[dict] = [
    {
        "name": "telemetry_overview",
        "description": "Totals for the whole database: observation period, spend, "
                       "tokens, tool calls, what shipped, human friction, and how "
                       "much spend is attributable to a unit of work. Start here.",
        "handler": t_overview,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "telemetry_sessions",
        "description": "The unit of work: one session, one item. Most recent "
                       "first, each with what it was for, what it cost, what it "
                       "committed and shipped, and how much correcting it took.",
        "handler": t_sessions,
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "default 25"},
                "project": {"type": "string",
                            "description": "substring of a project name"},
            },
        },
    },
    {
        "name": "telemetry_session",
        "description": "One session in full: its turns, which were corrections "
                       "or steering, what it cost, its commits and deployments, "
                       "the skills and MCP servers it used, documents it should "
                       "have read, and any diagnosis already written.",
        "handler": t_session,
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string",
                               "description": "full id or a unique prefix"},
                "include_turns": {"type": "boolean",
                                  "description": "default true; false for just the summary"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "telemetry_friction",
        "description": "Where the human had to intervene: corrections, steering, "
                       "permission rejections, tool failures, and the files edited "
                       "over and over. The cost of getting it wrong.",
        "handler": t_friction,
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "default 20"}},
        },
    },
    {
        "name": "telemetry_inventory",
        "description": "Installed skills and configured MCP servers against what "
                       "was actually invoked, plus subagent use. The entries with "
                       "zero invocations are the point.",
        "handler": t_inventory,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "telemetry_files",
        "description": "Which files were read, written and created, including "
                       "through the shell, and which directories the work "
                       "concentrated in. Pass unread_under to list files on disk "
                       "that were never opened.",
        "handler": t_files,
        "inputSchema": {
            "type": "object",
            "properties": {
                "under": {"type": "string", "description": "path prefix filter"},
                "unread_under": {"type": "string",
                                 "description": "directory to check for never-opened files"},
                "pattern": {"type": "string", "description": "default *.md"},
                "limit": {"type": "integer", "description": "default 40"},
            },
        },
    },
    {
        "name": "telemetry_docs",
        "description": "Knowledge-base coverage: which documents were consulted, "
                       "which agent-facing ones never were (cold spots), and which "
                       "a session should have read but did not.",
        "handler": t_docs,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "limit to one scanned root"},
                "limit": {"type": "integer", "description": "cold spots to return, default 30"},
            },
        },
    },
    {
        "name": "telemetry_schema",
        "description": "Tables and columns in the database, for writing telemetry_sql.",
        "handler": t_schema,
        "inputSchema": {
            "type": "object",
            "properties": {"table": {"type": "string",
                                     "description": "filter to matching table names"}},
        },
    },
    {
        "name": "telemetry_sql",
        "description": "Read-only SQL against the database, for anything the other "
                       "tools do not answer. SELECT/WITH/EXPLAIN/PRAGMA only. Call "
                       "telemetry_schema first if unsure of the shape.",
        "handler": t_sql,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer",
                          "description": f"rows to return, max {SQL_ROW_CAP}"},
            },
            "required": ["query"],
        },
    },
]

BY_NAME = {t["name"]: t for t in TOOLS}


# ------------------------------------------------------------- transport ---

def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, default=str) + "\n")
    sys.stdout.flush()


def _result(req_id, result: dict) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}})


def _call_tool(params: dict) -> dict:
    name = params.get("name")
    tool = BY_NAME.get(name)
    if tool is None:
        return {"content": [{"type": "text", "text": f"no such tool: {name}"}],
                "isError": True}
    args = params.get("arguments") or {}
    conn = None
    try:
        conn = _open()
        payload = tool["handler"](conn, args)
        return {"content": [{"type": "text",
                             "text": json.dumps(payload, default=str)}]}
    except (ValueError, LookupError, FileNotFoundError, sqlite3.Error) as exc:
        # Asking for a session that does not exist, or writing bad SQL, is a
        # message back to the caller - not something to spill a traceback over.
        print(f"{name}: {exc}", file=sys.stderr)
        return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True}
    except Exception as exc:                      # reported, never raised at the client
        print(f"{name} failed: {traceback.format_exc()}", file=sys.stderr)
        return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True}
    finally:
        if conn is not None:
            conn.close()


def handle(message: dict) -> dict | None:
    """One request in, one response out. None for notifications."""
    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}
    if method == "tools/list":
        return {"tools": [{k: v for k, v in t.items() if k != "handler"}
                          for t in TOOLS]}
    if method == "tools/call":
        return _call_tool(params)
    if method == "ping":
        return {}
    if method and method.startswith("notifications/"):
        return None
    if req_id is None:
        return None
    raise LookupError(method or "(no method)")


def serve(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _error(None, -32700, "parse error")
            continue
        req_id = message.get("id")
        try:
            result = handle(message)
        except LookupError as exc:
            _error(req_id, -32601, f"method not found: {exc}")
            continue
        except Exception as exc:
            print(traceback.format_exc(), file=sys.stderr)
            _error(req_id, -32603, f"{type(exc).__name__}: {exc}")
            continue
        if result is not None and req_id is not None:
            _result(req_id, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
