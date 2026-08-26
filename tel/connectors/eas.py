"""Expo / EAS feed: over-the-air updates, which are how the mobile app ships.

`eas update:list` reports the commit *message* rather than a SHA, so updates
are matched back to a commit by subject. That match is exact or absent - no
fuzzy guessing - and unmatched updates are still recorded, just without a SHA.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .. import db
from ._util import run_json, ConnectorError

# `"feat(atlas-video): ..." (1 day ago by alexr)`
_MESSAGE = re.compile(r'^"(?P<subject>.*)"\s*\(', re.S)


def _subject(message: str | None) -> str | None:
    if not message:
        return None
    m = _MESSAGE.match(message.strip())
    return (m.group("subject").strip() if m else message.strip()) or None


def list_branches(app_dir: str, limit: int = 20) -> list[str]:
    try:
        data = run_json(["eas", "branch:list", "--json", "--non-interactive",
                         "--limit", str(limit)], cwd=app_dir, timeout=180)
    except ConnectorError:
        return []
    names = []
    for entry in data if isinstance(data, list) else [data]:
        name = entry.get("name") if isinstance(entry, dict) else None
        if name:
            names.append(name)
    return names


# eas-cli rejects --limit above 50 outright, so clamp rather than let the
# whole collection fail on an argument the caller could not have known about.
MAX_LIMIT = 50


def collect(conn, project_id: str, app_dir: str, branches: list[str] | None = None,
            limit: int = 50, service: str | None = None) -> dict:
    counts = {"deployments": 0, "matched_to_commit": 0, "branches": 0}
    errors: list[str] = []
    # eas-cli does not report which app an update belongs to, so name it after
    # the directory it was collected from. Callers that know better pass it.
    service = service or Path(app_dir).name or "app"
    limit = max(1, min(int(limit), MAX_LIMIT))
    branches = branches or list_branches(app_dir) or ["production", "staging"]

    # Commit subject -> sha, so an update can be tied to the work that made it.
    by_subject = {
        (r["subject"] or "").strip(): (r["commit_sha"], r["committed_at"])
        for r in db.q(conn, "SELECT subject, commit_sha, committed_at"
                            " FROM git_activity WHERE subject IS NOT NULL")
    }

    for branch in branches:
        try:
            data = run_json(["eas", "update:list", "--branch", branch,
                             "--limit", str(limit), "--json",
                             "--non-interactive"], cwd=app_dir, timeout=240)
        except ConnectorError as exc:
            errors.append(f"{branch}: {exc}")
            continue
        counts["branches"] += 1
        page = (data or {}).get("currentPage") or []
        for u in page:
            subject = _subject(u.get("message"))
            sha, commit_ts = by_subject.get(subject, (None, None)) if subject else (None, None)
            if sha:
                counts["matched_to_commit"] += 1
            if db.insert_ignore(conn, "deployments", {
                "deployment_id": f"eas:{u.get('group')}",
                "provider": "eas",
                "project_id": project_id,
                "service": service,
                "environment": _env_bucket(branch),
                "status": "published",
                # update:list reports no timestamp, so use the commit's.
                "created_at": u.get("createdAt") or commit_ts,
                "commit_sha": sha,
                "branch": branch,
                "version": u.get("runtimeVersion"),
                "url": u.get("manifestPermalink"),
                "raw_json": json.dumps(u),
            }):
                counts["deployments"] += 1
    conn.commit()
    if errors:
        # Surface failures rather than reporting a quiet zero.
        counts["errors"] = "; ".join(errors)
    return counts


def _env_bucket(branch: str) -> str:
    low = (branch or "").lower()
    for key in ("production", "staging", "preview", "development"):
        if key in low:
            return key
    return branch or "unknown"
