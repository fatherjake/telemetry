"""`telemetry` command line."""
from __future__ import annotations

import argparse
import datetime as _dt
import textwrap
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import config, db, gitctx, ingest, queries as Q, report
from .synthetic import build_demo_session

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, RED, YELLOW = "\033[32m", "\033[31m", "\033[33m"
CYAN = "\033[36m"


def _c(text, colour):
    return f"{colour}{text}{RESET}" if sys.stdout.isatty() else str(text)


def _now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ------------------------------------------------------------------ helpers --

def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def compose(*args, capture=False):
    return subprocess.run(
        ["docker", "compose", "-f", str(config.COMPOSE_FILE), *args],
        cwd=str(config.ROOT), capture_output=capture, text=True)


def collector_running() -> bool:
    try:
        r = subprocess.run(
            ["docker", "ps", "--filter", f"name={config.CONTAINER_NAME}",
             "--filter", "status=running", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=20)
        return config.CONTAINER_NAME in r.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def health_ok(timeout=3) -> bool:
    for url in (f"http://127.0.0.1:{config.HEALTH_PORT}/",
                f"http://127.0.0.1:{config.OTLP_HTTP_PORT}/health"):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                if r.status == 200:
                    return True
        except Exception:
            continue
    return False


def port_open(port: int, host="127.0.0.1", timeout=2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


PIDFILE = None  # set lazily


def _pidfile() -> Path:
    return config.VAR_DIR / "receiver.pid"


def fallback_running() -> int | None:
    p = _pidfile()
    if not p.exists():
        return None
    try:
        pid = int(p.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        p.unlink(missing_ok=True)
        return None


def raw_stats() -> dict:
    files = ingest.scan_raw_files()
    total_bytes = sum(p.stat().st_size for p, _ in files if p.exists())
    newest = max((p.stat().st_mtime for p, _ in files if p.exists()), default=0)
    lines = 0
    for p, _ in files:
        if p.exists():
            with p.open("rb") as fh:
                lines += sum(1 for _ in fh)
    return {"files": len(files), "bytes": total_bytes, "lines": lines,
            "newest_mtime": newest}


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


# ----------------------------------------------------------------- commands --

def cmd_start(args) -> int:
    config.ensure_dirs()
    if args.no_docker:
        if fallback_running():
            print("fallback receiver already running")
            return 0
        log = config.VAR_DIR / "receiver.log"
        proc = subprocess.Popen(
            [sys.executable, "-m", "tel.receiver", str(config.OTLP_HTTP_PORT)],
            cwd=str(config.ROOT), stdout=log.open("a"), stderr=subprocess.STDOUT,
            start_new_session=True)
        _pidfile().write_text(str(proc.pid))
        time.sleep(1.0)
        ok = port_open(config.OTLP_HTTP_PORT)
        print(_c("started" if ok else "failed to start", GREEN if ok else RED),
              f"fallback OTLP/JSON receiver on http://127.0.0.1:{config.OTLP_HTTP_PORT}")
        print(f"{DIM}note: fallback mode accepts http/json only "
              f"(set OTEL_EXPORTER_OTLP_PROTOCOL=http/json){RESET}")
        print(f"log: {log}")
        return 0 if ok else 1

    if not docker_available():
        print(_c("docker is not available or the daemon is not running.", RED))
        print("Either start Docker Desktop, or run:  ./telemetry start --no-docker")
        return 1

    r = compose("up", "-d", capture=True)
    if r.returncode != 0:
        print(_c("failed to start the collector", RED))
        print(r.stderr.strip()[-2000:])
        return 1
    for _ in range(30):
        if health_ok():
            break
        time.sleep(1)
    ok = health_ok()
    print(_c("collector running" if ok else "collector started but health check failed",
             GREEN if ok else YELLOW))
    print(f"  OTLP gRPC  http://localhost:{config.OTLP_GRPC_PORT}")
    print(f"  OTLP HTTP  http://localhost:{config.OTLP_HTTP_PORT}")
    print(f"  health     http://localhost:{config.HEALTH_PORT}")
    print(f"  raw data   {config.RAW_DIR}")
    print(f"\nNext:  source {config.ENV_FILE.name}   then run claude as usual")
    return 0


def cmd_stop(args) -> int:
    rc = 0
    pid = fallback_running()
    if pid:
        try:
            os.kill(pid, 15)
            print("stopped fallback receiver")
        except OSError as e:
            print(_c(f"could not stop receiver: {e}", RED))
            rc = 1
        _pidfile().unlink(missing_ok=True)
    if docker_available() and collector_running():
        r = compose("down", capture=True)
        if r.returncode == 0:
            print("stopped collector")
        else:
            print(_c(r.stderr.strip()[-1000:], RED))
            rc = 1
    elif not pid:
        print("nothing running")
    return rc


def cmd_status(args) -> int:
    running_docker = collector_running()
    pid = fallback_running()
    hp = health_ok()
    grpc = port_open(config.OTLP_GRPC_PORT)
    http = port_open(config.OTLP_HTTP_PORT)

    print(f"{BOLD}Collector{RESET}")
    if running_docker:
        print(f"  container      {_c('running', GREEN)} ({config.CONTAINER_NAME})")
    elif pid:
        print(f"  fallback       {_c('running', GREEN)} (pid {pid}, http/json only)")
    else:
        print(f"  container      {_c('not running', RED)}")
    print(f"  health         {_c('ok', GREEN) if hp else _c('unreachable', RED)}")
    print(f"  OTLP gRPC 4317 {_c('open', GREEN) if grpc else _c('closed', RED)}")
    print(f"  OTLP HTTP 4318 {_c('open', GREEN) if http else _c('closed', RED)}")

    if config.DB_PATH.exists():
        _c2 = db.connect(create=False)
        deploys = db.scalar(_c2, "SELECT COUNT(*) FROM deployments")
        commits = db.scalar(_c2, "SELECT COUNT(*) FROM git_activity")
        _c2.close()
        if commits and not deploys:
            print(_c("\nNo deployments recorded but commits exist - connector data "
                     "does not survive a database rebuild.", YELLOW))
            print("  run: ./telemetry config connect all")

    rs = raw_stats()
    print(f"\n{BOLD}Raw telemetry{RESET}  {config.RAW_DIR}")
    print(f"  files          {rs['files']}")
    print(f"  size           {human_bytes(rs['bytes'])}")
    print(f"  lines          {rs['lines']:,}")
    if rs["newest_mtime"]:
        age = time.time() - rs["newest_mtime"]
        state = (_c("events arriving", GREEN) if age < 300
                 else _c("no new events recently", YELLOW))
        last = _dt.datetime.fromtimestamp(rs["newest_mtime"]).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  last write     {last}  ({age/60:.1f} min ago)  {state}")
    else:
        print(f"  last write     {_c('never - no telemetry received yet', YELLOW)}")

    if not config.DB_PATH.exists():
        print(f"\n{BOLD}Database{RESET}\n  not created yet - run: ./telemetry analyse")
    else:
        conn = db.connect()
        o = Q.overview(conn)
        pending = rs["lines"] - (db.scalar(conn, "SELECT COALESCE(SUM(lines_consumed),0) FROM raw_files"))
        print(f"\n{BOLD}Database{RESET}  {config.DB_PATH}")
        print(f"  sessions       {o['sessions']}")
        print(f"  events         {o['events']:,}   metrics {o['metric_points']:,}   spans {o['spans']:,}")
        print(f"  api calls      {o['api_calls']:,}   cost ${o['cost_usd']:.4f}")
        print(f"  tool calls     {o['tool_calls']:,}   files touched "
              f"{o['distinct_files_read'] + o['distinct_files_changed']:,}")
        print(f"  last analysed  {db.get_meta(conn, 'last_analyse_at') or 'never'}")
        if pending > 0:
            print(f"  {_c(f'{pending:,} raw lines not yet analysed - run ./telemetry analyse', YELLOW)}")
        conn.close()

    policy = config.load_policy()
    content_on = [k for k in ("store_content", "store_tool_content",
                              "store_api_bodies", "store_span_events")
                  if policy.get(k)]
    print(f"\n{BOLD}Storage policy{RESET}")
    if content_on:
        print(f"  {_c('content storage ON', YELLOW)}: {', '.join(content_on)}")
        print(f"  {DIM}data/ holds source code and conversation text. "
              f"./telemetry config privacy --disable-all reverts.{RESET}")
    else:
        print(f"  {_c('metadata only', GREEN)} (no prompts, responses or file contents)")
    gb = rs["bytes"] / (1024 ** 3)
    if gb >= 1.0:
        print(f"  {_c(f'raw archive is {gb:.1f} GB', YELLOW)} - see README on pruning")

    env_set = os.environ.get("CLAUDE_CODE_ENABLE_TELEMETRY") == "1"
    print(f"\n{BOLD}This shell{RESET}")
    print(f"  CLAUDE_CODE_ENABLE_TELEMETRY  {_c('set', GREEN) if env_set else _c('not set', YELLOW)}")
    if not env_set:
        print(f"  {DIM}run: source {config.ENV_FILE}{RESET}")
    return 0


def cmd_analyse(args) -> int:
    conn = db.connect()
    t0 = time.time()
    res = ingest.analyse(conn, progress=lambda msg: print(f"{DIM}{msg}{RESET}",
                                                         flush=True))
    print(f"scanned {res['files']} raw file(s) in {time.time()-t0:.1f}s")
    print(f"  log records   {res['logs']:,}")
    print(f"  metric points {res['metrics']:,}")
    print(f"  spans         {res['traces']:,}")
    if res["skipped"]:
        print(_c(f"  skipped       {res['skipped']} (incomplete or unparseable lines)", YELLOW))
    if res.get("described"):
        print(f"  named         {res['described']} session(s)")
    o = Q.overview(conn)
    print(f"\ntotals: {o['sessions']} sessions, {o['api_calls']} api calls, "
          f"${o['cost_usd']:.4f}, {o['tool_calls']} tool calls, "
          f"{o['files_read']} reads, {o['files_changed']} writes/edits, "
          f"{o['bash_commands']} commands")
    conn.close()
    return 0


def _print_table(headers, rows):
    if not rows:
        print(f"{DIM}(no rows){RESET}")
        return
    widths = [len(h) for h in headers]
    srows = [[("" if c is None else str(c)) for c in r] for r in rows]
    for r in srows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(BOLD + line + RESET if sys.stdout.isatty() else line)
    print(DIM + "  ".join("-" * w for w in widths) + RESET
          if sys.stdout.isatty() else "  ".join("-" * w for w in widths))
    for r in srows:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))


def cmd_report(args) -> int:
    conn = db.connect()
    if args.view == "sessions":
        from . import report_sessions
        path = report_sessions.write(conn, Path(args.output) if args.output else None)
    else:
        path = report.write(conn, Path(args.output) if args.output else None)
    conn.close()
    print(f"report written to {path}")
    print(f"open with:  open {path}")
    if args.open:
        subprocess.run(["open", str(path)], check=False)
    return 0


def cmd_session_hook(args) -> int:
    """Record local git context for a session. Reads Claude Code hook JSON on stdin."""
    payload = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
    except (ValueError, OSError):
        payload = {}

    session_id = args.session_id or payload.get("session_id")
    cwd = args.cwd or payload.get("cwd") or os.getcwd()
    if not session_id:
        # Nothing useful to record; never fail the user's session over telemetry.
        return 0

    desc = gitctx.describe_full(cwd)
    # Append to a plain file rather than writing SQLite. This keeps the hook
    # fast (it runs on every session start), avoids any chance of lock
    # contention with a concurrent `analyse`, and - importantly - keeps this
    # context on disk so the database remains rebuildable from files alone.
    record = {
        "session_id": session_id, "phase": args.phase, "captured_at": _now(),
        "cwd": cwd, "repo_root": desc.get("repo_root"),
        "remote_url": desc.get("remote_url"), "branch": desc.get("branch"),
        "head_sha": desc.get("head_sha"), "is_dirty": desc.get("is_dirty"),
    }
    config.SESSION_CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with config.SESSION_CONTEXT_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    if not args.quiet:
        print(f"recorded {args.phase} context for {session_id[:8]} "
              f"({desc.get('repo_root') or cwd})")
    return 0


HOOK_SETTINGS_KEY = "hooks"


def _hook_block() -> dict:
    exe = str(config.ROOT / "telemetry")
    return {
        "SessionStart": [{"hooks": [{"type": "command",
                                     "command": f"{exe} session-hook --phase start --quiet"}]}],
        "SessionEnd": [{"hooks": [{"type": "command",
                                   "command": f"{exe} session-hook --phase end --quiet"}]}],
    }


def cmd_install_hooks(args) -> int:
    target = Path(args.settings or (Path.home() / ".claude" / "settings.json"))
    print(f"{BOLD}This will modify {target}{RESET}")
    print("It adds SessionStart and SessionEnd hooks that record the working directory,")
    print("repo root, branch and HEAD for each Claude Code session. It reads no file contents.")
    print(json.dumps(_hook_block(), indent=2))
    if not args.yes:
        try:
            if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("aborted")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\naborted")
            return 1

    settings = {}
    if target.exists():
        backup = target.with_suffix(f".json.bak-{int(time.time())}")
        shutil.copy2(target, backup)
        print(f"backed up existing settings to {backup}")
        try:
            settings = json.loads(target.read_text() or "{}")
        except ValueError:
            print(_c("existing settings.json is not valid JSON; aborting", RED))
            return 1

    hooks = settings.setdefault(HOOK_SETTINGS_KEY, {})
    for event, block in _hook_block().items():
        existing = hooks.setdefault(event, [])
        cmds = json.dumps(existing)
        if "session-hook" in cmds:
            continue
        existing.extend(block)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(settings, indent=2) + "\n")
    print(_c("hooks installed", GREEN))
    print("They take effect in newly started Claude Code sessions.")
    return 0


def cmd_uninstall_hooks(args) -> int:
    target = Path(args.settings or (Path.home() / ".claude" / "settings.json"))
    if not target.exists():
        print("nothing to do")
        return 0
    settings = json.loads(target.read_text() or "{}")
    hooks = settings.get(HOOK_SETTINGS_KEY, {})
    removed = 0
    for event in list(hooks):
        kept = []
        for entry in hooks[event]:
            if "session-hook" in json.dumps(entry):
                removed += 1
                continue
            kept.append(entry)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    if not hooks:
        settings.pop(HOOK_SETTINGS_KEY, None)
    target.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"removed {removed} hook entr{'y' if removed == 1 else 'ies'}")
    return 0


# --- global env block in Claude Code settings -------------------------------

# Keys this project owns inside settings.json "env". uninstall-env removes
# exactly these and leaves anything else you have put there alone.
MANAGED_ENV_PREFIXES = ("CLAUDE_CODE_ENABLE_TELEMETRY",
                        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
                        "CLAUDE_CODE_OTEL_", "OTEL_", "TELEMETRY_")


def _managed(key: str) -> bool:
    return any(key.startswith(pfx) for pfx in MANAGED_ENV_PREFIXES)


def parse_env_file(path: Path | None = None) -> dict:
    """Read the uncommented `export KEY=VALUE` lines out of .env.telemetry.

    .env.telemetry stays the single source of truth, so re-running
    ./setup.sh --minimal and then install-env keeps the two consistent.
    """
    src = Path(path or config.ENV_FILE)
    out: dict[str, str] = {}
    if not src.exists():
        return out
    for line in src.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        # strip trailing inline comments (`VALUE   # explanation`)
        if "#" in v:
            head, sep, _tail = v.partition("#")
            if head.rstrip() != head or not head:   # only when whitespace precedes #
                v = head.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if _managed(k):
            out[k] = v
    return out


def cmd_install_env(args) -> int:
    target = Path(args.settings or (Path.home() / ".claude" / "settings.json"))
    env = parse_env_file()
    if not env:
        print(_c(f"no settings found in {config.ENV_FILE} - run ./setup.sh first", RED))
        return 1

    print(f"{BOLD}This will add an \"env\" block to {target}{RESET}")
    print("Every Claude Code session on this machine will then send telemetry to")
    print("your local collector, whatever shell or launcher started it.\n")
    for k, v in env.items():
        print(f"  {k}={v}")
    print()
    if not args.yes:
        try:
            if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("aborted")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\naborted")
            return 1

    settings = {}
    if target.exists():
        backup = target.with_suffix(f".json.bak-{int(time.time())}")
        shutil.copy2(target, backup)
        print(f"backed up existing settings to {backup}")
        try:
            settings = json.loads(target.read_text() or "{}")
        except ValueError:
            print(_c("existing settings.json is not valid JSON; aborting", RED))
            return 1

    block = settings.setdefault("env", {})
    block.update(env)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(settings, indent=2) + "\n")
    print(_c(f"installed {len(env)} variable(s)", GREEN))
    print("Takes effect in newly started Claude Code sessions - settings are read")
    print("at startup, so sessions already running are unaffected.")
    print(f"\nUndo with:  ./telemetry config uninstall")
    return 0


def cmd_uninstall_env(args) -> int:
    target = Path(args.settings or (Path.home() / ".claude" / "settings.json"))
    if not target.exists():
        print("nothing to do")
        return 0
    settings = json.loads(target.read_text() or "{}")
    block = settings.get("env", {})
    removed = [k for k in list(block) if _managed(k)]
    for k in removed:
        block.pop(k)
    if not block:
        settings.pop("env", None)
    target.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"removed {len(removed)} telemetry variable(s) from {target}")
    return 0


# --- storage policy ----------------------------------------------------------

POLICY_LABELS = {
    "store_content": ("prompt and assistant-response text",
                      "OTEL_LOG_USER_PROMPTS / OTEL_LOG_ASSISTANT_RESPONSES"),
    "store_tool_content": ("full tool arguments, including file contents",
                           "OTEL_LOG_TOOL_DETAILS"),
    "store_api_bodies": ("raw API request/response JSON",
                         "OTEL_LOG_RAW_API_BODIES"),
    "store_span_events": ("tool input/output bodies from spans",
                          "OTEL_LOG_TOOL_CONTENT"),
    "git_reconcile": ("read-only git commit reconciliation", "-"),
    "auto_describe": ("name finished sessions from their prompts, during analyse",
                      "-"),
}


def cmd_privacy(args) -> int:
    policy = config.load_policy()

    if args.enable_all or args.disable_all:
        want = bool(args.enable_all)
        for k in POLICY_LABELS:
            if k not in ("git_reconcile", "auto_describe"):
                policy[k] = want
    for k in POLICY_LABELS:
        flag = getattr(args, k, None)
        if flag is not None:
            policy[k] = flag == "on"

    changed = args.enable_all or args.disable_all or any(
        getattr(args, k, None) is not None for k in POLICY_LABELS)
    if changed:
        config.save_policy(policy)

    print(f"{BOLD}Telemetry storage policy{RESET}  {config.POLICY_FILE}")
    for k, (desc, gate) in POLICY_LABELS.items():
        on = policy.get(k, False)
        state = (_c("ON ", GREEN if k in ("git_reconcile", "auto_describe") else RED)
                 if on else _c("off", GREEN))
        print(f"  {state}  {k:20s} {desc}")
        if k == "auto_describe" and on:
            print(f"       {DIM}sends prompt text to Anthropic through your "
                  f"`claude` CLI{RESET}")
        if gate != "-":
            print(f"       {DIM}needs Claude Code: {gate}{RESET}")
    print(f"  {_c('ON ', GREEN)}  {'redact_secrets':20s} credential redaction "
          f"{DIM}(always on){RESET}")

    if changed:
        print()
        print("Saved. Applies to the next ./telemetry analyse.")
        any_content = any(policy.get(k) for k in
                          ("store_content", "store_tool_content",
                           "store_api_bodies", "store_span_events"))
        if any_content:
            print(_c("\nContent storage is enabled. Make sure Claude Code is also "
                     "exporting content:", YELLOW))
            print("  ./setup.sh --full && ./telemetry init --yes")
            print(_c("data/ will now contain source code and conversation text. It is "
                     "gitignored; treat it as sensitive.", YELLOW))
    return 0


# --- outside-world connectors ------------------------------------------------

def _load_connector_cfg() -> dict:
    try:
        if config.CONNECTORS_FILE.exists():
            return json.loads(config.CONNECTORS_FILE.read_text() or "{}")
    except (OSError, ValueError):
        pass
    return {}


def _save_connector_cfg(cfg: dict) -> None:
    config.ensure_dirs()
    config.CONNECTORS_FILE.write_text(json.dumps(cfg, indent=2) + "\n")


def _pick_project(conn, wanted: str | None):
    """Resolve a project by name, id or remote; default to the busiest one."""
    rows = db.q(conn, "SELECT project_id, project_name, repo_root,"
                      " remote_normalized FROM projects WHERE is_git=1")
    if wanted:
        for r in rows:
            if wanted in (r["project_id"], r["project_name"] or "",
                          r["remote_normalized"] or ""):
                return r
        return None
    ranked = db.q(conn, """
        SELECT p.project_id, p.project_name, p.repo_root, p.remote_normalized,
               COALESCE(SUM(a.cost_usd),0) AS cost
          FROM projects p LEFT JOIN api_calls a ON a.project_id = p.project_id
         WHERE p.is_git=1 GROUP BY p.project_id ORDER BY cost DESC LIMIT 1""")
    return ranked[0] if ranked else None


def cmd_connect(args) -> int:
    from .connectors import github as gh_conn, eas as eas_conn

    conn = db.connect()
    cfg = _load_connector_cfg()
    project = _pick_project(conn, args.project)
    if not project:
        print(_c("no git project found - run ./telemetry analyse first", RED))
        return 1
    pid = project["project_id"]
    saved = cfg.setdefault(pid, {})
    providers = ["github", "eas"] if args.provider == "all" else [args.provider]
    rc = 0

    for provider in providers:
        print(f"{BOLD}{provider}{RESET}  project {project['project_name']}")
        try:
            if provider == "github":
                repo = (args.repo or saved.get("github_repo")
                        or gh_conn.repo_from_remote(project["remote_normalized"]))
                if not repo:
                    print(_c("  could not determine the repo; pass --repo owner/name", RED))
                    rc = 1
                    continue
                print(f"  repo {repo}")
                counts = gh_conn.collect(conn, pid, repo, limit=args.limit)
                saved["github_repo"] = repo
            else:
                app_dir = args.dir or saved.get("eas_dir")
                if not app_dir:
                    print(_c("  pass --dir pointing at the Expo app directory", RED))
                    rc = 1
                    continue
                branches = (args.branch or saved.get("eas_branches")
                            or ["production", "staging"])
                if isinstance(branches, str):
                    branches = [branches]
                print(f"  dir {app_dir}  branches {', '.join(branches)}")
                counts = eas_conn.collect(conn, pid, app_dir, branches,
                                          limit=args.limit)
                saved["eas_dir"] = app_dir
                saved["eas_branches"] = branches
        except Exception as exc:
            print(_c(f"  {type(exc).__name__}: {exc}", RED))
            rc = 1
            continue
        for k, v in counts.items():
            print(f"    {k:20s} {v}")

    _save_connector_cfg(cfg)
    _link_deploys_to_streams(conn)
    conn.commit()
    conn.close()
    print("\nrun ./telemetry run to see cost against shipped work")
    return rc


def _link_deploys_to_streams(conn) -> None:
    """Attach each deployment to the project and time of the commit it shipped."""
    conn.execute("""
        UPDATE deployments SET project_id = COALESCE(project_id,
            (SELECT g.project_id FROM git_activity g
              WHERE g.commit_sha = deployments.commit_sha))
         WHERE commit_sha IS NOT NULL""")
    # EAS reports no publish time. Where an update matched a commit, the
    # commit's timestamp is the best available stand-in; unmatched updates
    # keep a NULL rather than a fabricated date.
    conn.execute("""
        UPDATE deployments SET created_at =
            (SELECT g.committed_at FROM git_activity g
              WHERE g.commit_sha = deployments.commit_sha)
         WHERE created_at IS NULL AND commit_sha IS NOT NULL""")
    conn.commit()


# --- model-assisted turn classification --------------------------------------

def cmd_classify(args) -> int:
    """Label turns with a small model instead of the length heuristic.

    This is the only command that sends anything off the machine, so it says
    so and asks first.
    """
    from . import classify as C

    conn = db.connect()
    todo = C.pending(conn, args.limit)
    total = db.scalar(conn, "SELECT COUNT(*) FROM turns")
    cached = db.scalar(conn, "SELECT COUNT(*) FROM turn_classification")

    if args.apply_only:
        n = C.apply_labels(conn)
        print(f"applied {cached} cached label(s) to turns")
        conn.close()
        return 0

    print(f"{BOLD}Turn classification{RESET}")
    print(f"  turns           {total}")
    print(f"  already labelled {cached}")
    print(f"  to classify     {len(todo)}"
          f"{'' if not args.limit else f' (limited to {args.limit})'}")
    if not todo:
        print("\nnothing to do - every turn with prompt text is already labelled")
        C.apply_labels(conn)
        conn.close()
        return 0

    batches = (len(todo) + args.batch - 1) // args.batch
    print(f"  batches         {batches} of up to {args.batch}, model '{args.model}'")

    print(f"\n{_c('This sends prompt text to Anthropic.', YELLOW)}")
    print("Everything else in this project stays on your machine; this does not.")
    print(f"It goes through your authenticated `claude` CLI, with telemetry and MCP")
    print(f"switched off for the subprocess so it cannot feed back into the database.")
    print(f"Each prompt is truncated to 500 characters, plus 160 characters of the")
    print(f"previous prompt for context. Results are cached, so a repeat run is free.")

    if args.dry_run:
        print(f"\n{DIM}--dry-run: showing the first batch payload, sending nothing{RESET}\n")
        print(C._payload(todo[:args.batch])[:2000])
        conn.close()
        return 0

    if not args.yes:
        try:
            if input("\nSend? [y/N] ").strip().lower() not in ("y", "yes"):
                print("aborted - nothing sent")
                conn.close()
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\naborted - nothing sent")
            conn.close()
            return 1

    def progress(batch_no, done, total_todo):
        print(f"  batch {batch_no}/{batches} … {done}/{total_todo}", flush=True)

    t0 = time.time()
    stats = C.classify(conn, model=args.model, limit=args.limit,
                       batch_size=args.batch, progress=progress)
    print(f"\nclassified {stats['classified']} turn(s) in {time.time()-t0:.0f}s")
    if stats["failed"]:
        print(_c(f"  {stats['failed']} could not be classified (kept the heuristic)",
                 YELLOW))

    rows = db.q(conn, """
        SELECT COALESCE(label_source,'heuristic') src,
               SUM(is_correction) corrections, SUM(is_steering) steers,
               COUNT(*) turns FROM turns GROUP BY 1""")
    print(f"\n{BOLD}Labels now in use{RESET}")
    _print_table(["SOURCE", "TURNS", "CORRECTIONS", "STEERING"],
                 [[r["src"], r["turns"], r["corrections"] or 0, r["steers"] or 0]
                  for r in rows])

    diff = [r for r in C.agreement(conn) if r["model_label"] != r["heuristic_label"]]
    if diff:
        print(f"\n{BOLD}Where the model disagreed with the length heuristic{RESET} "
              f"({len(diff)})")
        _print_table(["HEURISTIC", "MODEL", "CONF", "PROMPT", "WHY"],
                     [[d["heuristic_label"], d["model_label"],
                       d["label_confidence"] or "-", (d["prompt"] or "")[:44],
                       (d["rationale"] or "")[:38]] for d in diff[:20]])
    print(f"\n{DIM}Re-run ./telemetry report --view sessions to pick these up.{RESET}")
    conn.close()
    return 0


def cmd_audit_skills(args) -> int:
    """Judge, per session, whether an available skill should have fired."""
    from . import skillaudit as A

    conn = db.connect()
    done = db.scalar(conn, "SELECT COUNT(DISTINCT session_id) FROM skill_audit")

    targets = None
    if args.session:
        targets = [r["session_id"] for r in db.q(
            conn, "SELECT session_id FROM sessions WHERE session_id LIKE ?",
            (f"{args.session}%",))]
        if not targets:
            print(_c(f"no session matching {args.session}", RED))
            conn.close()
            return 1
    pending = targets if targets else A.pending_sessions(conn, args.limit)

    if not args.report_only:
        print(f"{BOLD}Skill trigger audit{RESET}")
        print(f"  sessions already audited  {done}")
        print(f"  to audit                  {len(pending)}"
              f"{' (re-audit)' if targets else ''}")
        if pending:
            print(f"  model                     {args.model}, one call per session")
            print(f"\n{_c('This sends session summaries to Anthropic.', YELLOW)}")
            print("Prompt text, tool names, file paths and commands for each session,")
            print("plus the descriptions of skills that were available. Same route and")
            print("same caveats as the classify pass; results are cached.")
            if args.dry_run:
                sid = pending[0]
                print(f"\n{DIM}--dry-run: summary for {sid[:8]}, sending nothing{RESET}\n")
                print(json.dumps(A.session_summary(conn, sid), indent=1)[:1800])
                print(f"\n{DIM}candidate skills: "
                      f"{[c['skill'] for c in A.candidates(conn, sid)][:12]}{RESET}")
                conn.close()
                return 0
            if not args.yes:
                try:
                    if input("\nSend? [y/N] ").strip().lower() not in ("y", "yes"):
                        print("aborted - nothing sent")
                        conn.close()
                        return 1
                except (EOFError, KeyboardInterrupt):
                    print("\naborted")
                    conn.close()
                    return 1

            def progress(i, total, sid):
                print(f"  auditing {i}/{total}  {sid[:8]}", flush=True)

            t0 = time.time()
            stats = A.audit(conn, model=args.model, limit=args.limit,
                            progress=progress, session_ids=targets)
            print(f"\naudited {stats['sessions']} session(s) in {time.time()-t0:.0f}s: "
                  f"{stats['judged']} judgements, {stats['flagged']} misses flagged")
            if stats["failed"]:
                print(_c(f"  {stats['failed']} session(s) failed", YELLOW))

    rows = A.misses(conn)
    if not rows:
        print("\nno missed skills found")
        conn.close()
        return 0

    print(f"\n{BOLD}Skills that should have fired and did not{RESET}")
    _print_table(["SKILL", "SESSIONS", "READ+SKIPPED", "HIGH CONF", "TIMES USED",
                  "COST IN THOSE SESSIONS", "EXAMPLE"],
                 [[r["skill_name"], r["sessions_missed"], r["times_read_not_used"] or 0,
                   r["high_confidence"] or 0, r["invocations"],
                   f"${r['cost_in_those_sessions'] or 0:.2f}",
                   (r["example_reason"] or "")[:52]] for r in rows])

    sm = A.session_misses(conn, 10)
    if sm:
        print(f"\n{BOLD}Sessions with the most missed skills{RESET}")
        _print_table(["SESSION", "PROJECT", "MISSED", "COST", "SKILLS"],
                     [[r["session_id"][:8], (r["project_name"] or "-")[:20],
                       r["missed"], f"${r['cost'] or 0:.2f}",
                       (r["skills"] or "")[:58]] for r in sm])
    print(f"\n{DIM}READ+SKIPPED is the strongest signal: the agent opened the skill "
          f"and declined it, which points at the description rather than at "
          f"discoverability.{RESET}")
    conn.close()
    return 0


def cmd_corrections(args) -> int:
    """Trace corrections to a cause, and to what would have prevented them."""
    from . import corrections as C

    conn = db.connect()
    todo = C.pending(conn, args.limit)
    done = db.scalar(conn, "SELECT COUNT(*) FROM correction_cause")

    if not args.report_only:
        if args.redo:
            print(f"{DIM}--redo: discarding {done} cached diagnosis(es){RESET}")
            todo = [r["turn_id"] for r in db.q(
                conn, "SELECT turn_id FROM turns WHERE is_correction=1"
                      " AND COALESCE(is_system,0)=0 AND prompt_text IS NOT NULL")]
        print(f"{BOLD}Correction diagnosis{RESET}")
        print(f"  already diagnosed  {done}")
        print(f"  to diagnose        {len(todo)}")
        if todo:
            print(f"\n{_c('This sends correction context to Anthropic.', YELLOW)}")
            print("The correcting message, the instruction before it, and the tools,")
            print("files and commands in between. Same route as the classify pass.")
            if args.dry_run:
                ctx = C.context_for(conn, todo[0])
                print(f"\n{DIM}--dry-run: one example, sending nothing{RESET}\n")
                print(json.dumps(ctx, indent=1)[:1600])
                conn.close()
                return 0
            if not args.yes:
                try:
                    if input("\nSend? [y/N] ").strip().lower() not in ("y", "yes"):
                        print("aborted - nothing sent")
                        conn.close()
                        return 1
                except (EOFError, KeyboardInterrupt):
                    print("\naborted")
                    conn.close()
                    return 1
            t0 = time.time()
            stats = C.diagnose(conn, model=args.model, limit=args.limit,
                               redo=args.redo,
                               progress=lambda d, t: print(f"  {d}/{t}", flush=True))
            print(f"\ndiagnosed {stats['diagnosed']} in {time.time()-t0:.0f}s")
            if stats["failed"]:
                print(_c(f"  {stats['failed']} failed", YELLOW))

    rows = C.by_cause(conn)
    if not rows:
        print("\nno corrections diagnosed yet")
        conn.close()
        return 0
    total = sum(r["cost"] or 0 for r in rows)
    print(f"\n{BOLD}Why corrections happened{RESET}")
    _print_table(["CAUSE", "N", "COST", "TIME"],
                 [[r["cause"].replace("_", " "), r["n"], f"${r['cost'] or 0:.2f}",
                   f"{r['minutes']} min"] for r in rows])

    proposed = C.proposed_knowledge(conn)
    if proposed:
        fixable = sum(p["cost"] or 0 for p in proposed)
        print(f"\n{BOLD}What to write down{RESET}  "
              f"${fixable:.2f} of ${total:.2f} traces to something writable")
        for p in proposed:
            print(f"\n  {_c(p['fix_location'].replace('_',' '), GREEN)}  "
                  f"{DIM}${p['cost'] or 0:.2f} · {p['corrections']} correction(s)"
                  f"{RESET}")
            print(f"    {p['suggested_fix']}")
            if p["what_was_missing"]:
                print(f"    {DIM}missing: {p['what_was_missing'][:120]}{RESET}")
    print(f"\n{DIM}design_iteration has no proposed fix by design: subjective "
          f"refinement has no right answer knowable in advance.{RESET}")
    conn.close()
    return 0


SEV = {"high": RED, "medium": YELLOW, "low": DIM}


def _resolve_session(conn, prefix: str) -> str | None:
    """Accept a session id prefix, since nobody types 36 characters."""
    rows = db.q(conn, "SELECT session_id FROM sessions WHERE session_id LIKE ?",
                (prefix + "%",))
    if not rows:
        print(_c(f"no session matches {prefix!r}", RED))
        return None
    if len(rows) > 1:
        print(_c(f"{prefix!r} matches {len(rows)} sessions; be more specific",
                 RED))
        return None
    return rows[0]["session_id"]


def cmd_diagnose(args) -> int:
    """What went wrong in a session, and what to change about it."""
    from . import sessiondx as D

    conn = db.connect()

    if args.session:
        args.session = _resolve_session(conn, args.session)
        if not args.session:
            conn.close()
            return 1

    # The signals are free. Printing them without spending anything is a
    # first-class mode, not a debug flag.
    if args.signals:
        targets = ([args.session] if args.session else
                   [r["session_id"] for r in db.q(conn,
                    "SELECT session_id FROM session_summary"
                    " WHERE first_seen IS NOT NULL"
                    " ORDER BY cost_usd DESC LIMIT ?", (args.limit or 10,))])
        any_ = False
        for sid in targets:
            sigs = D.signals(conn, sid)
            if not sigs:
                continue
            any_ = True
            name = db.scalar(conn, "SELECT COALESCE(p.project_name,'—') FROM"
                                   " sessions s LEFT JOIN projects p ON"
                                   " p.project_id=s.project_id WHERE"
                                   " s.session_id=?", (sid,), "—")
            print(f"\n{BOLD}{sid[:8]}{RESET}  {DIM}{name}{RESET}")
            for x in sigs:
                print(f"  {_c(x['kind'].replace('_',' ').ljust(24), CYAN)}"
                      f"{x['subject'][:60]}")
                print(f"  {' ' * 24}{DIM}{x['detail']}{RESET}")
        if not any_:
            print("no friction signals above threshold")
        conn.close()
        return 0

    todo = [args.session] if args.session else D.pending(conn, args.limit)
    done = db.scalar(conn, "SELECT COUNT(DISTINCT session_id) FROM session_diagnosis")
    clean = db.scalar(conn, "SELECT COUNT(*) FROM session_dx_clean")

    if not args.report_only and todo:
        print(f"{BOLD}Session diagnosis{RESET}")
        print(f"  already reviewed   {done + clean}  {DIM}({done} with findings,"
              f" {clean} clean){RESET}")
        print(f"  to review          {len(todo)}")
        print(f"\n{_c('This sends friction signals to Anthropic.', YELLOW)}")
        print("File paths, command lines, tool names and the numbers around")
        print("them - plus the session description. No file contents.")
        if args.dry_run:
            ctx = None
            for sid in todo:
                ctx = D.context_for(conn, sid)
                if ctx:
                    break
            print(f"\n{DIM}--dry-run: one example, sending nothing{RESET}\n")
            print(json.dumps(ctx, indent=1)[:2000] if ctx
                  else "no session has signals above threshold")
            conn.close()
            return 0
        if not args.yes:
            try:
                if input("\nSend? [y/N] ").strip().lower() not in ("y", "yes"):
                    print("aborted - nothing sent")
                    conn.close()
                    return 1
            except (EOFError, KeyboardInterrupt):
                print("\naborted")
                conn.close()
                return 1
        t0 = time.time()
        stats = D.diagnose(conn, model=args.model, limit=args.limit,
                           redo=args.redo, session_id=args.session,
                           progress=lambda s: print(f"  {s[:8]}", flush=True))
        # --redo discards the cache and re-derives the worklist inside
        # diagnose(), so the count taken before the call is the wrong
        # denominator - it printed "16/11".
        print(f"\nreviewed {stats['sessions']}/{stats['pending']} in "
              f"{time.time()-t0:.0f}s: {stats['findings']} finding(s), "
              f"{stats['clean']} clean")
        if stats["failed"]:
            print(_c(f"  {stats['failed']} failed", YELLOW))

    rows = D.top_findings(conn, args.limit or 20)
    if not rows:
        print("\nnothing diagnosed yet - run without --report-only")
        conn.close()
        return 0
    print(f"\n{BOLD}What to change{RESET}")
    for r in rows:
        sev = (r["severity"] or "low").lower()
        print(f"\n  {_c(sev.upper().ljust(7), SEV.get(sev, DIM))}"
              f"{_c(r['kind'].replace('_',' '), CYAN)}  "
              f"{DIM}{r['sess']} · {r['project_name'] or '—'}{RESET}")
        for ln in textwrap.wrap(r["finding"] or "", 88):
            print(f"    {ln}")
        if r["evidence"]:
            print(f"    {DIM}{r['evidence'][:110]}{RESET}")
        if r["fix"]:
            print(f"    {_c('→ ' + (r['fix_location'] or '').replace('_',' '), GREEN)}"
                  f"  {r['fix']}")
    kinds = D.by_kind(conn)
    if kinds:
        print(f"\n{BOLD}By kind{RESET}")
        _print_table(["KIND", "N", "SESSIONS"],
                     [[k["kind"].replace("_", " "), k["n"], k["sessions"]]
                      for k in kinds])
    conn.close()
    return 0


def cmd_docs(args) -> int:
    """Knowledge-base coverage: what is cold, and which cold spots matter."""
    from . import docs as D

    conn = db.connect()
    root = str(Path(args.root).expanduser()) if args.root else None

    if args.scan or args.root:
        if not root:
            print(_c("pass --root pointing at the knowledge base", RED))
            conn.close()
            return 1
        n = D.scan(conn, root, args.pattern, args.exclude)
        print(f"{DIM}scanned {n} file(s) under {root}{RESET}")

    todo = D.unprofiled(conn, root, args.limit)
    if todo and not args.report_only:
        print(f"\n{BOLD}Profiling{RESET}  {len(todo)} unprofiled document(s)")
        print(f"\n{_c('This sends document paths, titles and short excerpts to '
                       'Anthropic.', YELLOW)}")
        print(f"Excerpts are capped at {D.EXCERPT_CHARS} characters. Knowledge bases")
        print("hold business-sensitive material: --titles-only sends no content,")
        print("and --exclude skips paths entirely.")
        if args.dry_run:
            print(f"\n{DIM}--dry-run: first batch, sending nothing{RESET}\n")
            print(json.dumps(todo[:3], indent=1)[:1400])
            conn.close()
            return 0
        if not args.yes:
            try:
                if input("\nSend? [y/N] ").strip().lower() not in ("y", "yes"):
                    print("aborted - nothing sent")
                    conn.close()
                    return 1
            except (EOFError, KeyboardInterrupt):
                print("\naborted")
                conn.close()
                return 1
        t0 = time.time()
        stats = D.profile(conn, root, model=args.model, limit=args.limit,
                          titles_only=args.titles_only,
                          progress=lambda d, t: print(f"  {d}/{t}", flush=True))
        print(f"\nprofiled {stats['profiled']} in {time.time()-t0:.0f}s")
        if stats["failed"]:
            print(_c(f"  {stats['failed']} failed", YELLOW))

    if args.gaps:
        pend = db.scalar(conn, """SELECT COUNT(*) FROM sessions s
                                   WHERE s.first_seen IS NOT NULL
                                     AND EXISTS (SELECT 1 FROM tool_calls t
                                                  WHERE t.session_id=s.session_id)
                                     AND s.session_id NOT IN
                                         (SELECT session_id FROM doc_gap)""")
        # --redo re-judges everything, so the count of unjudged sessions must
        # not gate it.
        if (pend or args.redo) and not args.report_only:
            total = db.scalar(conn, """SELECT COUNT(*) FROM sessions s
                                        WHERE s.first_seen IS NOT NULL
                                          AND EXISTS (SELECT 1 FROM tool_calls t
                                                       WHERE t.session_id=s.session_id)""")
            n = total if args.redo else pend
            print(f"\n{BOLD}Per-session document gaps{RESET}  {n} session(s)"
                  f"{' (re-judge)' if args.redo else ''}")
            print(f"{DIM}Candidates are pre-filtered locally by overlap, so only "
                  f"plausible documents are judged.{RESET}")
            if not args.yes:
                try:
                    if input("Send session summaries + document topics? [y/N] "
                             ).strip().lower() not in ("y", "yes"):
                        print("aborted")
                        conn.close()
                        return 1
                except (EOFError, KeyboardInterrupt):
                    print("\naborted")
                    conn.close()
                    return 1
            t0 = time.time()
            st = D.judge_gaps(conn, model=args.model, redo=args.redo,
                              progress=lambda i, n, w: print(f"  {i}/{n}", flush=True))
            print(f"  judged {st['judged']} pairing(s) across {st['sessions']} "
                  f"session(s) in {time.time()-t0:.0f}s — {st['gaps']} gaps")

        rows = D.gap_summary(conn)
        if rows:
            print(f"\n{BOLD}Documents that should have been read{RESET}")
            _print_table(["DOCUMENT", "WORDS", "SESSIONS", "HIGH", "EXAMPLE REASON"],
                         [[r["rel_path"][:40], r["word_count"], r["sessions"],
                           r["high"] or 0, (r["example"] or "")[:52]] for r in rows])
            print(f"\n{BOLD}By session{RESET}")
            for sess in Q.active_sessions(conn):
                g = D.gaps_for_session(conn, sess["session_id"])
                if g:
                    print(f"\n  {_c(Q.session_title(conn, dict(sess))[:60], YELLOW)}")
                    for x in g:
                        print(f"    {x['rel_path'][:48]:50s} "
                              f"{DIM}{x['confidence'] or '?':6s} {(x['reason'] or '')[:46]}{RESET}")
        else:
            print("\nno document gaps found")
        conn.close()
        return 0

    sm = D.summary(conn, root)
    if not sm["total"]:
        print("no documents scanned - pass --root")
        conn.close()
        return 0
    print(f"\n{BOLD}Knowledge base{RESET}  {root or 'all roots'}")
    print(f"  documents            {sm['total']}  ({sm['profiled']} profiled)")
    print(f"  agent-facing         {sm['agent_facing']}"
          f"   human-only {sm['human_only']}")
    print(f"  opened at least once {sm['read']}"
          f"   of which agent-facing {sm['agent_facing_read']}")
    cold_line = _c(f"cold spots           {sm['cold_spots']}", YELLOW)
    print(f"  {cold_line}   "
          f"{DIM}agent-facing, medium+ relevance, never opened{RESET}")

    cold = D.cold_spots(conn, root)
    if cold:
        print(f"\n{BOLD}Cold spots that matter{RESET}")
        _print_table(["DOCUMENT", "RELEVANCE", "WORDS", "TOPIC"],
                     [[c["rel_path"][:44], c["agent_relevance"], c["word_count"],
                       (c["topic"] or "")[:52]] for c in cold[:args.limit or 30]])

    warm = [r for r in D.coverage(conn, root) if r["reads"]]
    if warm:
        print(f"\n{BOLD}Documents actually consulted{RESET}")
        _print_table(["DOCUMENT", "READS", "SESSIONS", "AUDIENCE", "LAST"],
                     [[w["rel_path"][:44], w["reads"], w["sessions"],
                       w["audience"] or "?", report.short_ts(w["last_read"])]
                      for w in warm])
    print(f"\n{DIM}Human-only documents are excluded from cold spots on purpose: "
          f"a contacts file going unread by a coding agent is correct.{RESET}")
    conn.close()
    return 0


def cmd_describe(args) -> int:
    """One line per session saying what it was trying to get done."""
    from . import narrate as N

    conn = db.connect()
    todo = N.pending(conn, args.limit)
    if args.redo:
        todo = N.pending(conn) or todo
    if todo and not args.report_only:
        print(f"{BOLD}Descriptions{RESET}  {len(todo)} item(s) to describe")
        print(f"\n{_c('This sends the prompts of each session to Anthropic.', YELLOW)}")
        print("Same content as the skills pass; results are cached.")
        if args.dry_run:
            k, i = todo[0]
            print(f"\n{DIM}--dry-run: one example ({k}), sending nothing{RESET}\n")
            print(json.dumps(N._payload(conn, k, i), indent=1)[:1500])
            conn.close()
            return 0
        if not args.yes:
            try:
                if input("\nSend? [y/N] ").strip().lower() not in ("y", "yes"):
                    print("aborted - nothing sent")
                    conn.close()
                    return 1
            except (EOFError, KeyboardInterrupt):
                print("\naborted")
                conn.close()
                return 1
        t0 = time.time()
        st = N.generate(conn, model=args.model, limit=args.limit, redo=args.redo,
                        progress=lambda d, t: print(f"  {d}/{t}", flush=True))
        print(f"\nwrote {st['written']} description(s) in {time.time()-t0:.0f}s")
        if st["failed"]:
            print(_c(f"  {st['failed']} failed", YELLOW))

    rows = db.q(conn, """
        SELECT substr(n.subject_id,1,8) label, n.description
          FROM narrative n
          JOIN sessions s ON s.session_id = n.subject_id
         WHERE n.kind='session'
         ORDER BY s.first_seen DESC""")
    if rows:
        print(f"\n{BOLD}Sessions{RESET}")
        for r in rows:
            print(f"  {_c(r['label'].ljust(10), GREEN)} {r['description']}")
    conn.close()
    return 0


def cmd_run(args) -> int:
    """The browser, kept current while you work.

    New telemetry is folded in behind the UI rather than in front of it: the
    list is worth looking at immediately, and it refreshes itself as work
    happens. `--no-follow` opens it on stored data and leaves it there.
    """
    conn = db.connect()
    if not args.static:
        if not sys.stdout.isatty():
            print(_c("run needs a terminal; use --static to print a preview", RED))
            conn.close()
            return 1
        from . import tuiapp
        try:
            tuiapp.launch(conn, follow=not args.no_follow)
        except KeyboardInterrupt:
            pass
        conn.close()
        return 0

    from . import tui

    print()
    print(tui.render(conn, args.width))
    sid = args.session
    if not sid:
        row = conn.execute("""SELECT t.session_id FROM turns t
                               WHERE t.is_correction=1
                               GROUP BY t.session_id
                               ORDER BY SUM(t.cost_usd) DESC LIMIT 1""").fetchone()
        sid = row["session_id"] if row else None
    if sid:
        full = db.scalar(conn, "SELECT session_id FROM sessions"
                               " WHERE session_id LIKE ?", (f"{sid}%",), default=sid)
        print()
        print(tui.render_session(conn, full, args.width))
    print()
    print(f"{DIM}static preview - navigation not built yet. "
          f"COLORTERM={os.environ.get('COLORTERM', 'unset')}{RESET}")
    conn.close()
    return 0


def cmd_ignore(args) -> int:
    """Paths excluded from file activity."""
    patterns = config.load_ignores()
    changed = False
    for pat in (args.add or []):
        if pat not in patterns:
            patterns.append(pat); changed = True
    for pat in (args.remove or []):
        if pat in patterns:
            patterns.remove(pat); changed = True
    if args.reset:
        patterns = list(config.DEFAULT_IGNORES); changed = True
    if changed:
        config.save_ignores(patterns)

    using_defaults = not config.IGNORE_FILE.exists()
    print(f"{BOLD}Ignored paths{RESET}  "
          f"{config.IGNORE_FILE if not using_defaults else 'built-in defaults'}")
    for pat in patterns:
        print(f"  {pat}")
    if changed:
        print(f"\n{DIM}saved - takes effect on the next ./telemetry analyse{RESET}")

    if config.DB_PATH.exists():
        conn = db.connect(create=False)
        ignored = db.get_meta(conn, "ignored_paths")
        total = db.scalar(conn, "SELECT COUNT(DISTINCT path) FROM file_activity")
        conn.close()
        print(f"\n{DIM}{total} distinct paths kept"
              f"{f'; {ignored} dropped on the last analyse' if ignored else ''}"
              f"{RESET}")
    return 0


def cmd_sql(args) -> int:
    conn = db.connect(create=False) if config.DB_PATH.exists() else db.connect()
    query = args.query
    lowered = query.strip().lower()
    if not lowered.startswith(("select", "with", "pragma", "explain")):
        print(_c("only read queries are allowed here; use sqlite3 directly if you "
                 "really mean to write", RED))
        return 1
    rows = conn.execute(query).fetchall()
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))
    elif rows:
        _print_table(list(rows[0].keys()), [list(r) for r in rows])
    else:
        print(f"{DIM}(no rows){RESET}")
    conn.close()
    return 0


def cmd_doctor(args) -> int:
    """End-to-end self test."""
    results = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append((name, ok, detail))
        mark = _c("PASS", GREEN) if ok else _c("FAIL", RED)
        print(f"  [{mark}] {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
        return ok

    print(f"{BOLD}Claude Telemetry self-test{RESET}\n")

    check("collector process running",
          lambda: (collector_running() or bool(fallback_running()),
                   config.CONTAINER_NAME if collector_running() else "fallback"))
    check("health endpoint responds", lambda: (health_ok(), f"port {config.HEALTH_PORT}"))
    # The fallback receiver speaks http/json only, so gRPC is not a failure
    # there - it is the documented shape of that mode.
    if fallback_running() and not collector_running():
        print(f"  [{_c('SKIP', DIM)}] OTLP gRPC port reachable"
              f"  {DIM}fallback receiver is http/json only{RESET}")
    else:
        check("OTLP gRPC port reachable",
              lambda: (port_open(config.OTLP_GRPC_PORT), f"port {config.OTLP_GRPC_PORT}"))
    check("OTLP HTTP port reachable",
          lambda: (port_open(config.OTLP_HTTP_PORT), f"port {config.OTLP_HTTP_PORT}"))

    before = raw_stats()["lines"]
    session = build_demo_session(str(config.ROOT))

    def send():
        res = session.send(port=config.OTLP_HTTP_PORT)
        return all(v == 200 for v in res.values()), str(res)
    if not check("synthetic OTLP event accepted", send):
        print(_c("\ncannot continue without a running collector", RED))
        return 1

    def landed():
        for _ in range(20):
            if raw_stats()["lines"] > before:
                return True, f"raw grew from {before} lines"
            time.sleep(1)
        return False, "no new raw lines after 20s"
    check("event landed in raw storage", landed)

    conn = db.connect()

    def normalised():
        ingest.analyse(conn)
        n = db.scalar(conn, "SELECT COUNT(*) FROM events WHERE session_id=?",
                      (session.session_id,))
        return n > 0, f"{n} events normalised for the test session"
    check("normalization works", normalised)

    def tables():
        checks = {
            "api_calls": "SELECT COUNT(*) FROM api_calls WHERE session_id=?",
            "tool_calls": "SELECT COUNT(*) FROM tool_calls WHERE session_id=?",
            "file_activity": "SELECT COUNT(*) FROM file_activity WHERE session_id=?",
            "bash_activity": "SELECT COUNT(*) FROM bash_activity WHERE session_id=?",
            "skill_calls": "SELECT COUNT(*) FROM skill_calls WHERE session_id=?",
            "subagent_activity": "SELECT COUNT(*) FROM subagent_activity WHERE session_id=?",
            "errors": "SELECT COUNT(*) FROM errors WHERE session_id=?",
            "metric_points": "SELECT COUNT(*) FROM metric_points WHERE session_id=?",
            "spans": "SELECT COUNT(*) FROM spans WHERE session_id=?",
        }
        counts = {k: db.scalar(conn, v, (session.session_id,)) for k, v in checks.items()}
        empty = [k for k, v in counts.items() if not v]
        return not empty, ("all populated: " + ", ".join(f"{k}={v}" for k, v in counts.items())
                           if not empty else f"empty: {', '.join(empty)}")
    check("every normalized table populated", tables)

    def privacy():
        # Secrets must be redacted in every posture. File content is only
        # expected to be absent when content storage is switched off.
        leaked = db.scalar(conn, "SELECT COUNT(*) FROM bash_activity WHERE command LIKE ?",
                           ("%sk-ant-secret%",))
        content = db.scalar(conn, "SELECT COUNT(*) FROM tool_calls WHERE params_json LIKE ?",
                            ("%must never be stored%",))
        if leaked:
            return False, f"SECRET LEAKED into {leaked} row(s)"
        if config.STORE_TOOL_CONTENT:
            return True, ("secrets redacted; file content stored as configured "
                          f"({content} row(s)) - ./telemetry config privacy to review")
        return (content == 0,
                "secrets redacted and no file contents stored"
                if content == 0 else f"file content leaked into {content} row(s)")
    check("privacy filters applied", privacy)

    def project_mapping():
        pid = db.scalar(conn, "SELECT project_id FROM sessions WHERE session_id=?",
                        (session.session_id,), default=None)
        return (bool(pid) and not str(pid).startswith("unknown"),
                f"session mapped to {pid}")
    check("session mapped to a git project", project_mapping)

    def html():
        path = report.write(conn)
        text = path.read_text()
        return (path.exists() and len(text) > 4000 and "</html>" in text,
                f"{path} ({len(text):,} bytes)")
    check("HTML report renders", html)

    # The test session is real data in every respect, so leaving it behind
    # would quietly skew every number the database reports.
    if args.keep:
        print(f"\n{DIM}--keep: the synthetic test session is still in your "
              f"database{RESET}")
    else:
        print()
        purge_synthetic(conn)
    conn.close()
    failed = [n for n, ok, _ in results if not ok]
    print()
    if failed:
        print(_c(f"{len(failed)} check(s) failed: {', '.join(failed)}", RED))
        return 1
    print(_c(f"all {len(results)} checks passed", GREEN))
    return 0


def purge_synthetic(conn) -> int:
    """Delete synthetic sessions from the normalized DB (raw files are untouched)."""
    sids = [r["session_id"] for r in db.q(
        conn, "SELECT session_id FROM sessions WHERE user_id='synthetic-user-0001'")]
    if not sids:
        print("no synthetic sessions found")
        return 0
    marks = ",".join("?" for _ in sids)
    db.purge_sessions(conn, sids, reason="synthetic")
    conn.execute("UPDATE git_activity SET session_id=NULL, attribution='none'"
                 f" WHERE session_id IN ({marks})", sids)
    # Drop projects that exist only because of the purged sessions.
    orphans = [r["project_id"] for r in db.q(conn, """
        SELECT project_id FROM projects
         WHERE project_id NOT LIKE 'unknown:%'
           AND project_id NOT IN (SELECT project_id FROM sessions WHERE project_id IS NOT NULL)
           AND project_id NOT IN (SELECT project_id FROM file_activity WHERE project_id IS NOT NULL)
    """)]
    if orphans:
        om = ",".join("?" for _ in orphans)
        for t in ("git_commit_files", "git_activity", "projects"):
            conn.execute(f"DELETE FROM {t} WHERE project_id IN ({om})", orphans)
    conn.commit()
    print(f"purged {len(sids)} synthetic session(s) from the database")
    if orphans:
        print(f"removed {len(orphans)} project(s) left with no activity")
    print(f"{DIM}raw files under data/raw/ are unchanged; re-running analyse will not "
          f"re-import them because the ingest cursor has already passed them.{RESET}")
    return 0


def cmd_env(args) -> int:
    if not config.ENV_FILE.exists():
        print(_c(f"{config.ENV_FILE} does not exist - run ./setup.sh", YELLOW))
        return 1
    print(config.ENV_FILE.read_text())
    return 0


# ------------------------------------------------------------ composites --

def _ask(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _mcp_command() -> list[str]:
    """How another program should launch this server."""
    return [str(config.ROOT / "telemetry"), "mcp"]


def cmd_mcp(args) -> int:
    """Serve the database to an MCP client on stdin/stdout.

    Not something to run by hand - Claude Code launches it. `telemetry init`
    registers it; `--register` does that on its own.
    """
    from . import mcp as mcp_server

    if args.register:
        return _register_mcp(args)
    if sys.stdin.isatty():
        print(f"{DIM}This speaks JSON-RPC on stdin/stdout and is meant to be "
              f"launched by an MCP client.{RESET}")
        print(f"Register it with Claude Code:\n  claude mcp add {config.MCP_SERVER_NAME} "
              f"--scope user -- {' '.join(_mcp_command())}")
        print(f"\n{DIM}Waiting for protocol traffic; ctrl-c to quit.{RESET}")
    try:
        return mcp_server.serve()
    except KeyboardInterrupt:
        return 0


def _register_mcp(args) -> int:
    """Add this server to Claude Code's MCP configuration."""
    if not shutil.which("claude"):
        print(_c("the claude CLI is not on PATH; register it yourself with:", YELLOW))
        print(f"  claude mcp add {config.MCP_SERVER_NAME} --scope user -- "
              f"{' '.join(_mcp_command())}")
        return 1
    listed = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True)
    if f"{config.MCP_SERVER_NAME}:" in (listed.stdout or ""):
        print(f"{config.MCP_SERVER_NAME} is already registered with Claude Code")
        return 0
    cmd = ["claude", "mcp", "add", config.MCP_SERVER_NAME, "--scope", "user", "--",
           *_mcp_command()]
    print(f"{BOLD}This will run:{RESET}\n  {' '.join(cmd)}")
    print("Claude Code sessions will then be able to query the database directly.")
    if not _ask("\nProceed?", getattr(args, "yes", False)):
        print("skipped")
        return 1
    res = subprocess.run(cmd)
    if res.returncode == 0:
        print(_c("registered", GREEN) + " - it appears in new Claude Code sessions")
    return res.returncode


# --- enrichment: the passes that cost money ---------------------------------

# Order matters: descriptions make everything downstream easier to read, and
# turn labels are what the diagnosis reasons over.
ENRICHMENTS: dict[str, str] = {
    "describe": "one line per session saying what it was trying to do",
    "classify": "label turns as correction / steering / normal",
    "corrections": "trace each correction to a cause and a fix",
    "diagnose": "what went wrong in a session and what to change",
    "skills": "skills that should have fired and did not",
    "docs": "profile the knowledge base and judge per-session gaps",
}


def _enrich_args(args, **overrides):
    """One namespace per pass, with every flag those passes read defaulted.

    The passes were written as their own commands and still are underneath;
    this is the shared front door, not a rewrite of six analyses.
    """
    base = dict(model=args.model, limit=args.limit, dry_run=args.dry_run,
                yes=args.yes, report_only=args.report_only, redo=args.redo,
                session=args.session, batch=20, apply_only=False, signals=False,
                root=args.root, pattern="*.md", exclude=None, scan=False,
                titles_only=args.titles_only, gaps=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def cmd_enrich(args) -> int:
    """Run the model-backed passes. Everything here sends data to Anthropic."""
    wanted = args.what or [k for k in ENRICHMENTS if k != "docs" or args.root]
    unknown = [w for w in wanted if w not in ENRICHMENTS]
    if unknown:
        print(_c(f"unknown pass: {', '.join(unknown)}", RED))
        print(f"choose from: {', '.join(ENRICHMENTS)}")
        return 1

    runners = {
        "describe": (cmd_describe, {}),
        # classify has no --report-only of its own; re-applying the cached
        # labels is the same idea, and calls no model.
        "classify": (cmd_classify, {"apply_only": args.report_only}),
        "corrections": (cmd_corrections, {}),
        "diagnose": (cmd_diagnose, {}),
        "skills": (cmd_audit_skills, {}),
        "docs": (cmd_docs, {"gaps": True, "scan": bool(args.root)}),
    }
    print(f"{BOLD}Enrich{RESET}  {', '.join(wanted)}")
    print(f"{DIM}Each pass asks before it sends anything, and caches what it "
          f"gets back.{RESET}")
    failed = []
    for name in wanted:
        fn, overrides = runners[name]
        print(f"\n{BOLD}── {name}{RESET}  {DIM}{ENRICHMENTS[name]}{RESET}")
        try:
            if fn(_enrich_args(args, **overrides)):
                failed.append(name)
        except KeyboardInterrupt:
            print(_c("\ninterrupted", YELLOW))
            return 1
    if failed:
        print(f"\n{DIM}skipped or aborted: {', '.join(failed)}{RESET}")
    return 0


# --- first run --------------------------------------------------------------

def cmd_init(args) -> int:
    """Guided first run: settings, hooks, collector, MCP, first analyse.

    Every step that writes outside this directory asks first, and every step
    is independently re-runnable, so stopping half way is safe.
    """
    print(f"{BOLD}Claude Telemetry — setup{RESET}\n")

    if not config.ENV_FILE.exists():
        print(f"{BOLD}1. Telemetry settings{RESET}")
        print(f"Writes {config.ENV_FILE}, which decides what Claude Code exports.")
        if not _ask("Run setup?", args.yes):
            print("aborted - nothing written")
            return 1
        res = subprocess.run(["bash", str(config.ROOT / "setup.sh")])
        if res.returncode != 0:
            return res.returncode
    else:
        print(f"{BOLD}1. Telemetry settings{RESET}  {DIM}{config.ENV_FILE} "
              f"already written{RESET}")

    print(f"\n{BOLD}2. Enable telemetry for every Claude Code session{RESET}")
    cmd_install_env(argparse.Namespace(yes=args.yes, settings=None))

    print(f"\n{BOLD}3. Session hooks{RESET}")
    cmd_install_hooks(argparse.Namespace(yes=args.yes, settings=None))

    print(f"\n{BOLD}4. Collector{RESET}")
    if collector_running() or fallback_running():
        print("already running")
    else:
        cmd_start(argparse.Namespace(no_docker=args.no_docker))

    print(f"\n{BOLD}5. Query the database from inside Claude Code{RESET}")
    _register_mcp(args)

    print(f"\n{BOLD}6. First analyse{RESET}")
    cmd_analyse(argparse.Namespace())

    print(f"\n{_c('ready', GREEN)} - `./telemetry run` opens the browser. "
          f"New sessions are captured from now on.")
    return 0


# --- configuration ----------------------------------------------------------

def cmd_uninstall(args) -> int:
    """Take telemetry back out of Claude Code's settings."""
    ns = argparse.Namespace(settings=args.settings)
    rc = cmd_uninstall_env(ns) or 0
    rc = cmd_uninstall_hooks(ns) or rc
    if shutil.which("claude"):
        subprocess.run(["claude", "mcp", "remove", config.MCP_SERVER_NAME, "--scope", "user"],
                       capture_output=True)
        print("removed the MCP server registration")
    print(f"{DIM}The collector is untouched - ./telemetry stop shuts it down. "
          f"Data in data/ is left alone.{RESET}")
    return rc


# -------------------------------------------------------------------- parser --

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="telemetry",
        description="Capture and analyse Claude Code OpenTelemetry data locally.")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    s = sub.add_parser("init", help="guided first run: settings, hooks, collector, MCP")
    s.add_argument("--yes", action="store_true", help="accept every step")
    s.add_argument("--no-docker", action="store_true",
                   help="use the built-in receiver instead of Docker")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("run", help="browse the database (the default)")
    s.add_argument("--no-follow", action="store_true",
                   help="open on stored data and leave it there, analysing nothing")
    s.add_argument("--static", action="store_true",
                   help="print a static colour preview instead of running it")
    s.add_argument("--session", help="session to show in the static preview")
    s.add_argument("--width", type=int)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("start", help="start the local collector")
    s.add_argument("--no-docker", action="store_true",
                   help="use the built-in http/json receiver instead of Docker")
    s.set_defaults(func=cmd_start)

    sub.add_parser("stop", help="stop the collector").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="is it running, and are events arriving?").set_defaults(
        func=cmd_status)
    sub.add_parser("analyse", help="ingest new raw telemetry into the database").set_defaults(
        func=cmd_analyse)

    s = sub.add_parser("enrich",
                       help="the model-backed passes: descriptions, labels, "
                            "diagnoses, skill audit, doc gaps (costs money)")
    s.add_argument("--what", action="append", metavar="PASS",
                   help=f"repeatable; one of {', '.join(ENRICHMENTS)}. "
                        f"Default: everything except docs.")
    s.add_argument("--session", help="restrict to one session id or prefix")
    s.add_argument("--model", default="haiku")
    s.add_argument("--limit", type=int)
    s.add_argument("--root", help="knowledge base directory, for the docs pass")
    s.add_argument("--titles-only", action="store_true",
                   help="docs: send no document content, only paths and titles")
    s.add_argument("--dry-run", action="store_true",
                   help="show what would be sent, send nothing")
    s.add_argument("--redo", action="store_true",
                   help="discard cached results and judge again")
    s.add_argument("--report-only", action="store_true",
                   help="print what is already cached, call no model")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_enrich)

    s = sub.add_parser("report", help="generate the local HTML report")
    s.add_argument("--view", choices=["full", "sessions"], default="full",
                   help="'full' is the complete telemetry report; 'sessions' is the "
                        "focused review - one card per session, with cost, output, "
                        "effort, skills and MCP folded in")
    s.add_argument("-o", "--output")
    s.add_argument("--open", action="store_true", help="open it afterwards")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("mcp", help="serve the database to Claude Code over MCP")
    s.add_argument("--register", action="store_true",
                   help="add this server to Claude Code and exit")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser("sql", help="run a read-only SQL query against the database")
    s.add_argument("query")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sql)

    s = sub.add_parser("doctor", help="run the end-to-end self test")
    s.add_argument("--keep", action="store_true",
                   help="leave the synthetic test session in the database")
    s.set_defaults(func=cmd_doctor)

    # --- config -------------------------------------------------------------

    cfg = sub.add_parser("config", help="privacy, ignores, connectors, teardown")
    cfgsub = cfg.add_subparsers(dest="config_command", metavar="<what>")
    cfg.set_defaults(func=lambda a: (cfg.print_help(), 0)[1])

    s = cfgsub.add_parser("privacy", help="show or change what the database stores")
    s.add_argument("--enable-all", action="store_true",
                   help="store everything Claude Code exports")
    s.add_argument("--disable-all", action="store_true",
                   help="metadata only (the default posture)")
    for _k in ("store_content", "store_tool_content", "store_api_bodies",
               "store_span_events", "git_reconcile", "auto_describe"):
        s.add_argument(f"--{_k.replace('_', '-')}", dest=_k, choices=["on", "off"])
    s.set_defaults(func=cmd_privacy)

    s = cfgsub.add_parser("ignore", help="paths excluded from file activity")
    s.add_argument("--add", action="append", metavar="GLOB")
    s.add_argument("--remove", action="append", metavar="GLOB")
    s.add_argument("--reset", action="store_true", help="restore the defaults")
    s.set_defaults(func=cmd_ignore)

    s = cfgsub.add_parser("connect", help="pull deployments/PRs from GitHub or EAS")
    s.add_argument("provider", choices=["github", "eas", "all"], default="all",
                   nargs="?")
    s.add_argument("--project", help="project name, id or remote")
    s.add_argument("--repo", help="GitHub owner/name")
    s.add_argument("--dir", help="Expo app directory for EAS")
    s.add_argument("--branch", action="append", help="EAS branch (repeatable)")
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(func=cmd_connect)

    cfgsub.add_parser("env", help="print the generated telemetry environment file"
                      ).set_defaults(func=cmd_env)

    s = cfgsub.add_parser("uninstall",
                          help="remove the env block, hooks and MCP registration")
    s.add_argument("--settings")
    s.set_defaults(func=cmd_uninstall)

    # Claude Code calls this on every session start; not for humans, and
    # listed nowhere - argparse hides a subparser added without `help`.
    s = sub.add_parser("session-hook")
    s.add_argument("--phase", choices=["start", "end"], default="start")
    s.add_argument("--session-id")
    s.add_argument("--cwd")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_session_hook)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    # Bare `telemetry` is the browser: the thing you want nine times out of ten.
    if not argv:
        argv = ["run"]
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    config.ensure_dirs()
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
