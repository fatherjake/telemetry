"""Model-assisted classification of turns as steering / correction / normal.

The length heuristic conflates "short" with "low-information" - `promote to
production` is 21 characters and ships a release. A small model reads the
prompt in the context of the previous one and labels it properly.

**This is the only part of the project that leaves the machine.** Prompt text
is sent to Anthropic's API through the already-authenticated `claude` CLI. It
is opt-in, never runs as part of `analyse`, and asks before sending.

Results are cached by prompt hash, so a second run costs nothing.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import subprocess

from . import config, db

MODEL = "haiku"
BATCH = 20
LABELS = {"steering", "correction", "normal"}

SYSTEM = """You label prompts that a developer sent to an AI coding agent.

For each numbered input, choose exactly one label:

- "correction": the developer is telling the agent it got something wrong, or
  redirecting it away from what it just did. Rejections, "no", "not quite",
  "actually", pointing out a mistake, asking to undo or revert, restating a
  requirement the agent missed.
- "steering": a short nudge that continues or approves what is already
  happening. Confirmations ("yes", "go ahead"), continuations ("carry on",
  "open them"), or a small adjustment that is not a correction. A steering
  prompt only makes sense in the context of the previous turn.
- "normal": a substantive instruction or question that stands on its own.
  Length is irrelevant here: "promote to production" is normal, not steering,
  because it starts new work rather than nudging work in progress.

Use gap_seconds as evidence: a nudge follows quickly; a long gap usually means
the developer returned to start something new.

Output ONLY a JSON array, one object per input, no prose and no code fences:
[{"i": 1, "label": "normal", "confidence": "high", "why": "five words"}]"""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def prompt_hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:16]


def pending(conn, limit: int | None = None) -> list[dict]:
    """Turns with prompt text whose current classification is stale or absent."""
    rows = db.q(conn, """
        SELECT t.turn_id, t.session_id, t.seq, t.prompt_text, t.prompt_length,
               t.gap_before_s,
               (SELECT p2.prompt_text FROM turns p2
                 WHERE p2.session_id = t.session_id AND p2.seq = t.seq - 1) AS prev_text,
               c.prompt_hash AS cached_hash
          FROM turns t
          LEFT JOIN turn_classification c ON c.turn_id = t.turn_id
         WHERE t.prompt_text IS NOT NULL AND TRIM(t.prompt_text) <> ''
           AND COALESCE(t.is_system, 0) = 0
         ORDER BY t.started_at""")
    out = []
    for r in rows:
        h = prompt_hash(r["prompt_text"])
        if r["cached_hash"] == h:
            continue
        out.append({**dict(r), "hash": h})
    return out[:limit] if limit else out


def _payload(batch: list[dict]) -> str:
    items = []
    for i, t in enumerate(batch, start=1):
        items.append({
            "i": i,
            "gap_seconds": int(t["gap_before_s"]) if t["gap_before_s"] else None,
            "previous_prompt": (t["prev_text"] or "")[:160] or None,
            "prompt": (t["prompt_text"] or "")[:500],
        })
    return json.dumps(items, ensure_ascii=False, indent=1)


def _extract_json(text: str):
    """Pull the JSON array out of a reply that may carry prose or fences."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = text.find("["), text.rfind("]")
        candidate = text[start:end + 1] if start != -1 and end > start else None
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
        return data if isinstance(data, list) else None
    except ValueError:
        return None


def call_model(batch: list[dict], model: str = MODEL, timeout: int = 180) -> list | None:
    """One classification call via the authenticated claude CLI.

    Telemetry and MCP servers are switched off for the subprocess: without
    that, classifying prompts would write new sessions into the very database
    being classified, and would pay MCP startup cost for nothing.
    """
    prompt = f"{SYSTEM}\n\nClassify these {len(batch)} prompts:\n\n{_payload(batch)}"
    env = {**os.environ,
           "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
           "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "0"}
    for key in ("OTEL_METRICS_EXPORTER", "OTEL_LOGS_EXPORTER", "OTEL_TRACES_EXPORTER"):
        env[key] = "none"
    cmd = ["claude", "-p", prompt, "--model", model,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--settings", json.dumps({
               "env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "0"}, "hooks": {}})]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return _extract_json(r.stdout)


def classify(conn, model: str = MODEL, limit: int | None = None,
             batch_size: int = BATCH, progress=None) -> dict:
    todo = pending(conn, limit)
    stats = {"pending": len(todo), "classified": 0, "batches": 0, "failed": 0}
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        stats["batches"] += 1
        if progress:
            progress(stats["batches"], start + len(batch), len(todo))
        result = call_model(batch, model) or call_model(batch, model)  # one retry
        if not result:
            stats["failed"] += len(batch)
            continue
        by_index = {}
        for item in result:
            if isinstance(item, dict) and isinstance(item.get("i"), int):
                by_index[item["i"]] = item
        for i, turn in enumerate(batch, start=1):
            item = by_index.get(i)
            label = (item or {}).get("label")
            if label not in LABELS:
                stats["failed"] += 1
                continue
            record = {
                "turn_id": turn["turn_id"], "label": label,
                "confidence": (item.get("confidence") or "")[:10],
                "rationale": (item.get("why") or "")[:200],
                "model": model, "prompt_hash": turn["hash"],
                "classified_at": _now(),
            }
            _store(conn, record)
            _append(record)
            stats["classified"] += 1
        conn.commit()
    apply_labels(conn)
    return stats


def _store(conn, r: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO turn_classification"
        "(turn_id, label, confidence, rationale, model, prompt_hash,"
        " classified_at) VALUES (?,?,?,?,?,?,?)",
        (r["turn_id"], r["label"], r["confidence"], r["rationale"],
         r["model"], r["prompt_hash"], r["classified_at"]))


def _append(r: dict) -> None:
    """Mirror to an append-only file so a database rebuild cannot lose paid work."""
    config.ensure_dirs()
    with config.TURN_LABELS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(r) + "\n")


def import_cached(conn) -> int:
    """Load labels from the sidecar file back into the database."""
    path = config.TURN_LABELS_FILE
    if not path.exists():
        return 0
    n = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("turn_id") and r.get("label") in LABELS:
                _store(conn, r)
                n += 1
    conn.commit()
    return n


def apply_labels(conn) -> int:
    """Fold model labels onto turns, so every downstream query sees one answer.

    `label_source` records which turns were labelled by the model and which
    fell back to the heuristic, so the two are always distinguishable.
    """
    conn.execute("UPDATE turns SET label_source = COALESCE(label_source, 'heuristic')")
    cur = conn.execute("""
        UPDATE turns SET
            is_steering    = (SELECT c.label='steering'   FROM turn_classification c
                               WHERE c.turn_id = turns.turn_id),
            is_correction  = (SELECT c.label='correction' FROM turn_classification c
                               WHERE c.turn_id = turns.turn_id),
            correction_cue = (SELECT c.rationale FROM turn_classification c
                               WHERE c.turn_id = turns.turn_id),
            label_confidence = (SELECT c.confidence FROM turn_classification c
                                 WHERE c.turn_id = turns.turn_id),
            label_source   = 'model'
         WHERE COALESCE(is_system, 0) = 0
           AND EXISTS (SELECT 1 FROM turn_classification c
                        WHERE c.turn_id = turns.turn_id)""")
    conn.commit()
    return cur.rowcount


def agreement(conn) -> list:
    """Where the model and the heuristic disagree - the interesting rows."""
    return db.q(conn, """
        SELECT t.turn_id, t.prompt_length, t.label_confidence,
               c.label AS model_label, c.rationale,
               substr(t.prompt_text, 1, 70) AS prompt,
               CASE WHEN t.prompt_length <= 45 THEN 'steering' ELSE 'normal' END
                 AS heuristic_label
          FROM turns t JOIN turn_classification c ON c.turn_id = t.turn_id
         ORDER BY t.started_at""")
