"""GitHub feed: pull requests, CI runs, deployments and reverts.

Uses the already-authenticated `gh` CLI rather than handling tokens.
Deployments are the important part: they carry a commit SHA and an
environment, which is what turns "committed" into "reached users".
"""
from __future__ import annotations

import json

from .. import db
from ._util import run_json, ConnectorError


def repo_from_remote(remote_normalized: str | None) -> str | None:
    """`github.com-work/acme/atlas` -> `acme/atlas`.

    SSH host aliases mean the first segment is not always `github.com`, so
    take the last two segments rather than stripping a known host.
    """
    if not remote_normalized:
        return None
    parts = [p for p in remote_normalized.split("/") if p]
    if len(parts) < 2:
        return None
    return "/".join(parts[-2:])


def collect(conn, project_id: str, repo: str, limit: int = 100) -> dict:
    counts = {"pull_requests": 0, "ci_runs": 0, "deployments": 0}

    # --- pull requests ---
    try:
        prs = run_json([
            "gh", "pr", "list", "--repo", repo, "--state", "all",
            "--limit", str(limit), "--json",
            "number,title,state,mergedAt,mergeCommit,additions,deletions,"
            "changedFiles,author,url,createdAt",
        ])
    except ConnectorError:
        prs = []
    for pr in prs or []:
        if db.insert_ignore(conn, "pull_requests", {
            "pr_id": f"{repo}#{pr.get('number')}",
            "project_id": project_id,
            "number": pr.get("number"),
            "title": pr.get("title"),
            "state": (pr.get("state") or "").lower(),
            "author": (pr.get("author") or {}).get("login"),
            "created_at": pr.get("createdAt"),
            "merged_at": pr.get("mergedAt"),
            "merge_commit": (pr.get("mergeCommit") or {}).get("oid"),
            "additions": pr.get("additions"),
            "deletions": pr.get("deletions"),
            "changed_files": pr.get("changedFiles"),
            "url": pr.get("url"),
            "raw_json": json.dumps(pr),
        }):
            counts["pull_requests"] += 1

    # --- CI runs ---
    try:
        runs = run_json([
            "gh", "run", "list", "--repo", repo, "--limit", str(limit),
            "--json", "databaseId,headSha,name,status,conclusion,startedAt,"
                      "updatedAt,url",
        ])
    except ConnectorError:
        runs = []
    for r in runs or []:
        if db.insert_ignore(conn, "ci_runs", {
            "run_id": f"{repo}:{r.get('databaseId')}",
            "project_id": project_id,
            "commit_sha": r.get("headSha"),
            "workflow": r.get("name"),
            "status": r.get("status"),
            "conclusion": r.get("conclusion"),
            "started_at": r.get("startedAt"),
            "finished_at": r.get("updatedAt"),
            "url": r.get("url"),
            "raw_json": json.dumps(r),
        }):
            counts["ci_runs"] += 1

    # --- deployments ---
    try:
        deploys = run_json([
            "gh", "api", f"repos/{repo}/deployments",
            "--paginate", "-X", "GET", "-F", "per_page=100",
        ])
    except ConnectorError:
        deploys = []
    for d in (deploys or [])[:limit * 2]:
        env = d.get("environment") or ""
        if db.insert_ignore(conn, "deployments", {
            "deployment_id": f"github:{d.get('id')}",
            "provider": "github",
            "project_id": project_id,
            "service": (env.split("/")[0].strip() or None) if "/" in env else None,
            "environment": _env_bucket(env),
            "status": d.get("task"),
            "created_at": d.get("created_at"),
            "finished_at": d.get("updated_at"),
            "commit_sha": d.get("sha"),
            "branch": d.get("ref"),
            "url": d.get("url"),
            "raw_json": json.dumps(d),
        }):
            counts["deployments"] += 1

    conn.commit()
    return counts


def _env_bucket(env: str) -> str:
    """`UV / production` -> `production`; keep anything unrecognised verbatim."""
    low = (env or "").lower()
    for key in ("production", "staging", "preview", "development"):
        if key in low:
            return key
    return env or "unknown"
