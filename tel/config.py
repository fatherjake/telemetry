"""Paths and runtime configuration.

Everything is rooted at the project directory; nothing is written outside it
except when the user explicitly runs the hook installer.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("TELEMETRY_DATA_DIR", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
DB_PATH = Path(os.environ.get("TELEMETRY_DB", DATA_DIR / "telemetry.db"))
# Locally collected session context (not OTLP). Append-only, and read back
# by `analyse`, so the database stays fully rebuildable from files on disk.
SESSION_CONTEXT_FILE = DATA_DIR / "session_context.jsonl"
VAR_DIR = ROOT / "var"
REPORT_DIR = Path(os.environ.get("TELEMETRY_REPORT_DIR", ROOT / "reports"))
ENV_FILE = ROOT / ".env.telemetry"
COMPOSE_FILE = ROOT / "docker-compose.yml"

# Collector endpoints (loopback only by design).
OTLP_GRPC_PORT = int(os.environ.get("TELEMETRY_OTLP_GRPC_PORT", "4317"))
OTLP_HTTP_PORT = int(os.environ.get("TELEMETRY_OTLP_HTTP_PORT", "4318"))
HEALTH_PORT = int(os.environ.get("TELEMETRY_HEALTH_PORT", "13133"))

CONTAINER_NAME = "claude-telemetry-collector"
# The name Claude Code registers this server under; it also prefixes every
# tool the session sees, as mcp__<name>__telemetry_*.
MCP_SERVER_NAME = "telemetry"


# Storage policy lives in a file so it applies to every `telemetry` invocation,
# not just to shells that happen to have the variables exported. Environment
# variables still win, so one-off overrides work.
POLICY_FILE = DATA_DIR / "telemetry.policy.json"
# Per-provider connector settings (repo slug, app directory, branches),
# remembered so `telemetry config connect` needs arguments only the first time.
CONNECTORS_FILE = DATA_DIR / "telemetry.connectors.json"
# Model-assigned turn labels. Append-only, outside the database, because
# they cost money to produce and a database rebuild must not destroy them.
TURN_LABELS_FILE = DATA_DIR / "turn_labels.jsonl"
# Skill-audit verdicts. Same reasoning as turn labels: paid for, so kept
# outside the database.
SKILL_AUDIT_FILE = DATA_DIR / "skill_audit.jsonl"
CORRECTION_CAUSE_FILE = DATA_DIR / "correction_cause.jsonl"
SESSION_DX_FILE = DATA_DIR / "session_diagnosis.jsonl"
SESSION_CHAT_FILE = DATA_DIR / "session_chat.jsonl"
DOC_PROFILE_FILE = DATA_DIR / "doc_profiles.jsonl"
DOC_GAP_FILE = DATA_DIR / "doc_gaps.jsonl"
NARRATIVE_FILE = DATA_DIR / "narratives.jsonl"
# Glob patterns for paths that should never count as file activity. Agent
# scratchpads and dependency trees are churn, not work on the project.
IGNORE_FILE = DATA_DIR / "telemetry.ignore"

DEFAULT_IGNORES = [
    "/tmp/*",
    "/private/tmp/*",
    "*/node_modules/*",
    "*/.git/*",
    "*/.venv/*",
    "*/__pycache__/*",
    "*/.next/*",
    "*/dist/*",
    "*/build/*",
    "*/.DS_Store",
]


def load_ignores() -> list[str]:
    """Patterns from the ignore file, or the defaults if it does not exist."""
    try:
        if IGNORE_FILE.exists():
            lines = []
            for raw in IGNORE_FILE.read_text().splitlines():
                line = raw.split("#", 1)[0].strip()
                if line:
                    lines.append(line)
            return lines
    except OSError:
        pass
    return list(DEFAULT_IGNORES)


def save_ignores(patterns: list[str]) -> None:
    ensure_dirs()
    header = ("# Paths matching these globs are excluded from file activity.\n"
              "# One glob per line; # starts a comment. Applied on every"
              " `telemetry analyse`.\n")
    IGNORE_FILE.write_text(header + "\n".join(patterns) + "\n")

_POLICY_DEFAULTS = {
    "store_content": False,
    "store_tool_content": False,
    "store_api_bodies": False,
    "store_span_events": False,
    "git_reconcile": True,
    "redact_secrets": True,
    # The one thing analyse does that leaves this machine. A database where half
    # the rows are called `b944dba4` is not readable, and a description is
    # only useful if it is already there when you look.
    "auto_describe": True,
}


def load_policy() -> dict:
    policy = dict(_POLICY_DEFAULTS)
    try:
        if POLICY_FILE.exists():
            import json as _json
            data = _json.loads(POLICY_FILE.read_text() or "{}")
            for k in policy:
                if k in data:
                    policy[k] = bool(data[k])
    except (OSError, ValueError):
        pass
    return policy


def save_policy(policy: dict) -> None:
    import json as _json
    ensure_dirs()
    merged = dict(_POLICY_DEFAULTS)
    merged.update({k: bool(v) for k, v in policy.items() if k in _POLICY_DEFAULTS})
    POLICY_FILE.write_text(_json.dumps(merged, indent=2) + "\n")


_POLICY = load_policy()


def _flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# --- Privacy switches (all default to the conservative option) ---------------

# Store prompt / assistant-response text when Claude Code exports it.
STORE_CONTENT = _flag("TELEMETRY_STORE_CONTENT", _POLICY["store_content"])

# Store the full tool argument JSON rather than the metadata-only allowlist.
STORE_TOOL_CONTENT = _flag("TELEMETRY_STORE_TOOL_CONTENT", _POLICY["store_tool_content"])

# Store raw API request/response bodies if OTEL_LOG_RAW_API_BODIES was enabled.
STORE_API_BODIES = _flag("TELEMETRY_STORE_API_BODIES", _POLICY["store_api_bodies"])

# Store span events (tool.output bodies) emitted under OTEL_LOG_TOOL_CONTENT.
STORE_SPAN_EVENTS = _flag("TELEMETRY_STORE_SPAN_EVENTS", _POLICY["store_span_events"])

# Run read-only git commands against detected repositories for reconciliation.
GIT_RECONCILE = _flag("TELEMETRY_GIT_RECONCILE", _POLICY["git_reconcile"])

# Name finished sessions from their prompts during `analyse`. This sends
# prompt text to Anthropic through the local `claude` CLI - the only step of
# the pipeline that talks to anything off this machine.
AUTO_DESCRIBE = _flag("TELEMETRY_AUTO_DESCRIBE", _POLICY["auto_describe"])

# Secret redaction. Deliberately has no "off" path in the CLI: content storage
# is a choice, leaking credentials into a database is not.
REDACT_SECRETS = True


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, VAR_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
