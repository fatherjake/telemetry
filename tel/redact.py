"""Secret redaction and content minimisation.

Two independent jobs:

1. ``scrub()`` removes things that look like credentials from any string we
   are about to persist (bash commands, error messages, remote URLs).
2. ``filter_tool_params()`` reduces a tool's argument JSON to a metadata-only
   allowlist, so file *paths* are kept but file *contents* never are.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

REDACTED = "[REDACTED]"

# Ordered, targeted patterns. Deliberately conservative: we do not blanket
# redact long hex strings, because git SHAs are 40 hex characters and are
# data we actively want to keep.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("stripe_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"(?i)\b(bearer|authorization\s*[:=]\s*bearer)\s+[A-Za-z0-9._\-]{12,}")),
    # key=value / key: value where the key name implies a secret
    ("keyword_assign", re.compile(
        r"(?i)\b((?:api[_-]?key|apikey|secret|password|passwd|pwd|token|access[_-]?key"
        r"|private[_-]?key|client[_-]?secret|auth[_-]?token|session[_-]?key)"
        r"[\"']?\s*[:=]\s*)[\"']?([^\s\"',;)]{4,})")),
    # credentials embedded in URLs
    ("url_credentials", re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@")),
]


def scrub(text: Any, _seen: set | None = None) -> Any:
    """Replace credential-looking substrings in ``text``."""
    if text is None:
        return None
    if not isinstance(text, str):
        return text
    out = text
    for name, pat in _PATTERNS:
        if name == "keyword_assign":
            out = pat.sub(lambda m: m.group(1) + REDACTED, out)
        elif name == "url_credentials":
            out = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}:{REDACTED}@", out)
        else:
            out = pat.sub(REDACTED, out)
    return out


def scrub_deep(obj: Any) -> Any:
    """Recursively scrub every string in a JSON-ish structure."""
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, list):
        return [scrub_deep(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub_deep(v) for k, v in obj.items()}
    return obj


def scrub_remote_url(url: str | None) -> str | None:
    """Strip credentials from a git remote URL but keep host/org/repo."""
    if not url:
        return None
    return scrub(url.strip())


# --- Tool parameter minimisation --------------------------------------------

# Keys we keep from tool arguments. Everything here is metadata: paths,
# commands, patterns, identifiers. Content-bearing keys (``content``,
# ``new_string``, ``old_string``, ``prompt``, ``edits``) are absent on purpose.
TOOL_PARAM_ALLOWLIST = {
    "file_path", "filePath", "path", "notebook_path", "notebookPath",
    "command", "bash_command", "full_command",
    "pattern", "glob", "type", "output_mode", "head_limit", "-n", "-i",
    "url", "query", "domain", "allowed_domains", "blocked_domains",
    "skill_name", "skill", "subagent_type", "agent_type", "agentType",
    "mcp_server_name", "mcp_tool_name", "server_name", "tool_name",
    "description", "offset", "limit", "timeout", "run_in_background",
    "replace_all", "args", "isolation", "model", "effort", "label", "phase",
    "shell_id", "filter", "plan", "todos",
}

# Keys that are known to carry file or message content; always dropped unless
# TELEMETRY_STORE_TOOL_CONTENT is on, and never included in the allowlist above.
CONTENT_KEYS = {
    "content", "new_string", "old_string", "edits", "prompt", "text",
    "new_source", "old_source", "body", "message", "response", "file_text",
}


def filter_tool_params(params: Any, store_content: bool = False) -> tuple[Any, list[str]]:
    """Reduce tool arguments to metadata.

    Returns ``(filtered, dropped_keys)``. ``args`` and ``plan``/``todos`` are
    truncated rather than kept whole because they can be long free text.
    """
    dropped: list[str] = []
    if params is None:
        return None, dropped
    if not isinstance(params, dict):
        return scrub_deep(params) if store_content else None, dropped

    out: dict = {}
    for k, v in params.items():
        if store_content:
            out[k] = scrub_deep(v)
            continue
        if k in CONTENT_KEYS or k not in TOOL_PARAM_ALLOWLIST:
            dropped.append(k)
            continue
        if isinstance(v, (dict, list)):
            # Nested structures may hide content; keep only a size marker.
            out[k] = f"<{type(v).__name__} len={len(v)}>"
        elif isinstance(v, str) and len(v) > 2048:
            out[k] = scrub(v[:2048]) + "…[truncated]"
        else:
            out[k] = scrub_deep(v)
    return out, dropped


def hash_text(text: str | None) -> str | None:
    """Stable short hash, used to correlate repeated commands without storing them twice."""
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
