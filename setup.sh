#!/usr/bin/env bash
# First-run setup for Claude Telemetry.
#
# This script does NOT modify your shell profile. It writes .env.telemetry in
# this directory, which you then `source` in whichever shell you want to be
# observed. Run ./install-shell.sh separately if you want it permanently.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PROFILE="standard"
PROTOCOL="grpc"
ENDPOINT_PORT=4317
for arg in "$@"; do
  case "$arg" in
    --minimal)  PROFILE="minimal" ;;
    --full)     PROFILE="full" ;;
    --http)     PROTOCOL="http/protobuf"; ENDPOINT_PORT=4318 ;;
    --http-json) PROTOCOL="http/json"; ENDPOINT_PORT=4318 ;;
    -h|--help)
      cat <<'USAGE'
usage: ./setup.sh [--minimal] [--http|--http-json]

  --minimal     do not enable OTEL_LOG_TOOL_DETAILS. You lose file paths,
                bash commands, skill names and subagent types, but no tool
                arguments ever reach disk.
  --full        export everything: prompts, assistant responses, tool
                input/output bodies and raw API bodies. Richest possible
                data. data/ will then contain your source code and the full
                text of your conversations. See PRIVACY.md.
  --http        use OTLP over http/protobuf on port 4318 instead of gRPC.
  --http-json   use OTLP over http/json on port 4318 (required by the
                --no-docker fallback receiver).
USAGE
      exit 0 ;;
  esac
done

say() { printf '%s\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }

say ""
say "Claude Telemetry — setup"
say "=============================="
say ""
say "Checking prerequisites:"

FAIL=0
if command -v python3 >/dev/null 2>&1; then
  ok "python3 $(python3 --version 2>&1 | awk '{print $2}') (standard library only, no pip install needed)"
else
  bad "python3 not found — required"; FAIL=1
fi

if command -v git >/dev/null 2>&1; then
  ok "git $(git --version | awk '{print $3}')"
else
  warn "git not found — project detection and commit reconciliation will be skipped"
fi

DOCKER_OK=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  ok "docker daemon running"
  DOCKER_OK=1
elif command -v docker >/dev/null 2>&1; then
  warn "docker installed but the daemon is not running"
else
  warn "docker not found"
fi
if [ "$DOCKER_OK" -eq 0 ]; then
  warn "you can still run:  ./telemetry start --no-docker   (http/json only)"
fi

if command -v claude >/dev/null 2>&1; then
  ok "claude $(claude --version 2>&1 | awk '{print $1}')"
else
  warn "claude CLI not found on PATH"
fi

[ "$FAIL" -eq 1 ] && { say ""; say "Setup cannot continue."; exit 1; }

mkdir -p data/raw reports var
chmod +x telemetry 2>/dev/null || true
[ -f install-shell.sh ] && chmod +x install-shell.sh

say ""
say "Writing .env.telemetry (profile: $PROFILE, protocol: $PROTOCOL)"

TOOL_DETAILS_LINE='export OTEL_LOG_TOOL_DETAILS=1'
TOOL_DETAILS_NOTE='# Enabled: without this Claude Code redacts tool parameters, and you lose
# file paths, bash commands, skill names and subagent types entirely.
# Trade-off: raw files under data/raw/ will then also contain the tool
# argument JSON, in which Claude Code truncates individual values at 512
# characters (~4 KB per call). That can include short excerpts of file
# content for Write/Edit. The normalized database never stores them —
# see PRIVACY.md. Re-run ./setup.sh --minimal to turn this off.'
if [ "$PROFILE" = "minimal" ]; then
  TOOL_DETAILS_LINE='# export OTEL_LOG_TOOL_DETAILS=1   # disabled by --minimal'
  TOOL_DETAILS_NOTE='# Disabled by --minimal. No tool arguments reach disk, but file paths,
# bash commands, skill names and subagent types will all be unavailable.'
fi

if [ "$PROFILE" = "full" ]; then
  CONTENT_BLOCK='# Content export: FULLY ENABLED by ./setup.sh --full.
#
# data/raw/ and data/telemetry.db will contain the text of your prompts, Claude'"'"'s
# responses, the contents of files it reads and writes, and full API request
# bodies (which include the whole conversation context). Credential patterns
# are still redacted before anything reaches the database, but source code and
# conversation text are stored verbatim by design.
#
# Revert with:  ./setup.sh && ./telemetry init --yes && ./telemetry config privacy --disable-all
export OTEL_LOG_USER_PROMPTS=1        # prompt text on claude_code.user_prompt
export OTEL_LOG_ASSISTANT_RESPONSES=1 # response text on claude_code.assistant_response
export OTEL_LOG_TOOL_CONTENT=1        # tool input/output bodies in spans
export OTEL_LOG_RAW_API_BODIES=1      # full API request/response JSON (inline, truncated)

# Raise the truncation ceiling from the 60 KB default so long files and
# responses survive intact. Set to file:<dir> on OTEL_LOG_RAW_API_BODIES
# instead if you want untruncated bodies written to disk - be aware that
# grows very fast on long sessions.
export CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH=262144'
else
  CONTENT_BLOCK='# Content export. All four are OFF: Claude Code redacts this content by
# default and this project keeps it that way. Enable with ./setup.sh --full,
# or uncomment individually.
# export OTEL_LOG_USER_PROMPTS=1        # prompt text on claude_code.user_prompt
# export OTEL_LOG_ASSISTANT_RESPONSES=1 # response text on claude_code.assistant_response
# export OTEL_LOG_TOOL_CONTENT=1        # tool input/output bodies in spans
# export OTEL_LOG_RAW_API_BODIES=1      # full API request/response JSON

# Even if you enable those, telemetry still refuses to store the content
# until you also run:  ./telemetry config privacy --enable-all'
fi

cat > .env.telemetry <<ENVEOF
# Claude Code telemetry -> local collector on this machine.
# Generated by ./setup.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
#
#   source .env.telemetry
#   claude
#
# Every setting below is documented at
# https://code.claude.com/docs/en/monitoring-usage
# Nothing here sends data off this machine: the endpoint is loopback only.

# --- required ---------------------------------------------------------------
export CLAUDE_CODE_ENABLE_TELEMETRY=1

# --- exporters --------------------------------------------------------------
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_TRACES_EXPORTER=otlp

# Tracing is a beta feature. It is what supplies per-tool spans carrying
# file_path, full_command, skill_name and subagent_type. Comment out both
# lines if you would rather stay on the stable event surface only.
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1

# --- destination ------------------------------------------------------------
export OTEL_EXPORTER_OTLP_PROTOCOL=$PROTOCOL
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:$ENDPOINT_PORT

# --- flush intervals --------------------------------------------------------
# Defaults are 60s for metrics and 5s for logs. Shorter intervals make
# \`telemetry status\` feel responsive during an interactive session.
export OTEL_METRIC_EXPORT_INTERVAL=10000
export OTEL_LOGS_EXPORT_INTERVAL=5000
export OTEL_TRACES_EXPORT_INTERVAL=5000

# --- what gets exported -----------------------------------------------------
$TOOL_DETAILS_NOTE
$TOOL_DETAILS_LINE

$CONTENT_BLOCK

# --- attribution ------------------------------------------------------------
# Free-form labels stamped onto every metric and event. Handy later for
# team/department roll-ups. No spaces are permitted in the value.
# export OTEL_RESOURCE_ATTRIBUTES="team=solo,cost_center=rnd"
ENVEOF

ok "wrote $HERE/.env.telemetry"

say ""
say "Next steps:"
say ""
if [ "$DOCKER_OK" -eq 1 ]; then
  say "  ./telemetry start              # start the local OTLP collector"
else
  say "  ./telemetry start --no-docker  # start the built-in receiver"
  say "  # then re-run: ./setup.sh --http-json   so Claude Code speaks http/json"
fi
say "  ./telemetry doctor             # verify the whole pipeline end to end"
say "  source .env.telemetry          # in every shell you want observed"
say "  claude                         # use Claude Code normally"
say ""
say "  ./telemetry status             # is it running, are events arriving?"
say "  ./telemetry analyse            # normalize new raw telemetry"
say "  ./telemetry report --open      # build and open the HTML report"
say ""
say "Optional, and it edits ~/.claude/settings.json so it asks first:"
say "  ./telemetry init               # hooks, collector, MCP, first analyse"
say ""
