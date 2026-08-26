"""Project identification and read-only git reconciliation.

All git invocations here are read-only (`rev-parse`, `remote get-url`,
`branch --show-current`, `log`, `show`). Nothing in this module writes to a
repository or reads file contents.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from .redact import scrub_remote_url

GIT_TIMEOUT = 15


def _git(args: list[str], cwd: str | Path) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


@lru_cache(maxsize=4096)
def _nearest_dir(path: str) -> str | None:
    """Walk up to the nearest existing *directory*.

    Handles file paths, paths that have since been deleted, and paths whose
    parent is itself a file - `git -C` needs a real directory or it fails
    outright.
    """
    p = Path(path)
    while not p.is_dir():
        parent = p.parent
        if parent == p:
            return None
        p = parent
    return str(p)


@lru_cache(maxsize=1024)
def _repo_of_dir(directory: str) -> dict:
    root = _git(["rev-parse", "--show-toplevel"], directory)
    if not root:
        return {"cwd": directory, "is_git": False}
    return {
        "cwd": directory,
        "is_git": True,
        "repo_root": root,
        "remote_url": scrub_remote_url(_git(["remote", "get-url", "origin"], directory)),
    }


def nearest_dir(path: str) -> str | None:
    """The directory a path lives in, for callers that key a cache by it."""
    return _nearest_dir(path)


def repo_info(path: str) -> dict:
    """Which repository a path belongs to, if any.

    Cached per *directory*, not per path: attribution runs over thousands of
    file paths that share a handful of directories, and the answer is the same
    for all of them.

    Deliberately excludes branch, HEAD and dirtiness. Those need
    `git status`, which walks the entire working tree - on a repo holding the
    telemetry's own data that is seconds per call, and nothing about attributing
    a file to a project depends on them. Use `describe_full` when you actually
    want the live state of a checkout.
    """
    directory = _nearest_dir(path)
    if directory is None:
        return {"cwd": path, "is_git": False}
    return dict(_repo_of_dir(directory))   # cached; hand out a copy


def normalize_remote(url: str | None) -> str | None:
    """Reduce a remote URL to `host/org/repo` so ssh and https forms match."""
    if not url:
        return None
    u = url.strip()
    u = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", u)
    u = re.sub(r"^[^/@]+@", "", u)          # strip user@ / credentials
    u = u.replace(":", "/", 1) if "@" not in u and not u.startswith("/") else u
    u = re.sub(r"\.git/?$", "", u)
    u = u.strip("/")
    return u.lower() or None


def project_id(repo_root: str | None, remote_url: str | None, cwd: str | None = None) -> str:
    """Stable id for a project.

    Preference order: normalized remote (stable across clones) > repo root
    path > working directory. Prefixed so the basis is visible in the id.
    """
    norm = normalize_remote(remote_url)
    if norm:
        basis, seed = "remote", norm
    elif repo_root:
        basis, seed = "root", str(repo_root)
    elif cwd:
        basis, seed = "dir", str(cwd)
    else:
        basis, seed = "unknown", "unknown"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    return f"{basis}:{digest}"


def project_name(repo_root: str | None, remote_url: str | None, cwd: str | None = None) -> str:
    norm = normalize_remote(remote_url)
    if norm:
        return "/".join(norm.split("/")[-2:])
    for candidate in (repo_root, cwd):
        if candidate:
            return Path(candidate).name
    return "unknown"


def describe(path: str) -> dict:
    """Project descriptor for a filesystem path. Cheap and cached."""
    info = repo_info(path)
    root = info.get("repo_root")
    remote = info.get("remote_url")
    cwd = info.get("cwd")
    return {
        "project_id": project_id(root, remote, cwd),
        "project_name": project_name(root, remote, cwd),
        "repo_root": root,
        "remote_url": remote,
        "remote_normalized": normalize_remote(remote),
        "is_git": 1 if info.get("is_git") else 0,
        "cwd": cwd,
    }


def describe_full(path: str) -> dict:
    """`describe` plus the live state of the checkout - branch, HEAD, dirty.

    Three extra git invocations, one of which walks the working tree, so this
    is for the handful of places that record what a session was sitting on,
    never for bulk attribution.
    """
    desc = describe(path)
    directory = _nearest_dir(path) if desc.get("is_git") else None
    if directory:
        desc.update({
            "branch": _git(["branch", "--show-current"], directory) or None,
            "head_sha": _git(["rev-parse", "HEAD"], directory) or None,
            "is_dirty": 1 if _git(["status", "--porcelain"], directory) else 0,
        })
    return desc


UNKNOWN_PROJECT = {
    "project_id": "unknown:000000000000",
    "project_name": "(unattributed)",
    "repo_root": None,
    "remote_url": None,
    "remote_normalized": None,
    "is_git": 0,
    "detection_method": "none",
}


# --- commit reconciliation ---------------------------------------------------

def commits_since(repo_root: str, since_iso: str, until_iso: str | None = None) -> list[dict]:
    """Read-only listing of commits in a time window, with per-file stats."""
    # --all would sweep in refs/stash, whose entries are commits but not work
    # anyone authored. --branches --remotes --tags covers the real history.
    args = ["log", "--branches", "--remotes", "--tags", f"--since={since_iso}",
            "--date=iso-strict",
            "--pretty=format:%x1e%H%x1f%aI%x1f%an%x1f%ae%x1f%s",
            "--numstat"]
    if until_iso:
        args.insert(2, f"--until={until_iso}")
    out = _git(args, repo_root)
    if not out:
        return []

    commits = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        header, _, rest = chunk.partition("\n")
        parts = header.split("\x1f")
        if len(parts) < 5:
            continue
        sha, when, an, ae, subject = parts[:5]
        files, ins, dels = [], 0, 0
        for line in rest.splitlines():
            line = line.strip()
            if not line:
                continue
            cells = line.split("\t")
            if len(cells) < 3:
                continue
            a, d, path = cells[0], cells[1], cells[2]
            ai = int(a) if a.isdigit() else 0
            di = int(d) if d.isdigit() else 0
            ins += ai
            dels += di
            files.append({"path": path, "insertions": ai, "deletions": di})
        commits.append({
            "commit_sha": sha, "committed_at": to_utc_iso(when), "author_name": an,
            "author_email": ae, "subject": subject, "files": files,
            "insertions": ins, "deletions": dels, "files_changed": len(files),
        })
    return commits


def to_utc_iso(stamp: str | None) -> str | None:
    """Git reports author dates in local time with an offset; session
    timestamps are UTC with a Z. Comparing the two as strings silently breaks
    commit-to-session attribution, so normalise to UTC here."""
    if not stamp:
        return None
    try:
        dt = _dt.datetime.fromisoformat(stamp.strip())
    except ValueError:
        return stamp
    if dt.tzinfo is None:
        return dt.isoformat(timespec="seconds") + "Z"
    return (dt.astimezone(_dt.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def commit_change_types(repo_root: str, sha: str) -> dict[str, str]:
    """Map path -> change type (A/M/D/R/C) for one commit."""
    out = _git(["show", "--name-status", "--pretty=format:", "-m", "--first-parent", sha], repo_root)
    if not out:
        return {}
    result = {}
    for line in out.splitlines():
        cells = line.strip().split("\t")
        if len(cells) >= 2 and cells[0]:
            result[cells[-1]] = cells[0][0]
    return result


def file_first_added(repo_root: str, path: str) -> str | None:
    """ISO timestamp of the commit that first added `path`, if any."""
    out = _git(["log", "--diff-filter=A", "--follow", "--date=iso-strict",
                "--pretty=format:%aI", "-1", "--", path], repo_root)
    return out.splitlines()[0] if out else None


def is_tracked(repo_root: str, path: str) -> bool:
    return _git(["ls-files", "--error-unmatch", path], repo_root) is not None
