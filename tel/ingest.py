"""Raw OTLP JSONL  ->  normalized SQLite.

Incremental and idempotent: each raw file has a line cursor in `raw_files`,
and every normalized row carries a dedupe key, so re-running `analyse` never
double counts. The raw files themselves are never modified.
"""
from __future__ import annotations

import collections
import datetime as _dt
import fnmatch
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from . import config, db, gitctx, otlp, shellfiles
from .redact import (filter_tool_params, hash_text, scrub, scrub_deep)

# --------------------------------------------------------------- constants ---

EVENT_PREFIX = "claude_code."

# Tool name -> category. Only categories we can justify from observed tool
# names are used; anything unrecognised falls through to "other".
FILE_TOOLS = {"Read": "read", "Edit": "edit", "MultiEdit": "edit",
              "Write": "write", "NotebookEdit": "notebook_edit"}
SHELL_TOOLS = {"Bash", "BashOutput", "KillShell", "KillBash"}
SEARCH_TOOLS = {"Grep", "Glob", "LS", "ToolSearch"}
PLANNING_TOOLS = {"TodoWrite", "ExitPlanMode", "EnterPlanMode"}
# Claude Code reports every MCP invocation with the literal tool_name
# "mcp_tool"; the real server and tool live in tool_parameters.
MCP_TOOL_NAMES = {"mcp_tool"}
WEB_TOOLS = {"WebSearch", "WebFetch"}
SUBAGENT_TOOLS = {"Task", "Agent"}
SKILL_TOOLS = {"Skill"}


def tool_category(tool_name: str | None) -> str:
    if not tool_name:
        return "unknown"
    if tool_name.startswith("mcp__") or tool_name in MCP_TOOL_NAMES:
        return "mcp"
    if tool_name in FILE_TOOLS:
        return "file"
    if tool_name in SHELL_TOOLS:
        return "shell"
    if tool_name in SEARCH_TOOLS:
        return "search"
    if tool_name in WEB_TOOLS:
        return "web"
    if tool_name in SUBAGENT_TOOLS:
        return "subagent"
    if tool_name in SKILL_TOOLS:
        return "skill"
    if tool_name in PLANNING_TOOLS:
        return "planning"
    return "other"


# Shell tokens that are not themselves the work being done.
#   WRAPPERS   - transparent: the real program is the next token (`sudo docker`)
#   TERMINAL   - the segment carries no program at all (`cd /some/path`)
#   LOW_SIGNAL - real programs, but plumbing; kept in `programs`, avoided as
#                the primary when something more meaningful is present
_WRAPPERS = {"sudo", "env", "time", "nohup", "exec", "command", "builtin",
             "eval", "npx", "bunx", "pnpx", "xargs", "nice",
             "then", "do", "else", "elif"}   # `do tsc $f` - tsc is the program
_TERMINAL = {"cd", "export", "set", "unset", "source", ".", "fi", "done",
             "if", "while", "for"}
_LOW_SIGNAL = {"echo", "printf", "cat", "head", "tail", "wc", "ls", "pwd",
               "true", "false", "sleep", "which", "basename", "dirname",
               "tee", "sort", "uniq", "tr", "cut", "date", "mkdir", "touch"}

_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def bash_programs(command: str | None) -> list[str]:
    """Every meaningful program in a command line, in order.

    `cd /x/y && sed -n '1,40p' f` yields ["sed"]: the directory change is not
    the work, and `/x/y` is an argument, not a program. Pipelines and `&&`
    chains each contribute their own program.
    """
    if not command:
        return []
    # Everything after a heredoc marker is data, not shell. Without this, the
    # body of `python3 - <<EOF ... import json ...` parses as programs.
    head = command.split("<<", 1)[0]
    out: list[str] = []
    for segment in _SEGMENT_SPLIT.split(head):
        segment = segment.strip()
        if segment.startswith("#"):
            continue
        for token in segment.split():
            token = token.strip("()`$'\"")
            if not token or _ENV_ASSIGN.match(token):
                continue          # leading FOO=bar assignments
            if token.startswith("-"):
                break             # a flag: we already passed the program
            name = token.rsplit("/", 1)[-1]
            if name in _WRAPPERS:
                continue          # transparent: keep looking in this segment
            if name in _TERMINAL:
                break             # nothing in this segment is a program
            if name and name not in out:
                out.append(name)
            break
    return out


def primary_program(programs: list[str]) -> str | None:
    """The most descriptive program in a command line."""
    if not programs:
        return None
    for name in programs:
        if name not in _LOW_SIGNAL:
            return name
    return programs[0]


def mcp_parts(tool_name: str | None) -> tuple[str | None, str | None]:
    """Split `mcp__server__tool` into (server, tool)."""
    if not tool_name or not tool_name.startswith("mcp__"):
        return None, None
    bits = tool_name.split("__")
    server = bits[1] if len(bits) > 1 else None
    tool = "__".join(bits[2:]) if len(bits) > 2 else None
    return server, tool


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _as_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_bool_int(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    s = str(v).strip().lower()
    if s in ("true", "1", "yes"):
        return 1
    if s in ("false", "0", "no"):
        return 0
    return None


def _maybe_json(v):
    """Attribute values arrive as JSON strings; decode when possible."""
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s.startswith(("{", "[")):
            try:
                return json.loads(s)
            except (ValueError, TypeError):
                return None
    return None


def _attr_hash(d: dict) -> str:
    return hashlib.sha256(
        json.dumps(d, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


# ------------------------------------------------------------ raw scanning ---

SIGNAL_GLOBS = {"logs": "logs*.jsonl", "metrics": "metrics*.jsonl", "traces": "traces*.jsonl"}


def scan_raw_files(raw_dir: Path | None = None) -> list[tuple[Path, str]]:
    raw_dir = Path(raw_dir or config.RAW_DIR)
    found: list[tuple[Path, str]] = []
    if not raw_dir.exists():
        return found
    for signal, pattern in SIGNAL_GLOBS.items():
        for p in sorted(raw_dir.glob(pattern)):
            if p.is_file():
                found.append((p, signal))
    return found


# ------------------------------------------------------------- normalizing ---

class Ingestor:
    def __init__(self, conn: sqlite3.Connection, progress=None,
                 describe: bool | None = None):
        self.conn = conn
        # None follows the policy file. A caller passes False when it analyses
        # on a timer and does not want a model call every tick.
        self.describe = config.AUTO_DESCRIBE if describe is None else bool(describe)
        # Where a caller wants to say what is happening. The TUI runs analyse
        # on a background thread behind a curses screen, so this is a callback
        # rather than a print.
        self.progress = progress or (lambda _msg: None)
        self.counts = {"logs": 0, "metrics": 0, "traces": 0, "skipped": 0,
                       "described": 0}
        # session_id -> merged standard attributes seen so far
        self.session_attrs: dict[str, dict] = {}

    # -- entry point ---------------------------------------------------------

    def run(self, raw_dir: Path | None = None) -> dict:
        started = _now()
        files = scan_raw_files(raw_dir)
        for path, signal in files:
            self._ingest_file(path, signal)
        self.conn.commit()

        self._import_session_context()
        self._flush_sessions()
        self._resolve_projects()
        # Needs session cwd, which _resolve_projects has just settled.
        self._derive_shell_files()
        self._apply_file_ignores()
        self._propagate_project_ids()
        # Session timestamps must be settled before the two passes that
        # depend on a session's time window.
        self._finalise_sessions()
        self._derive_file_creation()
        self._derive_turns()
        if config.GIT_RECONCILE:
            self._reconcile_git()
            self._classify_commits()
        # Inventory is a local directory scan and several analyses depend on
        # it, so refresh it here rather than only when a report is run.
        try:
            from .inventory import refresh
            refresh(self.conn)
        except Exception:
            pass
        self._drop_purged()
        if self.describe:
            self._describe_sessions()
        self.conn.commit()

        self.conn.execute(
            "INSERT INTO ingest_runs(started_at, finished_at, files_scanned,"
            " logs_ingested, metrics_ingested, spans_ingested, notes)"
            " VALUES (?,?,?,?,?,?,?)",
            (started, _now(), len(files), self.counts["logs"],
             self.counts["metrics"], self.counts["traces"],
             json.dumps({"skipped_lines": self.counts["skipped"]})),
        )
        db.set_meta(self.conn, "last_analyse_at", _now())
        self.conn.commit()
        return dict(self.counts, files=len(files))

    def _drop_purged(self) -> None:
        """Keep a purged session purged.

        The collector batches, so events for a session can land after it was
        deleted - which is how the self-test's synthetic sessions kept coming
        back into a real database, one per run. Anything belonging to a purged
        id is dropped again here rather than being allowed to rebuild it.
        """
        ids = [r["session_id"] for r in
               db.q(self.conn, "SELECT session_id FROM purged_sessions")]
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        self.conn.execute("PRAGMA defer_foreign_keys = ON")
        for table in db.SESSION_TABLES:
            self.conn.execute(
                f"DELETE FROM {table} WHERE session_id IN ({marks})", ids)
        self.conn.commit()

    def _describe_sessions(self) -> None:
        """Name each session from its prompts, and rename it as it grows.

        The only step of this pipeline that talks to anything off this
        machine. It lives in analyse rather than behind its own command
        because a database where half the rows are called `b944dba4` is not
        readable, and a name nobody remembered to generate is no name at all.

        Failure here is never allowed to fail an analyse: the telemetry is
        already normalised by this point and a missing sentence is a cosmetic
        loss. `telemetry config privacy --auto-describe off` turns it off.
        """
        try:
            from . import narrate
            todo = narrate.pending(self.conn, limit=narrate.AUTO_BATCH)
            if not todo:
                return
            self.progress(f"naming {len(todo)} session(s)…")
            stats = narrate.generate(self.conn, limit=narrate.AUTO_BATCH)
            self.counts["described"] = stats.get("written", 0)
        except Exception:
            self.counts["described"] = 0

    # -- file handling -------------------------------------------------------

    def _ingest_file(self, path: Path, signal: str) -> None:
        st = path.stat()
        row = self.conn.execute(
            "SELECT lines_consumed, bytes_consumed, inode FROM raw_files WHERE path=?",
            (str(path),)
        ).fetchone()
        start_line = row["lines_consumed"] if row else 0

        # The collector rotates its active file at a size threshold: the old
        # content is renamed and a fresh file takes the same path. A cursor
        # keyed on path alone would then skip the start of the new file, so
        # compare identity and size and rewind when either says "different
        # file". Re-reading is safe because every row has a semantic dedupe
        # key, so replaying a file cannot double count.
        if row is not None:
            rotated = (row["inode"] is None or row["inode"] != st.st_ino
                       or st.st_size < (row["bytes_consumed"] or 0))
            if rotated:
                start_line = 0

        consumed = start_line
        ingested = 0
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, start=1):
                if lineno <= start_line:
                    continue
                consumed = lineno
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    # Partially flushed final line: stop here and retry next run.
                    consumed = lineno - 1
                    self.counts["skipped"] += 1
                    break
                try:
                    ingested += self._dispatch(payload, signal, str(path), lineno)
                except Exception as exc:  # never let one bad record stop ingest
                    self.counts["skipped"] += 1
                    db.set_meta(self.conn, "last_ingest_error",
                                f"{path}:{lineno}: {type(exc).__name__}: {exc}")

        size = path.stat().st_size
        self.conn.execute(
            "INSERT INTO raw_files(path, signal, lines_consumed, bytes_consumed,"
            " inode, records_ingested, first_ingested_at, last_ingested_at)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(path) DO UPDATE SET lines_consumed=excluded.lines_consumed,"
            " bytes_consumed=excluded.bytes_consumed, inode=excluded.inode,"
            " records_ingested=raw_files.records_ingested+excluded.records_ingested,"
            " last_ingested_at=excluded.last_ingested_at",
            (str(path), signal, consumed, size, st.st_ino, ingested, _now(), _now()),
        )
        self.conn.commit()

    def _dispatch(self, payload: dict, signal: str, path: str, lineno: int) -> int:
        n = 0
        if signal == "logs":
            for i, rec in enumerate(otlp.iter_logs(payload)):
                self._handle_log(rec, path, lineno, i)
                n += 1
            self.counts["logs"] += n
        elif signal == "metrics":
            for i, pt in enumerate(otlp.iter_metric_points(payload)):
                self._handle_metric(pt, path, lineno, i)
                n += 1
            self.counts["metrics"] += n
        elif signal == "traces":
            for i, sp in enumerate(otlp.iter_spans(payload)):
                self._handle_span(sp, path, lineno, i)
                n += 1
            self.counts["traces"] += n
        return n

    # -- shared attribute handling ------------------------------------------

    STANDARD_KEYS = {
        "session.id", "user.id", "user.email", "user.account_uuid",
        "user.account_id", "organization.id", "app.version", "app.entrypoint",
        "terminal.type", "workspace.host_paths", "user.groups", "identity.source",
    }

    def _track_session(self, attrs: dict, resource: dict) -> str | None:
        sid = attrs.get("session.id") or resource.get("session.id")
        if not sid:
            return None
        cur = self.session_attrs.setdefault(sid, {"resource": {}})
        for k in self.STANDARD_KEYS:
            v = attrs.get(k, resource.get(k))
            if v not in (None, "") and cur.get(k) in (None, ""):
                cur[k] = v
        for k, v in resource.items():
            if k not in self.STANDARD_KEYS and k != "service.name":
                cur["resource"][k] = v
        return sid

    # -- logs ----------------------------------------------------------------

    def _handle_log(self, rec: dict, path: str, lineno: int, idx: int) -> None:
        attrs = rec["attributes"]
        resource = rec["resource"]
        body = rec["body"]

        name = attrs.get("event.name")
        if not name and isinstance(body, str):
            name = body[len(EVENT_PREFIX):] if body.startswith(EVENT_PREFIX) else body
        name = (name or "unknown").strip()

        sid = self._track_session(attrs, resource)
        ts = otlp.ns_to_iso(rec["ts_ns"]) or attrs.get("event.timestamp")
        seq = _as_int(attrs.get("event.sequence"))

        if sid and seq is not None:
            dk = f"ev|{sid}|{seq}|{name}"
        else:
            dk = f"ev|{path}|{lineno}|{idx}"

        safe_attrs = self._sanitise_event_attrs(name, attrs)

        event_id = db.insert_ignore(self.conn, "events", {
            "dedupe_key": dk,
            "session_id": sid,
            "event_name": name,
            "ts": ts,
            "ts_ns": rec["ts_ns"],
            "sequence": seq,
            "prompt_id": attrs.get("prompt.id"),
            "message_uuid": attrs.get("message.uuid"),
            "trace_id": rec.get("trace_id"),
            "span_id": rec.get("span_id"),
            "attrs_json": json.dumps(safe_attrs, default=str),
            "raw_json": json.dumps(rec["record"], default=str),
            "source_path": path,
            "source_line": lineno,
        })
        if event_id is None:
            return  # already ingested

        ctx = {"event_id": event_id, "session_id": sid, "ts": ts,
               "ts_ns": rec["ts_ns"], "dk": dk,
               "prompt_id": attrs.get("prompt.id")}

        handler = getattr(self, f"_ev_{name}", None)
        if handler:
            handler(attrs, ctx)

    def _sanitise_event_attrs(self, name: str, attrs: dict) -> dict:
        """Apply the content policy before anything is written to the DB."""
        out = {}
        for k, v in attrs.items():
            if k in ("prompt", "response", "user_prompt") and not config.STORE_CONTENT:
                out[k] = "[CONTENT NOT STORED]" if v not in (None, "") else v
                continue
            if k in ("body",) and not config.STORE_API_BODIES:
                out[k] = "[CONTENT NOT STORED]"
                continue
            if k in ("tool_parameters", "tool_input"):
                parsed = _maybe_json(v)
                filtered, dropped = filter_tool_params(parsed, config.STORE_TOOL_CONTENT)
                out[k] = filtered
                if dropped:
                    out[k + ".dropped_keys"] = dropped
                continue
            out[k] = scrub_deep(v)
        return out

    # -- individual event handlers ------------------------------------------

    _API_COMMON = {
        "model": "model", "request_id": "request_id",
        "client_request_id": "client_request_id", "speed": "speed",
        "query_source": "query_source", "effort": "effort",
        "agent.name": "agent_name", "skill.name": "skill_name",
        "plugin.name": "plugin_name", "marketplace.name": "marketplace_name",
        "mcp_server.name": "mcp_server_name", "mcp_tool.name": "mcp_tool_name",
    }

    def _api_row(self, attrs: dict, ctx: dict, outcome: str) -> dict:
        row = {
            "dedupe_key": ctx["dk"], "event_id": ctx["event_id"],
            "session_id": ctx["session_id"], "prompt_id": ctx["prompt_id"],
            "ts": ctx["ts"], "ts_ns": ctx["ts_ns"], "outcome": outcome,
            "duration_ms": _as_float(attrs.get("duration_ms")),
            "attempt": _as_int(attrs.get("attempt")),
            "status_code": (str(attrs["status_code"]) if attrs.get("status_code") is not None else None),
        }
        for src, dest in self._API_COMMON.items():
            row[dest] = attrs.get(src)
        return row

    def _ev_api_request(self, attrs: dict, ctx: dict) -> None:
        row = self._api_row(attrs, ctx, "ok")
        row.update({
            "cost_usd": _as_float(attrs.get("cost_usd")),
            "cost_usd_micros": _as_int(attrs.get("cost_usd_micros")),
            "input_tokens": _as_int(attrs.get("input_tokens")),
            "output_tokens": _as_int(attrs.get("output_tokens")),
            "cache_read_tokens": _as_int(attrs.get("cache_read_tokens")),
            "cache_creation_tokens": _as_int(attrs.get("cache_creation_tokens")),
        })
        if row["cost_usd"] is None and row["cost_usd_micros"] is not None:
            row["cost_usd"] = row["cost_usd_micros"] / 1_000_000.0
        db.insert_ignore(self.conn, "api_calls", row)
        self._attribute_skill(attrs, ctx, source="api_attribution")

    def _ev_api_error(self, attrs: dict, ctx: dict) -> None:
        row = self._api_row(attrs, ctx, "error")
        row["error"] = scrub(attrs.get("error"))
        db.insert_ignore(self.conn, "api_calls", row)
        db.insert_ignore(self.conn, "errors", {
            "dedupe_key": ctx["dk"], "session_id": ctx["session_id"],
            "ts": ctx["ts"], "kind": "api_error", "source_event": "api_error",
            "model": attrs.get("model"), "status_code": row["status_code"],
            "message": row["error"],
        })

    def _ev_api_refusal(self, attrs: dict, ctx: dict) -> None:
        row = self._api_row(attrs, ctx, "refusal")
        row["refusal_category"] = attrs.get("category")
        db.insert_ignore(self.conn, "api_calls", row)
        db.insert_ignore(self.conn, "errors", {
            "dedupe_key": ctx["dk"], "session_id": ctx["session_id"],
            "ts": ctx["ts"], "kind": "api_refusal", "source_event": "api_refusal",
            "model": attrs.get("model"), "error_name": attrs.get("category"),
        })

    def _ev_user_prompt(self, attrs: dict, ctx: dict) -> None:
        text = attrs.get("prompt") if config.STORE_CONTENT else None
        db.insert_ignore(self.conn, "prompts", {
            "dedupe_key": ctx["dk"], "session_id": ctx["session_id"],
            "prompt_id": ctx["prompt_id"], "message_uuid": attrs.get("message.uuid"),
            "ts": ctx["ts"], "prompt_length": _as_int(attrs.get("prompt_length")),
            "command_name": attrs.get("command_name"),
            "command_source": attrs.get("command_source"),
            "prompt_text": scrub(text) if text and text != "<REDACTED>" else None,
        })

    def _ev_assistant_response(self, attrs: dict, ctx: dict) -> None:
        text = attrs.get("response") if config.STORE_CONTENT else None
        db.insert_ignore(self.conn, "responses", {
            "dedupe_key": ctx["dk"], "session_id": ctx["session_id"],
            "prompt_id": ctx["prompt_id"], "message_uuid": attrs.get("message.uuid"),
            "ts": ctx["ts"], "response_length": _as_int(attrs.get("response_length")),
            "model": attrs.get("model"), "request_id": attrs.get("request_id"),
            "query_source": attrs.get("query_source"),
            "response_text": scrub(text) if text and text != "<REDACTED>" else None,
        })

    def _ev_tool_result(self, attrs: dict, ctx: dict) -> None:
        self._tool_row(attrs, ctx, origin="tool_result")

    def _ev_tool_decision(self, attrs: dict, ctx: dict) -> None:
        self._tool_row(attrs, ctx, origin="tool_decision")

    def _ev_internal_error(self, attrs: dict, ctx: dict) -> None:
        db.insert_ignore(self.conn, "errors", {
            "dedupe_key": ctx["dk"], "session_id": ctx["session_id"],
            "ts": ctx["ts"], "kind": "internal_error",
            "source_event": "internal_error",
            "error_name": attrs.get("error_name"),
            "error_code": attrs.get("error_code"),
        })

    def _ev_compaction(self, attrs: dict, ctx: dict) -> None:
        """Undocumented event. A failed compaction burns tokens and loses
        context, so surface it alongside the other errors."""
        if _as_bool_int(attrs.get("success")) == 0:
            db.insert_ignore(self.conn, "errors", {
                "dedupe_key": ctx["dk"], "session_id": ctx["session_id"],
                "ts": ctx["ts"], "kind": "compaction_failed",
                "source_event": "compaction",
                "error_name": attrs.get("trigger"),
                "message": scrub(attrs.get("error")),
            })

    def _ev_mcp_server_connection(self, attrs: dict, ctx: dict) -> None:
        if str(attrs.get("status")) == "failed":
            db.insert_ignore(self.conn, "errors", {
                "dedupe_key": ctx["dk"], "session_id": ctx["session_id"],
                "ts": ctx["ts"], "kind": "mcp_connection",
                "source_event": "mcp_server_connection",
                "error_code": attrs.get("error_code"),
                "message": scrub(attrs.get("error")),
            })

    # -- tool calls ----------------------------------------------------------

    def _tool_row(self, attrs: dict, ctx: dict, origin: str) -> None:
        sid = ctx["session_id"]
        tool_use_id = attrs.get("tool_use_id")
        merge_key = f"tc|{sid}|{tool_use_id}" if tool_use_id else f"tc|{ctx['dk']}"

        tool_name = attrs.get("tool_name")
        params = _maybe_json(attrs.get("tool_parameters")) or {}
        tinput = _maybe_json(attrs.get("tool_input")) or {}
        merged_params = {**tinput, **params}
        filtered, dropped = filter_tool_params(merged_params, config.STORE_TOOL_CONTENT)
        filtered = filtered or {}

        mserver, mtool = mcp_parts(tool_name)
        file_path = (filtered.get("file_path") or filtered.get("filePath")
                     or filtered.get("path") or filtered.get("notebook_path"))
        # Observed in 2.1.237: `bash_command` holds only the program name
        # ("wc"), while `full_command` holds the whole command line. Prefer
        # the fullest form available.
        command = (filtered.get("full_command")
                   or (filtered.get("command") if tool_name in SHELL_TOOLS else None)
                   or filtered.get("bash_command"))
        command = scrub(command) if isinstance(command, str) else None

        row = {
            "merge_key": merge_key, "session_id": sid, "prompt_id": ctx["prompt_id"],
            "ts": ctx["ts"], "ts_ns": ctx["ts_ns"], "tool_use_id": tool_use_id,
            "tool_name": tool_name, "tool_source": attrs.get("tool_source"),
            "tool_category": tool_category(tool_name),
            "success": _as_bool_int(attrs.get("success")),
            "duration_ms": _as_float(attrs.get("duration_ms")),
            "error_type": attrs.get("error_type"),
            "error_message": scrub(attrs.get("error")),
            "decision": attrs.get("decision") or attrs.get("decision_type"),
            "decision_source": attrs.get("source") or attrs.get("decision_source"),
            "tool_input_size_bytes": _as_int(attrs.get("tool_input_size_bytes")),
            "tool_result_size_bytes": _as_int(attrs.get("tool_result_size_bytes")),
            "mcp_server_name": filtered.get("mcp_server_name") or mserver,
            "mcp_tool_name": filtered.get("mcp_tool_name") or mtool,
            "mcp_server_scope": attrs.get("mcp_server_scope"),
            "skill_name": filtered.get("skill_name") or filtered.get("skill") or attrs.get("skill.name"),
            "subagent_type": filtered.get("subagent_type") or filtered.get("agent_type"),
            "file_path": file_path if isinstance(file_path, str) else None,
            "bash_command": command,
            "params_json": json.dumps(filtered, default=str) if filtered else None,
            "dropped_param_keys": json.dumps(sorted(set(dropped))) if dropped else None,
            "sources": origin,
        }
        db.upsert_merge(self.conn, "tool_calls", "merge_key", row)
        # Record which signals contributed, without losing earlier ones.
        self.conn.execute(
            "UPDATE tool_calls SET sources = CASE WHEN sources LIKE ? THEN sources"
            " ELSE sources || '+' || ? END WHERE merge_key=?",
            (f"%{origin}%", origin, merge_key),
        )

        if row["success"] is False or row["success"] == 0:
            db.insert_ignore(self.conn, "errors", {
                "dedupe_key": f"toolfail|{merge_key}", "session_id": sid,
                "ts": ctx["ts"], "kind": "tool_failure", "source_event": origin,
                "tool_name": tool_name, "error_name": row["error_type"],
                "message": row["error_message"],
            })

        self._derive_activity(row, ctx)

    def _derive_activity(self, row: dict, ctx: dict) -> None:
        sid, tuid = row["session_id"], row["tool_use_id"]
        key = row["merge_key"]
        tool_name = row["tool_name"]

        op = FILE_TOOLS.get(tool_name or "")
        if op and row["file_path"]:
            p = row["file_path"]
            db.upsert_merge(self.conn, "file_activity", "merge_key", {
                "merge_key": f"fa|{key}", "session_id": sid, "ts": row["ts"],
                "tool_use_id": tuid, "tool_name": tool_name, "operation": op,
                "path": p, "file_ext": Path(p).suffix.lower() or None,
                "success": row["success"], "via": "tool", "op_confidence": "high",
            })


        if tool_name in SHELL_TOOLS and row["bash_command"]:
            cmd = row["bash_command"]
            progs = bash_programs(cmd)
            db.upsert_merge(self.conn, "bash_activity", "merge_key", {
                "merge_key": f"ba|{key}", "session_id": sid, "ts": row["ts"],
                "tool_use_id": tuid, "command": cmd, "command_hash": hash_text(cmd),
                "program": (primary_program(progs) or "")[:64] or None,
                "programs": json.dumps(progs) if progs else None,
                "success": row["success"], "duration_ms": row["duration_ms"],
                "error_type": row["error_type"],
            })

        if row["skill_name"]:
            db.upsert_merge(self.conn, "skill_calls", "merge_key", {
                "merge_key": f"sk|{key}", "session_id": sid, "ts": row["ts"],
                "skill_name": row["skill_name"], "invocation_source": "tool_call",
                "tool_use_id": tuid, "success": row["success"],
                "duration_ms": row["duration_ms"],
            })

        if row["subagent_type"] or tool_name in SUBAGENT_TOOLS:
            db.upsert_merge(self.conn, "subagent_activity", "merge_key", {
                "merge_key": f"sa|{key}", "session_id": sid, "ts": row["ts"],
                "subagent_type": row["subagent_type"], "tool_use_id": tuid,
                "success": row["success"], "duration_ms": row["duration_ms"],
                "source": "tool_call",
            })

    def _derive_shell_files(self) -> None:
        """Re-parse every stored bash command for file access.

        Deliberately a rebuild rather than an incremental step: the parser is
        a heuristic that will keep improving, and rebuilding from the stored
        commands means those improvements apply to history without re-reading
        the raw archive. Tool-derived rows are untouched.
        """
        self.conn.execute("DELETE FROM file_activity WHERE via='shell'")
        rows = db.q(self.conn, """
            SELECT b.bash_activity_id, b.session_id, b.ts, b.tool_use_id,
                   b.command, b.success,
                   COALESCE(s.cwd, g.repo_root, g.cwd) AS base
              FROM bash_activity b
              LEFT JOIN sessions s ON s.session_id = b.session_id
              LEFT JOIN local_session_git_context g ON g.session_id = b.session_id
             WHERE b.command IS NOT NULL""")
        for r in rows:
            base = shellfiles.base_dir(r["command"]) or r["base"]
            for i, (path, op, conf) in enumerate(shellfiles.parse(r["command"])):
                # `~` is the user's home, not a directory to append to cwd:
                # joining it produced paths like `/repo/~/Workspace/...`.
                if path.startswith("~"):
                    resolved = str(Path(path).expanduser())
                elif not path.startswith("/") and base:
                    resolved = str(Path(base) / path)
                else:
                    resolved = path
                db.insert_ignore(self.conn, "file_activity", {
                    "merge_key": f"fa|sh|{r['bash_activity_id']}|{i}",
                    "session_id": r["session_id"], "ts": r["ts"],
                    "tool_use_id": r["tool_use_id"], "tool_name": "Bash",
                    "operation": op, "path": resolved,
                    "file_ext": Path(resolved).suffix.lower() or None,
                    "success": r["success"], "via": "shell",
                    "op_confidence": conf,
                })
        self.conn.commit()

    def _attribute_skill(self, attrs: dict, ctx: dict, source: str) -> None:
        """Skill attribution that arrives on API events rather than tool events."""
        name = attrs.get("skill.name")
        if not name or name == "custom":
            return
        db.upsert_merge(self.conn, "skill_calls", "merge_key", {
            "merge_key": f"sk|api|{ctx['dk']}", "session_id": ctx["session_id"],
            "ts": ctx["ts"], "skill_name": name, "invocation_source": source,
        })
        if attrs.get("agent.name"):
            db.upsert_merge(self.conn, "subagent_activity", "merge_key", {
                "merge_key": f"sa|api|{ctx['dk']}", "session_id": ctx["session_id"],
                "ts": ctx["ts"], "agent_name": attrs.get("agent.name"),
                "source": source,
            })

    # -- metrics -------------------------------------------------------------

    def _handle_metric(self, pt: dict, path: str, lineno: int, idx: int) -> None:
        attrs = pt["attributes"]
        sid = self._track_session(attrs, pt["resource"])
        ts = otlp.ns_to_iso(pt["ts_ns"])
        dk = f"mp|{pt['metric_name']}|{pt['ts_ns']}|{_attr_hash(attrs)}"

        db.insert_ignore(self.conn, "metric_points", {
            "dedupe_key": dk, "metric_name": pt["metric_name"], "kind": pt["kind"],
            "unit": pt["unit"] or None, "value": pt["value"], "session_id": sid, "ts": ts,
            "ts_ns": pt["ts_ns"],
            "attrs_json": json.dumps(scrub_deep(attrs), default=str),
            "raw_json": json.dumps(pt["point"], default=str),
            "source_path": path, "source_line": lineno,
        })
        if pt["metric_name"] == "claude_code.session.count" and sid:
            st = attrs.get("start_type")
            if st:
                self.session_attrs.setdefault(sid, {"resource": {}})["start_type"] = st

    # -- spans ---------------------------------------------------------------

    def _handle_span(self, sp: dict, path: str, lineno: int, idx: int) -> None:
        attrs = sp["attributes"]
        sid = self._track_session(attrs, sp["resource"])
        tuid = attrs.get("tool_use_id") or attrs.get("gen_ai.tool.call.id")
        dk = f"sp|{sp['trace_id']}|{sp['span_id']}"

        safe = {}
        for k, v in attrs.items():
            if k in ("user_prompt", "system_prompt_preview", "user_system_prompt",
                     "response.model_output", "tool_input") and not config.STORE_CONTENT:
                safe[k] = "[CONTENT NOT STORED]" if v not in (None, "") else v
            else:
                safe[k] = scrub_deep(v)

        # Span events carry tool input/output bodies when OTEL_LOG_TOOL_CONTENT
        # is on. Keep names and sizes always; keep bodies only when allowed.
        span_events = None
        raw_events = sp["span"].get("events") or []
        if raw_events:
            collected = []
            for ev in raw_events:
                ev_attrs = otlp.attrs(ev.get("attributes"))
                if config.STORE_SPAN_EVENTS:
                    payload = scrub_deep(ev_attrs)
                else:
                    payload = {k: (f"<{len(str(v))} chars not stored>"
                                   if isinstance(v, str) and len(str(v)) > 64
                                   else scrub_deep(v))
                               for k, v in ev_attrs.items()}
                collected.append({"name": ev.get("name"),
                                  "time": otlp.ns_to_iso(ev.get("timeUnixNano")),
                                  "attributes": payload})
            span_events = json.dumps(collected, default=str)

        inserted = db.insert_ignore(self.conn, "spans", {
            "dedupe_key": dk, "trace_id": sp["trace_id"], "span_id": sp["span_id"],
            "parent_span_id": sp["parent_span_id"], "name": sp["name"],
            "session_id": sid, "start_ts": otlp.ns_to_iso(sp["start_ns"]),
            "end_ts": otlp.ns_to_iso(sp["end_ns"]), "start_ns": sp["start_ns"],
            "duration_ms": sp["duration_ms"], "status_code": str(sp["status"]) if sp["status"] else None,
            "tool_use_id": tuid,
            "attrs_json": json.dumps(safe, default=str),
            "span_events": span_events,
            "raw_json": json.dumps(sp["span"], default=str),
            "source_path": path, "source_line": lineno,
        })
        if inserted is None:
            return

        # Spans carry file_path / full_command / skill_name / subagent_type
        # directly (gated by OTEL_LOG_TOOL_DETAILS), so use them to enrich the
        # tool_calls row that the events pipeline created.
        if sp["name"] in ("claude_code.tool", "claude_code.tool.execution") and sid and tuid:
            ts = otlp.ns_to_iso(sp["start_ns"])
            merge_key = f"tc|{sid}|{tuid}"
            tool_name = attrs.get("tool_name")
            cmd = scrub(attrs.get("full_command")) if attrs.get("full_command") else None
            row = {
                "merge_key": merge_key, "session_id": sid, "ts": ts,
                "ts_ns": sp["start_ns"], "tool_use_id": tuid,
                "tool_name": tool_name,
                "tool_category": tool_category(tool_name) if tool_name else None,
                "duration_ms": sp["duration_ms"],
                "result_tokens": _as_int(attrs.get("result_tokens")),
                "success": _as_bool_int(attrs.get("success")),
                "file_path": attrs.get("file_path"),
                "bash_command": cmd,
                "skill_name": attrs.get("skill_name"),
                "subagent_type": attrs.get("subagent_type"),
                "agent_id": attrs.get("agent_id"),
                "parent_agent_id": attrs.get("parent_agent_id"),
                "workflow_run_id": attrs.get("workflow.run_id"),
                "sources": "span",
            }
            db.upsert_merge(self.conn, "tool_calls", "merge_key", row)
            self.conn.execute(
                "UPDATE tool_calls SET sources = CASE WHEN sources LIKE '%span%'"
                " THEN sources ELSE sources || '+span' END WHERE merge_key=?",
                (merge_key,),
            )
            full = self.conn.execute(
                "SELECT * FROM tool_calls WHERE merge_key=?", (merge_key,)
            ).fetchone()
            if full:
                self._derive_activity(dict(full), {"session_id": sid, "ts": ts})

    # -- session / project resolution ---------------------------------------

    def _import_session_context(self) -> None:
        """Load git context recorded by the optional SessionStart/End hooks."""
        path = config.SESSION_CONTEXT_FILE
        if not path.exists():
            return
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                sid = r.get("session_id")
                if not sid:
                    continue
                self.conn.execute(
                    "INSERT OR REPLACE INTO local_session_git_context"
                    "(session_id, phase, captured_at, cwd, repo_root, remote_url,"
                    " branch, head_sha, is_dirty) VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, r.get("phase", "start"), r.get("captured_at"),
                     r.get("cwd"), r.get("repo_root"), r.get("remote_url"),
                     r.get("branch"), r.get("head_sha"), r.get("is_dirty")))
                self.conn.execute(
                    "INSERT OR IGNORE INTO sessions(session_id, cwd) VALUES (?,?)",
                    (sid, r.get("cwd")))
                self.conn.execute(
                    "UPDATE sessions SET cwd=COALESCE(cwd,?) WHERE session_id=?",
                    (r.get("cwd"), sid))
        self.conn.commit()

    def _flush_sessions(self) -> None:
        for sid, a in self.session_attrs.items():
            paths = a.get("workspace.host_paths")
            if isinstance(paths, str):
                paths = [paths]
            db.upsert_merge(self.conn, "sessions", "session_id", {
                "session_id": sid,
                "start_type": a.get("start_type"),
                "user_id": a.get("user.id"),
                "user_email": a.get("user.email"),
                "account_uuid": a.get("user.account_uuid"),
                "account_id": a.get("user.account_id"),
                "organization_id": a.get("organization.id"),
                "app_version": a.get("app.version"),
                "app_entrypoint": a.get("app.entrypoint"),
                "terminal_type": a.get("terminal.type"),
                "workspace_paths": json.dumps(paths) if paths else None,
                "resource_attrs": json.dumps(a.get("resource")) if a.get("resource") else None,
            })
        # Sessions that only ever appeared on metrics/spans still need a row.
        self.conn.execute(
            "INSERT OR IGNORE INTO sessions(session_id)"
            " SELECT DISTINCT session_id FROM events WHERE session_id IS NOT NULL")
        self.conn.execute(
            "INSERT OR IGNORE INTO sessions(session_id)"
            " SELECT DISTINCT session_id FROM metric_points WHERE session_id IS NOT NULL")
        self.conn.commit()

    def _ensure_project(self, desc: dict, method: str) -> str:
        db.upsert_merge(self.conn, "projects", "project_id", {
            "project_id": desc["project_id"],
            "project_name": desc["project_name"],
            "repo_root": desc.get("repo_root"),
            "remote_url": desc.get("remote_url"),
            "remote_normalized": desc.get("remote_normalized"),
            "is_git": desc.get("is_git", 0),
            "detection_method": method,
            "first_seen": _now(), "last_seen": _now(),
        })
        self.conn.execute("UPDATE projects SET last_seen=? WHERE project_id=?",
                          (_now(), desc["project_id"]))
        return desc["project_id"]

    def _resolve_projects(self) -> None:
        """Attach every session to a project, best source first."""
        self._ensure_project(dict(gitctx.UNKNOWN_PROJECT), "none")

        rows = db.q(self.conn,
                    "SELECT session_id, workspace_paths, cwd, project_id FROM sessions")
        for r in rows:
            if r["project_id"]:
                continue
            candidate, method = None, None

            hook = self.conn.execute(
                "SELECT repo_root, remote_url, cwd FROM local_session_git_context"
                " WHERE session_id=? ORDER BY phase='start' DESC LIMIT 1",
                (r["session_id"],)).fetchone()
            if hook and (hook["repo_root"] or hook["cwd"]):
                candidate = hook["repo_root"] or hook["cwd"]
                method = "session_hook"

            if not candidate and r["cwd"]:
                candidate, method = r["cwd"], "session_cwd"

            if not candidate and r["workspace_paths"]:
                try:
                    paths = json.loads(r["workspace_paths"])
                except ValueError:
                    paths = []
                if paths:
                    candidate, method = paths[0], "workspace.host_paths"

            if not candidate:
                # Claude Code 2.1.237 does not actually emit
                # workspace.host_paths, so fall back to inferring the
                # repository from absolute paths seen in tool arguments and
                # bash commands.
                root = self._infer_repo_root(r["session_id"])
                if root:
                    candidate, method = root, "path_inference"

            if candidate:
                # Records what the checkout was sitting on, so it wants the
                # live state, not just the project.
                desc = gitctx.describe_full(candidate)
                pid = self._ensure_project(desc, method)
                self.conn.execute(
                    "UPDATE sessions SET project_id=?, cwd=COALESCE(cwd,?),"
                    " project_detection_method=? WHERE session_id=?",
                    (pid, candidate, method, r["session_id"]))
                if desc.get("is_git"):
                    self.conn.execute(
                        "INSERT OR REPLACE INTO local_session_git_context"
                        "(session_id, phase, captured_at, cwd, repo_root, remote_url,"
                        " branch, head_sha, is_dirty) VALUES (?,?,?,?,?,?,?,?,?)",
                        (r["session_id"], "observed", _now(), candidate,
                         desc.get("repo_root"), desc.get("remote_url"),
                         desc.get("branch"), desc.get("head_sha"), desc.get("is_dirty")))
            else:
                self.conn.execute(
                    "UPDATE sessions SET project_id=?, project_detection_method='none'"
                    " WHERE session_id=?",
                    (gitctx.UNKNOWN_PROJECT["project_id"], r["session_id"]))
        self.conn.commit()

    _ABS_PATH_RE = re.compile(r"/(?:[A-Za-z0-9._+@-]+/)*[A-Za-z0-9._+@-]+")

    def _candidate_paths(self, session_id: str) -> list[str]:
        """Absolute paths this session touched, from any signal."""
        out: list[str] = []
        for sql, col in (
            ("SELECT path FROM file_activity WHERE session_id=? AND path LIKE '/%'", 0),
            ("SELECT file_path FROM tool_calls WHERE session_id=? AND file_path LIKE '/%'", 0),
        ):
            out.extend(r[col] for r in self.conn.execute(sql, (session_id,)))
        for r in self.conn.execute(
                "SELECT command FROM bash_activity WHERE session_id=? AND command IS NOT NULL",
                (session_id,)):
            out.extend(m.group(0) for m in self._ABS_PATH_RE.finditer(r[0]))
        return out

    def _infer_repo_root(self, session_id: str) -> str | None:
        """Most frequently referenced git repository for a session."""
        counts: collections.Counter = collections.Counter()
        for path in self._candidate_paths(session_id)[:200]:
            if len(path) < 5:
                continue
            desc = self._project_of(path)
            if desc.get("is_git") and desc.get("repo_root"):
                counts[desc["repo_root"]] += 1
        if not counts:
            return None
        return counts.most_common(1)[0][0]

    def _apply_file_ignores(self) -> None:
        """Drop file activity that matches an ignore pattern.

        Applied here rather than at write time so that changing the patterns
        takes effect on the next analyse, whatever produced the row - tool
        events are not re-derivable without re-reading the raw archive.
        """
        patterns = config.load_ignores()
        if not patterns:
            return
        paths = [r["path"] for r in
                 db.q(self.conn, "SELECT DISTINCT path FROM file_activity"
                                 " WHERE path IS NOT NULL")]
        doomed = [p for p in paths
                  if any(fnmatch.fnmatch(p, pat) for pat in patterns)]
        for chunk_start in range(0, len(doomed), 400):
            chunk = doomed[chunk_start:chunk_start + 400]
            marks = ",".join("?" for _ in chunk)
            self.conn.execute(
                f"DELETE FROM file_activity WHERE path IN ({marks})", chunk)
        if doomed:
            db.set_meta(self.conn, "ignored_paths", str(len(doomed)))
        self.conn.commit()

    def _propagate_project_ids(self) -> None:
        tables = ["events", "metric_points", "spans", "api_calls", "tool_calls",
                  "skill_calls", "file_activity", "bash_activity",
                  "subagent_activity", "prompts", "responses", "errors"]
        for t in tables:
            self.conn.execute(
                f"UPDATE {t} SET project_id = ("
                f" SELECT s.project_id FROM sessions s WHERE s.session_id = {t}.session_id"
                f") WHERE project_id IS NULL AND session_id IS NOT NULL")

        # A file can live outside the session's project; prefer its own repo.
        rows = db.q(self.conn,
                    "SELECT DISTINCT path FROM file_activity WHERE path IS NOT NULL"
                    " AND repo_relative_path IS NULL")
        for r in rows:
            desc = self._project_of(r["path"])
            if not desc.get("is_git"):
                continue
            self._ensure_project(desc, "file_path")
            root = desc["repo_root"]
            try:
                rel = str(Path(r["path"]).resolve().relative_to(Path(root).resolve()))
            except (ValueError, OSError):
                rel = None
            self.conn.execute(
                "UPDATE file_activity SET project_id=?, repo_relative_path=?"
                " WHERE path=?", (desc["project_id"], rel, r["path"]))
        self.conn.commit()

    def _finalise_sessions(self) -> None:
        self.conn.execute("""
            UPDATE sessions SET
              first_seen = (SELECT MIN(ts) FROM events e WHERE e.session_id = sessions.session_id AND e.ts IS NOT NULL),
              last_seen  = (SELECT MAX(ts) FROM events e WHERE e.session_id = sessions.session_id AND e.ts IS NOT NULL)
            WHERE EXISTS (SELECT 1 FROM events e WHERE e.session_id = sessions.session_id AND e.ts IS NOT NULL)
        """)
        # Fall back to metric timestamps for metrics-only sessions.
        self.conn.execute("""
            UPDATE sessions SET
              first_seen = COALESCE(first_seen, (SELECT MIN(ts) FROM metric_points m WHERE m.session_id = sessions.session_id)),
              last_seen  = COALESCE(last_seen,  (SELECT MAX(ts) FROM metric_points m WHERE m.session_id = sessions.session_id))
        """)
        self.conn.execute("""
            UPDATE sessions
               SET duration_s = (julianday(last_seen) - julianday(first_seen)) * 86400.0
             WHERE first_seen IS NOT NULL AND last_seen IS NOT NULL
        """)
        self.conn.commit()

    # -- derived: created vs modified ---------------------------------------

    def _derive_file_creation(self) -> None:
        """Classify write operations as create vs modify.

        Method (documented in README):
          1. git evidence - if the file's first `A` (added) commit in the
             repository is at or after the session start, treat as created
             (confidence: high).
          2. absence of a prior read - a Write with no earlier Read/Edit of the
             same path anywhere in the database before that moment
             (confidence: medium).
          3. otherwise unknown.
        """
        rows = db.q(self.conn, """
            SELECT f.file_activity_id, f.path, f.ts, f.session_id, s.first_seen
              FROM file_activity f
              LEFT JOIN sessions s ON s.session_id = f.session_id
             WHERE f.operation IN ('write','notebook_edit') AND f.created IS NULL
        """)
        cache: dict[str, str | None] = {}
        for r in rows:
            path, ts = r["path"], r["ts"]
            created, method, conf = None, "unknown", "none"

            desc = self._project_of(path)
            root = desc.get("repo_root")
            if root:
                if path not in cache:
                    cache[path] = self._first_added(root, path)
                added_at = cache[path]
                session_start = r["first_seen"] or ts
                if added_at and session_start and added_at >= session_start[:19]:
                    created, method, conf = 1, "git_added", "high"
                elif added_at:
                    created, method, conf = 0, "git_added", "high"

            if created is None:
                prior = db.scalar(self.conn, """
                    SELECT COUNT(*) FROM file_activity
                     WHERE path = ? AND ts < ? AND operation IN ('read','edit')
                """, (path, ts or ""), default=0)
                created = 1 if prior == 0 else 0
                method, conf = "no_prior_read", "medium"

            self.conn.execute(
                "UPDATE file_activity SET created=?, create_method=?, create_confidence=?"
                " WHERE file_activity_id=?", (created, method, conf, r["file_activity_id"]))
        self.conn.commit()

    _DIR_CACHE_TTL_S = 86400.0

    def _project_of(self, path: str) -> dict:
        """`gitctx.describe`, with the answer kept in the database.

        Attribution asks the same question of the same directories on every
        run, and each miss is a `git` subprocess. Positive answers are kept
        for good; "not a repository" is re-checked after a day, since a
        directory can be `git init`ed later.
        """
        directory = gitctx.nearest_dir(path)
        if directory is None:
            return dict(gitctx.UNKNOWN_PROJECT, cwd=path)
        row = self.conn.execute(
            "SELECT is_git, repo_root, remote_url, checked_at FROM git_dir_cache"
            " WHERE dir=?", (directory,)).fetchone()
        if row is not None and (row["is_git"]
                                or self._fresh(row["checked_at"], self._DIR_CACHE_TTL_S)):
            root, remote = row["repo_root"], row["remote_url"]
            return {
                "project_id": gitctx.project_id(root, remote, directory),
                "project_name": gitctx.project_name(root, remote, directory),
                "repo_root": root, "remote_url": remote,
                "remote_normalized": gitctx.normalize_remote(remote),
                "is_git": 1 if row["is_git"] else 0, "cwd": directory,
            }
        desc = gitctx.describe(path)
        self.conn.execute(
            "INSERT OR REPLACE INTO git_dir_cache"
            "(dir, is_git, repo_root, remote_url, checked_at) VALUES (?,?,?,?,?)",
            (directory, desc.get("is_git", 0), desc.get("repo_root"),
             desc.get("remote_url"), _now()))
        return desc

    @staticmethod
    def _fresh(checked_at: str | None, ttl_s: float) -> bool:
        if not checked_at:
            return False
        try:
            when = _dt.datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
        except ValueError:
            return False
        return (_dt.datetime.now(_dt.timezone.utc) - when).total_seconds() < ttl_s

    _ADDED_CACHE_TTL_S = 6 * 3600.0

    def _first_added(self, root: str, path: str) -> str | None:
        """When git first saw this path, through a cache.

        The subprocess behind this is the slowest thing in an incremental
        analyse, and shell-derived rows are rebuilt every run, so the same
        paths would be probed over and over. A found commit is cached for
        good; "not committed yet" is re-checked after a few hours.
        """
        hit = self.conn.execute(
            "SELECT added_at, checked_at FROM git_first_added"
            " WHERE repo_root=? AND path=?", (root, path)).fetchone()
        if hit and (hit["added_at"]
                    or self._fresh(hit["checked_at"], self._ADDED_CACHE_TTL_S)):
            return hit["added_at"]
        added_at = gitctx.file_first_added(root, path)
        self.conn.execute(
            "INSERT OR REPLACE INTO git_first_added"
            "(repo_root, path, added_at, checked_at) VALUES (?,?,?,?)",
            (root, path, added_at, _now()))
        return added_at

    # -- derived: git reconciliation ----------------------------------------

    # -- derived: work streams ----------------------------------------------

    _CONVENTIONAL = re.compile(r"^(?P<type>[a-zA-Z]+)"
                               r"(?:\((?P<scope>[^)]+)\))?"
                               r"(?P<breaking>!)?:\s*(?P<subject>.*)$")
    _REVERT = re.compile(r'^Revert\s+"(?P<subject>.*)"\s*$')

    def _classify_commits(self) -> None:
        """Parse conventional-commit type and scope out of commit subjects.

        These used to group commits into work streams spanning sessions. The
        session is the unit now, so the parsed values are kept as what they
        are - a label on a commit, and the fallback name for a session that
        has no description yet.
        """
        rows = db.q(self.conn, "SELECT commit_sha, subject FROM git_activity")
        for r in rows:
            subject = (r["subject"] or "").strip()
            m = self._CONVENTIONAL.match(subject)
            ctype = m.group("type").lower() if m else None
            scope = (m.group("scope").strip().lower() if m and m.group("scope")
                     else None)
            self.conn.execute(
                "UPDATE git_activity SET commit_type=?, commit_scope=?"
                " WHERE commit_sha=?", (ctype, scope, r["commit_sha"]))
        self._detect_reverts()
        self.conn.commit()

    def _detect_reverts(self) -> None:
        """A revert is the clearest evidence that work had to be undone."""
        by_subject = {}
        for r in db.q(self.conn, "SELECT commit_sha, subject, project_id FROM git_activity"):
            by_subject.setdefault((r["project_id"], (r["subject"] or "").strip()),
                                  r["commit_sha"])
        for r in db.q(self.conn, "SELECT commit_sha, subject, project_id,"
                                 " committed_at FROM git_activity"):
            m = self._REVERT.match((r["subject"] or "").strip())
            if not m:
                continue
            target = by_subject.get((r["project_id"], m.group("subject").strip()))
            db.insert_ignore(self.conn, "reverts", {
                "revert_sha": r["commit_sha"], "project_id": r["project_id"],
                "reverted_sha": target, "detected_at": r["committed_at"],
                "method": "commit_subject",
            })

    # -- derived: human effort per turn --------------------------------------

    # Conservative correction cues. Matched against the start of a prompt or
    # as a whole phrase, because "no" inside a sentence means nothing. These
    # are heuristics and are recorded with the cue that fired so a human can
    # audit any number built on them.
    _CORRECTION_CUES = [
        "no,", "no ", "nope", "not quite", "not right", "that's wrong",
        "thats wrong", "doesn't work", "doesnt work", "didn't work",
        "didnt work", "still not", "still broken", "try again", "revert",
        "undo", "instead", "actually", "i meant", "wrong ", "fix that",
        "that broke", "go back", "not what i", "re-review", "review again",
    ]
    _STEERING_MAX_CHARS = 45

    # Prompts the harness injects rather than the human typing: monitor
    # notifications, system reminders, slash-command echoes. They cost money
    # and belong in the cost figures, but counting them as human turns
    # overstates effort and skews the steering share.
    _SYSTEM_PROMPT_RE = re.compile(
        r"^\s*<(task-notification|system-reminder|local-command-[a-z]+|"
        r"command-name|command-message|command-args)\b", re.I)

    def _classify_prompt(self, text: str | None, length: int | None,
                         command_name: str | None = None) -> tuple[int, int, str | None]:
        """Return (is_steering, is_correction, cue).

        The two labels are mutually exclusive and correction wins, so the
        counts, the wall-clock totals and the strip colours all agree. A slash
        command (`/exit`, `/compact`) is a dispatch, not a typed instruction,
        so it is never a steering nudge.
        """
        short = (length is not None and length <= self._STEERING_MAX_CHARS
                 and not command_name)
        if text:
            low = text.strip().lower()
            for cue in self._CORRECTION_CUES:
                if low.startswith(cue) or f" {cue}" in low[:160]:
                    return 0, 1, cue.strip()
        return (1 if short else 0), 0, None

    def _derive_turns(self) -> None:
        """Build one row per prompt.id with the work it caused.

        prompt.id is stamped on every event a turn produces, which makes it
        the natural join for "what did this one human instruction cost".
        """
        self.conn.execute("DELETE FROM turns")
        prompts = db.q(self.conn, """
            SELECT p.prompt_id, p.session_id, p.project_id, p.ts, p.prompt_length,
                   p.prompt_text, p.command_name
              FROM prompts p
             WHERE p.prompt_id IS NOT NULL
             ORDER BY p.session_id, p.ts
        """)
        prev_end: dict[str, str] = {}
        seq: dict[str, int] = {}

        for r in prompts:
            tid = r["prompt_id"]
            sid = r["session_id"]
            seq[sid] = seq.get(sid, 0) + 1

            agg = self.conn.execute("""
                SELECT MAX(ts) AS ended,
                       SUM(CASE WHEN event_name='api_request' THEN 1 ELSE 0 END) AS api
                  FROM events WHERE prompt_id=?""", (tid,)).fetchone()
            cost = db.scalar(self.conn,
                             "SELECT COALESCE(SUM(cost_usd),0) FROM api_calls"
                             " WHERE prompt_id=? AND outcome='ok'", (tid,), default=0.0)
            tools = self.conn.execute("""
                SELECT COUNT(*) AS n,
                       SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN decision='reject' THEN 1 ELSE 0 END) AS rejects,
                       SUM(CASE WHEN decision_source IN ('user_reject','user_abort')
                                THEN 1 ELSE 0 END) AS overrides,
                       COUNT(DISTINCT file_path) AS files
                  FROM tool_calls WHERE prompt_id=?""", (tid,)).fetchone()

            ended = agg["ended"] if agg else None
            started = r["ts"]
            dur = self._seconds_between(started, ended)
            gap = self._seconds_between(prev_end.get(sid), started)
            if ended:
                prev_end[sid] = ended

            text = r["prompt_text"]
            is_system = 1 if (text and self._SYSTEM_PROMPT_RE.match(text)) else 0
            steering, correction, cue = self._classify_prompt(
                text, r["prompt_length"], r["command_name"])
            if is_system:
                steering = correction = 0
                cue = None

            db.insert_ignore(self.conn, "turns", {
                "turn_id": tid, "session_id": sid, "project_id": r["project_id"],
                "seq": seq[sid], "started_at": started, "ended_at": ended,
                "duration_s": dur, "gap_before_s": gap,
                "prompt_length": r["prompt_length"],
                "prompt_text": r["prompt_text"],
                "is_steering": steering, "is_correction": correction,
                "correction_cue": cue, "is_system": is_system,
                "api_calls": (agg["api"] if agg else 0) or 0,
                "cost_usd": cost,
                "tool_calls": (tools["n"] if tools else 0) or 0,
                "tool_failures": (tools["failed"] if tools else 0) or 0,
                "rejects": (tools["rejects"] if tools else 0) or 0,
                "user_overrides": (tools["overrides"] if tools else 0) or 0,
                "files_touched": (tools["files"] if tools else 0) or 0,
            })

        self._derive_rework()
        # `turns` is rebuilt from scratch each run, so re-apply any cached
        # model labels; otherwise analyse would silently discard them.
        try:
            from .classify import apply_labels, import_cached
            import_cached(self.conn)
            apply_labels(self.conn)
            from .skillaudit import import_cached as import_audit
            import_audit(self.conn)
            from .corrections import import_cached as import_causes
            import_causes(self.conn)
            from .docs import import_cached as import_docs, import_gaps
            import_docs(self.conn)
            import_gaps(self.conn)
            from .narrate import import_cached as import_narrative
            import_narrative(self.conn)
            from .sessiondx import import_cached as import_dx
            import_dx(self.conn)
            from .sessionchat import import_cached as import_chat
            import_chat(self.conn)
        except Exception:
            pass
        self.conn.commit()

    @staticmethod
    def _seconds_between(a: str | None, b: str | None) -> float | None:
        if not a or not b:
            return None
        try:
            ta = _dt.datetime.fromisoformat(a.replace("Z", "+00:00"))
            tb = _dt.datetime.fromisoformat(b.replace("Z", "+00:00"))
        except ValueError:
            return None
        delta = (tb - ta).total_seconds()
        return delta if delta >= 0 else None

    def _derive_rework(self) -> None:
        """Files returned to across separate turns within a session."""
        self.conn.execute("DELETE FROM file_rework")
        self.conn.execute("""
            INSERT INTO file_rework
              (session_id, project_id, path, repo_relative_path, turns, edits,
               first_ts, last_ts)
            SELECT t.session_id, MAX(f.project_id), f.path,
                   MAX(f.repo_relative_path),
                   COUNT(DISTINCT t.prompt_id), COUNT(*),
                   MIN(f.ts), MAX(f.ts)
              FROM file_activity f
              JOIN tool_calls t ON t.tool_use_id = f.tool_use_id
                               AND t.session_id = f.session_id
             WHERE f.path IS NOT NULL AND t.prompt_id IS NOT NULL
             GROUP BY t.session_id, f.path
            HAVING COUNT(DISTINCT t.prompt_id) > 1
        """)
        self.conn.commit()

    def _attribute_commit(self, project_id, commit) -> tuple[str | None, str]:
        """Which session produced this commit?

        Time alone is a poor signal: sessions run concurrently, so "the one
        that started most recently" credited an atlas-app commit to a
        session that never opened either changed file. Prefer the session that actually
        touched the files the commit changed, and fall back to the time window
        only when no file evidence exists.
        """
        paths = [f["path"] for f in commit.get("files", [])]
        if paths:
            marks = ",".join("?" for _ in paths)
            row = self.conn.execute(f"""
                SELECT f.session_id, COUNT(*) touches
                  FROM file_activity f
                 WHERE f.session_id IS NOT NULL
                   AND EXISTS (SELECT 1 FROM (SELECT ? AS p {"UNION ALL SELECT ?" * (len(paths) - 1)})
                                WHERE f.path LIKE '%/' || p)
                 GROUP BY f.session_id ORDER BY touches DESC LIMIT 1""",
                tuple(paths)).fetchone()
            if row and row["touches"]:
                return row["session_id"], "changed_files"

        row = self.conn.execute(
            "SELECT session_id FROM sessions WHERE project_id=?"
            " AND first_seen <= ? AND (last_seen >= ? OR last_seen IS NULL)"
            " ORDER BY first_seen DESC LIMIT 1",
            (project_id, commit["committed_at"], commit["committed_at"])).fetchone()
        return (row["session_id"], "session_time_window") if row else (None, "none")

    def _reconcile_git(self) -> None:
        """Record commits made in observed repositories during observed sessions."""
        projects = db.q(self.conn,
                        "SELECT project_id, repo_root FROM projects"
                        " WHERE is_git=1 AND repo_root IS NOT NULL")
        for p in projects:
            window = self.conn.execute(
                "SELECT MIN(first_seen) AS a, MAX(last_seen) AS b FROM sessions"
                " WHERE project_id=?", (p["project_id"],)).fetchone()
            if not window or not window["a"]:
                continue
            commits = gitctx.commits_since(p["repo_root"], window["a"], None)
            for c in commits:
                sess, how = self._attribute_commit(p["project_id"], c)
                db.insert_ignore(self.conn, "git_activity", {
                    "dedupe_key": f"git|{p['project_id']}|{c['commit_sha']}",
                    "project_id": p["project_id"], "commit_sha": c["commit_sha"],
                    "committed_at": c["committed_at"], "author_name": c["author_name"],
                    "author_email": c["author_email"], "subject": scrub(c["subject"]),
                    "files_changed": c["files_changed"], "insertions": c["insertions"],
                    "deletions": c["deletions"],
                    "session_id": sess,
                    "attribution": how,
                    "source": "local_git_reconcile",
                })
                types = gitctx.commit_change_types(p["repo_root"], c["commit_sha"])
                for f in c["files"]:
                    db.insert_ignore(self.conn, "git_commit_files", {
                        "commit_sha": c["commit_sha"], "project_id": p["project_id"],
                        "path": f["path"], "change_type": types.get(f["path"]),
                        "insertions": f["insertions"], "deletions": f["deletions"],
                    })
        self.conn.commit()


def analyse(conn: sqlite3.Connection, raw_dir: Path | None = None,
            progress=None, describe: bool | None = None) -> dict:
    return Ingestor(conn, progress=progress, describe=describe).run(raw_dir)
