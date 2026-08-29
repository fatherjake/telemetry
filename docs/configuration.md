# Configuration

What Claude Code is told to export, what this project stores, and how to keep
the receiver running.

See [PRIVACY.md](../PRIVACY.md) for the full account of what is and is not
recorded.

---

## Enabling Claude Code telemetry

`telemetry install` writes an `env` block into `~/.claude/settings.json`,
backing up the existing file first. Settings cover every session however it
was launched — from an editor, a launcher or a terminal — which a shell file
does not. This is what it sets and why; every variable was checked against the
current docs, see [`docs/anthropic-telemetry-notes.md`](anthropic-telemetry-notes.md).

```
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=otlp
OTEL_TRACES_EXPORTER=otlp
CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1     # spans: file_path, full_command
OTEL_EXPORTER_OTLP_PROTOCOL=http/json     # what the receiver speaks
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_METRIC_EXPORT_INTERVAL=10000         # default 60000 feels dead
OTEL_LOGS_EXPORT_INTERVAL=5000
OTEL_TRACES_EXPORT_INTERVAL=5000
OTEL_LOG_TOOL_DETAILS=1                   # see the trade-off below
```

`--full` adds `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_ASSISTANT_RESPONSES`,
`OTEL_LOG_TOOL_CONTENT`, `OTEL_LOG_RAW_API_BODIES` and a 256 KB content
ceiling. Pair it with `telemetry config privacy --enable-all` to actually
store what arrives. Budget ~70–260 KB per API call for bodies, and watch the
size of `~/.telemetry/raw`.

`telemetry config env` shows what is installed against what would be
installed, marking every difference.

Telemetry settings are read at startup, so a session already running when you
install will not be observed. Start a new one.

**The one trade-off.** `OTEL_LOG_TOOL_DETAILS=1` is what unlocks file paths,
bash commands, skill names and subagent types. Without it Claude Code redacts
tool parameters and this project can tell you _that_ a tool ran, but nothing
about what it touched. With it, the raw archive can contain short excerpts of
file content (Claude Code truncates values at 512 characters). The normalized
database never stores them unless you ask — [`PRIVACY.md`](../PRIVACY.md) is
precise about this.

---

## Ignoring paths

Agent scratchpads and dependency trees are churn, not work on the project —
they were 22% of all recorded file activity here.

```bash
telemetry config ignore                          # show the patterns in force
telemetry config ignore --add '*/coverage/*'
telemetry config ignore --remove '*/dist/*'
telemetry config ignore --reset
```

Defaults cover `/tmp`, `/private/tmp`, `node_modules`, `.git`, `.venv`,
`__pycache__`, `.next`, `dist`, `build` and `.DS_Store`. Editing them writes
`~/.telemetry/ignore`, and the patterns are applied on every `analyse` rather
than at write time — tool-derived rows cannot be re-derived without re-reading
the raw archive, so filtering at the end means a changed pattern takes effect
whatever produced the row.

## Verifying events are arriving

```bash
telemetry status
```

```
Receiver
  process        running (pid 48213)
  OTLP 4318     open

Raw telemetry  /Users/you/.telemetry/raw
  files          3
  size           412.7 KB
  last write     2026-08-27 11:36:41  (0.4 min ago)  events arriving

Database  /Users/you/.telemetry/telemetry.db
  sessions       7
  events         1,180   metrics 402   spans 233
  api calls      96   cost $4.1233
  ...
  412.7 KB of raw telemetry not yet analysed - run telemetry analyse
```

Other checks:

```bash
telemetry doctor                          # end-to-end assertions
curl -s localhost:4318/health               # receiver health
tail -1 ~/.telemetry/raw/logs.jsonl | node -e 'process.stdin.on("data",d=>console.log(JSON.stringify(JSON.parse(d),null,2)))' | head
tail -20 ~/.telemetry/receiver.log          # the receiver's own log
claude --debug                              # Claude Code's OTLP export errors
```

Inside a Claude Code session, `/status` shows its telemetry configuration.

---

---

## Keeping the receiver running

The receiver is an ordinary detached process, so it does not survive a reboot.
On macOS a launch agent covers that. `telemetry start` exits once the receiver
is up, so this is a one-shot at login, not a daemon — no `KeepAlive`, or
launchd would respawn it forever:

```bash
TELEMETRY="$(command -v telemetry)"
cat > ~/Library/LaunchAgents/com.claude-telemetry.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.claude-telemetry</string>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$HOME/.telemetry/launchagent.log</string>
  <key>StandardErrorPath</key><string>$HOME/.telemetry/launchagent.log</string>
  <!-- launchd's PATH does not include a version-managed node. -->
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>ProgramArguments</key>
  <array><string>$TELEMETRY</string><string>start</string></array>
</dict>
</plist>
PLIST
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.claude-telemetry.plist
```

Undo with `launchctl bootout gui/$(id -u)/com.claude-telemetry` and delete the
file.
