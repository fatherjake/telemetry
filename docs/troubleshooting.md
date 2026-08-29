# Troubleshooting

**`telemetry status` says no telemetry received.**
Telemetry is read at Claude Code startup — run `telemetry install`, then
start a _new_ session. Confirm with `/status` inside Claude Code, or
`claude --debug` to see export errors.

**Receiver will not start.**
Port 4318 already in use is the common one — another collector, or Jaeger.
Set `TELEMETRY_OTLP_PORT` and re-run `telemetry install`.

**Events are rejected with a 415.**
The receiver accepts `http/json` only. `telemetry install` sets
`OTEL_EXPORTER_OTLP_PROTOCOL=http/json`; check with `telemetry config env`.

**Events arrive but `analyse` finds nothing.**
Check the signal actually landed: `wc -l ~/.telemetry/raw/*.jsonl`. If only
`metrics.jsonl` grows, `OTEL_LOGS_EXPORTER` is not set to `otlp`.

**No file paths, bash commands or skill names.**
`OTEL_LOG_TOOL_DETAILS=1` is missing.

**No spans.**
Needs both `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` and
`OTEL_TRACES_EXPORTER=otlp`.

**Everything is `(unattributed)`.**
Expected when a session did no file or bash work with absolute paths. Install
the session hooks for reliable mapping.

**A raw line failed to parse.**
The last line of a file being written can be partial. The ingester stops at the
first unparseable line and resumes there next run. `meta.last_ingest_error`
holds the detail.

**Rebuild the database.**
`rm ~/.telemetry/telemetry.db && telemetry analyse` — it rebuilds from the
raw archive and the sidecars, so nothing that cost money is lost.

**Reset everything.**
`telemetry stop && telemetry config uninstall && rm -rf ~/.telemetry`

---
