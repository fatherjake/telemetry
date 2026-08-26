"""What skills and MCP servers are *installed*, as opposed to used.

Usage comes from telemetry. This module supplies the denominator, by reading
the same configuration Claude Code reads. Nothing here executes a skill or
starts a server; it only reads names, paths and descriptions.

Secrets are never stored: MCP `args` and `env` frequently carry tokens, so
only the command's program name is kept.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from . import db
from .redact import scrub

SKILL_FILE = "SKILL.md"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _frontmatter(path: Path) -> dict:
    """Minimal YAML frontmatter reader - name and description only."""
    out: dict = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    if not text.startswith("---"):
        return out
    end = text.find("\n---", 3)
    if end == -1:
        return out
    for line in text[3:end].splitlines():
        if ":" not in line or line.startswith((" ", "\t", "#")):
            continue
        k, _, v = line.partition(":")
        k = k.strip().lower()
        if k in ("name", "description"):
            out[k] = v.strip().strip('"\'')
    return out


def scan_skills(project_dirs: dict[str, str] | None = None) -> list[dict]:
    """Find skills in user, project and plugin scopes."""
    found: list[dict] = []
    seen: set[str] = set()

    def add(name, scope, path, desc, project_id=None):
        key = f"{scope}:{name}"
        if key in seen:
            return
        seen.add(key)
        found.append({"skill_id": key, "name": name, "scope": scope,
                      "project_id": project_id, "source_path": str(path),
                      "description": (desc or "")[:400], "discovered_at": _now()})

    home = Path.home()
    for d in sorted((home / ".claude" / "skills").glob("*")):
        if (d / SKILL_FILE).exists():
            fm = _frontmatter(d / SKILL_FILE)
            add(fm.get("name") or d.name, "user", d, fm.get("description"))

    plugins = home / ".claude" / "plugins"
    if plugins.exists():
        for skill_md in plugins.glob("**/skills/*/" + SKILL_FILE):
            fm = _frontmatter(skill_md)
            add(fm.get("name") or skill_md.parent.name, "plugin",
                skill_md.parent, fm.get("description"))

    for project_id, root in (project_dirs or {}).items():
        base = Path(root) / ".claude" / "skills"
        if not base.exists():
            continue
        for d in sorted(base.glob("*")):
            if (d / SKILL_FILE).exists():
                fm = _frontmatter(d / SKILL_FILE)
                add(fm.get("name") or d.name, "project", d,
                    fm.get("description"), project_id)
    return found


def _mcp_from_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace") or "{}")
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
        return data["mcpServers"]
    return {}


def scan_mcp(project_dirs: dict[str, str] | None = None) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()

    def add(name, scope, cfg, path, project_id=None):
        key = f"{scope}:{name}"
        if key in seen:
            return
        seen.add(key)
        command = cfg.get("command") if isinstance(cfg, dict) else None
        transport = (cfg.get("type") or cfg.get("transport")
                     if isinstance(cfg, dict) else None)
        if not transport:
            transport = "http" if (isinstance(cfg, dict) and cfg.get("url")) else "stdio"
        found.append({
            "mcp_id": key, "name": name, "scope": scope,
            "project_id": project_id, "config_path": str(path),
            "transport": transport,
            # Only the program name: args and env routinely carry tokens.
            "command": scrub(str(command).split("/")[-1]) if command else None,
            "discovered_at": _now(),
        })

    home = Path.home()
    for path in (home / ".claude.json", home / ".claude" / "settings.json"):
        for name, cfg in _mcp_from_file(path).items():
            add(name, "user", cfg, path)

    for project_id, root in (project_dirs or {}).items():
        for fname in (".mcp.json", ".claude/settings.json"):
            path = Path(root) / fname
            for name, cfg in _mcp_from_file(path).items():
                add(name, "project", cfg, path, project_id)
    return found


def refresh(conn) -> dict:
    """Rescan and store. Inventory is current state, so it is replaced."""
    project_dirs = {
        r["project_id"]: r["repo_root"]
        for r in db.q(conn, "SELECT project_id, repo_root FROM projects"
                            " WHERE repo_root IS NOT NULL")
    }
    skills = scan_skills(project_dirs)
    servers = scan_mcp(project_dirs)
    conn.execute("DELETE FROM skill_inventory")
    conn.execute("DELETE FROM mcp_inventory")
    for row in skills:
        db.insert_ignore(conn, "skill_inventory", row)
    for row in servers:
        db.insert_ignore(conn, "mcp_inventory", row)
    conn.commit()
    return {"skills": len(skills), "mcp_servers": len(servers),
            "projects_scanned": len(project_dirs)}
