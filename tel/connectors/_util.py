"""Shared helpers for connectors."""
from __future__ import annotations

import json
import subprocess


class ConnectorError(RuntimeError):
    pass


def run(cmd: list[str], cwd: str | None = None, timeout: int = 120) -> str:
    """Run a CLI and return stdout, raising with useful context on failure."""
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
    except FileNotFoundError:
        raise ConnectorError(f"{cmd[0]} is not on PATH")
    except subprocess.TimeoutExpired:
        raise ConnectorError(f"{' '.join(cmd[:3])} timed out after {timeout}s")
    if r.returncode != 0:
        detail = (r.stderr or r.stdout).strip().splitlines()
        raise ConnectorError(f"{' '.join(cmd[:3])} failed: "
                             f"{detail[-1] if detail else 'no output'}")
    return r.stdout


def run_json(cmd: list[str], cwd: str | None = None, timeout: int = 120):
    """Run a CLI expecting JSON, tolerating banner lines before the payload."""
    out = run(cmd, cwd=cwd, timeout=timeout)
    stripped = out.strip()
    for opener in ("{", "["):
        idx = stripped.find(opener)
        if idx == -1:
            continue
        try:
            return json.loads(stripped[idx:])
        except ValueError:
            continue
    raise ConnectorError(f"{cmd[0]} did not return JSON: {stripped[:200]}")
