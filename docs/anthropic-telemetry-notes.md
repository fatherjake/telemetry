# What Claude Code's OpenTelemetry actually exposes

Source: **<https://code.claude.com/docs/en/monitoring-usage>**
(the older `docs.anthropic.com/en/docs/claude-code/monitoring-usage` and
`docs.claude.com/en/docs/claude-code/monitoring-usage` URLs both 301 to it).

Documentation read: **2026-08-20**.
Verified empirically against **Claude Code 2.1.237** on macOS the same day, by
running a real session into this project's receiver and dumping every
attribute received. Sections marked **[verified]** were observed in real
telemetry; **[documented]** means the docs say so but this dataset has not yet
exercised it; **[gap]** means observed behaviour differs from the docs.

---

## 1. Enabling telemetry

Telemetry is off unless you opt in. The minimum is:

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1     # required
export OTEL_METRICS_EXPORTER=otlp         # console | otlp | prometheus | none
export OTEL_LOGS_EXPORTER=otlp            # console | otlp | none
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc   # grpc | http/json | http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
```

**[verified]** These are the correct current variable names. The values in the
project brief were accurate.

### Other variables that matter here

| Variable                                                               | Effect                                            | Default         |
| ---------------------------------------------------------------------- | ------------------------------------------------- | --------------- |
| `OTEL_EXPORTER_OTLP_HEADERS`                                           | auth headers, e.g. `Authorization=Bearer …`       | none            |
| `OTEL_METRIC_EXPORT_INTERVAL`                                          | metric flush, ms                                  | `60000`         |
| `OTEL_LOGS_EXPORT_INTERVAL`                                            | event flush, ms                                   | `5000`          |
| `OTEL_EXPORTER_OTLP_{METRICS,LOGS,TRACES}_{ENDPOINT,PROTOCOL,HEADERS}` | per-signal overrides                              | inherit generic |
| `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE`                    | `delta` or `cumulative`                           | `delta`         |
| `OTEL_RESOURCE_ATTRIBUTES`                                             | custom `key=value,key=value` labels on everything | none            |
| `CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH`                                  | content truncation, UTF-16 units                  | `61440` (60 KB) |

Metric cardinality switches: `OTEL_METRICS_INCLUDE_SESSION_ID` (default true),
`OTEL_METRICS_INCLUDE_ACCOUNT_UUID` (true), `OTEL_METRICS_INCLUDE_RESOURCE_ATTRIBUTES`
(true), `OTEL_METRICS_INCLUDE_VERSION` (**false**), `OTEL_METRICS_INCLUDE_ENTRYPOINT`
(**false**).

> **[verified]** Because the last two default to _false_, `app.version` and
> `app.entrypoint` were absent from every real record. The `sessions` table
> leaves those columns NULL unless you opt in.

### Tracing (beta)

```bash
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
export OTEL_TRACES_EXPORTER=otlp
```

**[verified]** Works in 2.1.237 and is the single most useful addition for this
project — see §6.

---

## 2. Supported OTLP protocols

| Protocol        | Endpoint                                         | Notes                                                                    |
| --------------- | ------------------------------------------------ | ------------------------------------------------------------------------ |
| `grpc`          | `http://localhost:4317`                          | lowest overhead; needs a collector to receive it                         |
| `http/protobuf` | `http://localhost:4318/v1/{logs,metrics,traces}` | sends `Content-Length`                                                   |
| `http/json`     | same                                             | **what this project uses** — the only one decodable without a dependency |

mTLS is supported per protocol family (`CLAUDE_CODE_CLIENT_CERT` / `…_KEY` for
HTTP, `OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE` / `…_CLIENT_KEY` for gRPC).
Irrelevant for a loopback receiver.

---

## 3. Metrics

All exported via the standard OTLP metrics protocol, delta temporality by
default.

| Metric                                | Unit   | Key attributes                                                                                                                       |
| ------------------------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| `claude_code.session.count`           | —      | `start_type`: `fresh`/`resume`/`continue`/`agents_view`                                                                              |
| `claude_code.token.usage`             | tokens | `type`: `input`/`output`/`cacheRead`/`cacheCreation`, `model`, `query_source`, `speed`, `effort`, agent/skill/plugin/MCP attribution |
| `claude_code.cost.usage`              | USD    | `model`, `query_source`, `speed`, `effort`, agent/skill/plugin/MCP attribution                                                       |
| `claude_code.lines_of_code.count`     | —      | `type`: `added`/`removed`, `model`                                                                                                   |
| `claude_code.commit.count`            | —      | standard only                                                                                                                        |
| `claude_code.pull_request.count`      | —      | standard only                                                                                                                        |
| `claude_code.code_edit_tool.decision` | —      | `tool_name`, `decision`, `source`, `language`                                                                                        |
| `claude_code.active_time.total`       | s      | `type`: `user`/`cli`                                                                                                                 |

**[verified]** `session.count`, `token.usage`, `cost.usage`, `active_time.total`.
**[documented]** the rest — a short read-only session produced no edits, commits
or PRs, so those counters never fired.

> **`claude_code.commit.count` is a counter, not an identifier.** There is no
> commit hash anywhere in Claude Code telemetry. See §8.

---

## 4. Events (logs signal)

Fifteen event types are documented. Every one carries `event.name`,
`event.timestamp` (ISO 8601), `event.sequence` (monotonic per session) plus the
standard attributes in §5.

| Event                                              | Purpose                                               | Status           |
| -------------------------------------------------- | ----------------------------------------------------- | ---------------- |
| `claude_code.user_prompt`                          | prompt submitted                                      | **[verified]**   |
| `claude_code.assistant_response`                   | response produced (v2.1.193+)                         | **[verified]**   |
| `claude_code.api_request`                          | one model call, with cost and tokens                  | **[verified]**   |
| `claude_code.api_error`                            | failed model call                                     | **[documented]** |
| `claude_code.api_refusal`                          | model refusal, with category                          | **[documented]** |
| `claude_code.tool_decision`                        | permission decision for a tool                        | **[verified]**   |
| `claude_code.tool_result`                          | tool finished, with duration and success              | **[verified]**   |
| `claude_code.api_request_body` / `…_response_body` | full JSON bodies, only with `OTEL_LOG_RAW_API_BODIES` | **[documented]** |
| `claude_code.permission_mode_changed`              | mode transitions                                      | **[documented]** |
| `claude_code.auth`                                 | login/logout                                          | **[documented]** |
| `claude_code.mcp_server_connection`                | MCP connect/fail/disconnect                           | **[verified]**   |
| `claude_code.internal_error`                       | error class and errno only                            | **[documented]** |
| `claude_code.plugin_installed`                     | install action                                        | **[documented]** |
| `claude_code.plugin_loaded`                        | per-session plugin inventory                          | **[verified]**   |

### The three that carry the economics

**`claude_code.api_request`** — this is where cost lives:
`model`, `cost_usd`, `cost_usd_micros`, `duration_ms`, `input_tokens`,
`output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `request_id`,
`client_request_id`, `speed`, `query_source` (`main`/`subagent`/`auxiliary`),
`effort`, and attribution attributes `agent.name`, `skill.name`, `plugin.name`,
`marketplace.name`, `mcp_server.name`, `mcp_tool.name`.

**[verified]** exactly as documented, except `skill.name` / `agent.name` are
present only when a skill or subagent was actually driving the request.

**`claude_code.tool_result`** — `tool_name`, `tool_use_id`, `success`,
`duration_ms`, `error_type`, `decision_type`, `decision_source`,
`tool_input_size_bytes`, `tool_result_size_bytes`, `mcp_server_scope`, and —
**only with `OTEL_LOG_TOOL_DETAILS=1`** — `tool_parameters` and `tool_input`.

**`claude_code.tool_decision`** — `tool_name`, `tool_use_id`, `decision`
(`accept`/`reject`), `tool_source` (`builtin`/`mcp`/`sdk_host_builtin_mcp`), and
`source`, the detailed reason: `config`, `hook`, `user_permanent`,
`user_temporary`, `user_abort`, `user_reject`. Rejected tools produce a
`tool_decision` but **no** `tool_result`.

---

## 5. Standard attributes

Present on metrics and events alike:

`session.id`, `user.id` (random, persisted in `~/.claude.json`), `user.email`,
`user.account_uuid`, `user.account_id`, `organization.id`, `terminal.type`,
`app.version` (off by default), `app.entrypoint` (off by default), plus
anything from `OTEL_RESOURCE_ATTRIBUTES`.

Event-only: `prompt.id` (correlates a prompt to everything it caused),
`message.uuid`, `client_request_id`, `workspace.host_paths`,
`workflow.run_id`, `workflow.name`.

**[verified]** on real events: `session.id`, `user.id`, `user.email`,
`user.account_uuid`, `user.account_id`, `organization.id`, `terminal.type`,
`prompt.id`, `message.uuid`, `client_request_id`.

> ### **[gap] `workspace.host_paths` was never emitted**
>
> The docs list it as an event attribute. Across every event of a real 2.1.237
> session it was **absent**. This matters: it is the only documented way to
> learn the working directory from telemetry, and therefore the only documented
> way to map a session to a git repository.
>
> This project works around it two ways — see the README's _Project mapping_
> section. Do not assume it will stay absent; the ingester still reads it when
> present, and prefers it over inference.

---

## 6. Tracing spans (beta)

`CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` plus `OTEL_TRACES_EXPORTER=otlp`.

```
claude_code.interaction            (root, one per user turn)
├── claude_code.llm_request        (one per model call; tokens, ttft_ms, stop_reason)
└── claude_code.tool
    ├── claude_code.tool.blocked_on_user   (permission-prompt wait, decision, source)
    └── claude_code.tool.execution         (actual run, success, error)
```

**[verified]** all five span types observed.

`claude_code.tool` is the richest single source of activity in the whole
surface. With `OTEL_LOG_TOOL_DETAILS=1` it carries **`file_path`**,
**`full_command`**, **`skill_name`**, **`subagent_type`**, plus `tool_use_id`,
`duration_ms`, `result_tokens`, `agent_id`, `parent_agent_id`,
`workflow.run_id`.

**[gap, minor]** Real spans also carried an undocumented `span.type` attribute
(e.g. `"tool"`).

`TRACEPARENT` is exported into subprocesses, and `traceparent` headers go to
the Anthropic API and HTTP MCP servers.

---

## 7. Content export and privacy

**Everything user-generated is redacted by default.** Confirmed empirically:
the `prompt` attribute arrived as the literal string `<REDACTED>`.

| Switch                                      | Unlocks                                                                                                  | Default                 |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ----------------------- |
| `OTEL_LOG_USER_PROMPTS=1`                   | prompt text on `user_prompt`                                                                             | off                     |
| `OTEL_LOG_ASSISTANT_RESPONSES=1`            | response text on `assistant_response`                                                                    | off (follows the above) |
| `OTEL_LOG_TOOL_DETAILS=1`                   | `tool_parameters`, `tool_input`, error messages, MCP/skill/plugin names, span `file_path`/`full_command` | off                     |
| `OTEL_LOG_TOOL_CONTENT=1`                   | tool input/output bodies as span events (needs tracing)                                                  | off                     |
| `OTEL_LOG_RAW_API_BODIES=1` or `file:<dir>` | full API request/response JSON                                                                           | off                     |

Extended thinking is _always_ omitted from API body logs and is not unlocked by
`OTEL_LOG_USER_PROMPTS`.

### The one trade-off this project has to make

Nearly everything the brief asks for — file paths, bash commands, skill names,
subagent types — is gated behind `OTEL_LOG_TOOL_DETAILS=1`. Without it you get
tool _names_, counts and durations, and nothing else.

`OTEL_LOG_TOOL_DETAILS=1` is therefore **on** in the generated
`telemetry install`. The consequence, stated plainly:

- `tool_parameters` **[verified]** is a curated metadata map. For Bash it was
  `{bash_command: "wc", full_command: "wc -l < /abs/path", description: "…"}` —
  no file content.
- `tool_input` **[verified]** is the raw argument object. For `Write`/`Edit`
  that includes `content` / `new_string` / `old_string`, i.e. real file
  content. Claude Code truncates individual values at 512 characters and the
  whole payload at roughly 4 KB.
- So with tool details on, **short excerpts of file content reach
  `~/.telemetry/raw/*.jsonl`** and the normalized database. They are scrubbed of
  credentials on the way in and otherwise kept. See `PRIVACY.md`.
- Dropping `OTEL_LOG_TOOL_DETAILS` from the installed `env` block turns it off
  if that trade is unacceptable.

---

## 8. Mapping to what the brief hoped for

| Wanted                      | Available?         | How                                                                               |
| --------------------------- | ------------------ | --------------------------------------------------------------------------------- |
| sessions                    | yes                | `session.id` on everything                                                        |
| prompt identifiers          | yes                | `prompt.id`, `message.uuid`                                                       |
| prompt text                 | yes                | `OTEL_LOG_USER_PROMPTS=1`, which `telemetry install` sets                         |
| model / API calls           | yes                | `claude_code.api_request`                                                         |
| token usage                 | yes                | four token fields per API call, plus `token.usage` metric                         |
| estimated cost              | yes                | `cost_usd` / `cost_usd_micros` per API call — Claude Code's own estimate          |
| tool calls                  | yes                | `tool_decision` + `tool_result`, joined on `tool_use_id`                          |
| skill usage                 | **gated**          | `skill_name` in tool params, `skill.name` on API events                           |
| bash commands               | **gated**          | `full_command` in tool params / span                                              |
| file paths read/edited      | **gated**          | `file_path` in tool params / span                                                 |
| files _created_ vs modified | **no**             | not exposed at all; must be inferred locally (README)                             |
| subagent activity           | **gated**          | `subagent_type`, `agent.name`, `agent_id`, `parent_agent_id`                      |
| errors                      | yes                | `api_error`, `api_refusal`, `internal_error`, `success=false` on tools            |
| durations                   | yes                | `duration_ms` on API calls, tools and spans                                       |
| **git commit IDs**          | **no**             | only `claude_code.commit.count`, a counter. Hashes must come from local `git log` |
| working directory           | **no** in practice | `workspace.host_paths` is documented but was not emitted; inferred locally        |
| repo / branch / remote      | **no**             | never exposed; collected locally                                                  |

Two things the brief hoped for simply are not in the telemetry: **commit
identifiers** and **file creation semantics**. Both are reconstructed locally
and are labelled as such everywhere they appear.

---

## 9. Undocumented **events** observed

A real _interactive_ session (as opposed to `claude -p`) emitted four event
types that appear nowhere in the documentation. They arrive on the normal logs
signal with `CLAUDE_CODE_ENABLE_TELEMETRY=1`; none of them needs the detailed
tracing beta.

| Event                                 | Attributes observed                                                                                                              |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `claude_code.hook_registered`         | `hook_event`, `hook_type`, `hook_source` (`userSettings`), `safe_mode` — one per registered hook, at startup                     |
| `claude_code.hook_execution_start`    | `hook_event`, `hook_name` (e.g. `SessionStart:resume`), `num_hooks`, `managed_only`, `hook_source`, `safe_mode`                  |
| `claude_code.hook_execution_complete` | the above plus `num_success`, `num_blocking`, `num_non_blocking_error`, `num_cancelled`, `total_duration_ms`                     |
| `claude_code.compaction`              | `prompt.id`, `trigger` (`manual`/…), `success`, `duration_ms`, `pre_tokens`, `error`, `precompute_reuse` (e.g. `miss_not_ready`) |

The docs describe a `claude_code.hook` **span** gated behind detailed tracing.
These are distinct: plain log events, emitted without it.

`claude_code.compaction` is the economically interesting one — `pre_tokens`
tells you how large the context was when compaction ran, and a failure means
tokens were spent and context lost. The ingester records failed compactions in
the `errors` table with kind `compaction_failed`. The other three are kept in
the generic `events` table only.

## 9a. Undocumented attributes observed

Worth knowing, but do not depend on them.

- `claude_code.plugin_loaded` carried `agent_path_count`, `command_path_count`,
  `skill_path_count`, `enabled_via`, `has_hooks`, `has_mcp`, `host_owned_mcp`,
  `plugin.scope`, `plugin_id_hash`, `safe_mode` — none of which appear in the
  documented attribute table.
- Spans carried `span.type`.
- **`query_source` does not use the documented vocabulary at all.** The docs
  say `main` / `subagent` / `auxiliary`. Real values observed so far:
  **`repl_main_thread`** (interactive main loop), **`sdk`** (headless
  `claude -p`), **`generate_session_title`** (a background auxiliary call).
  Not one documented value has appeared. Anything grouping cost by
  `query_source` must treat the vocabulary as open — the database stores whatever
  arrives rather than validating against the documented set.

The `events` table keeps the full redacted attribute map for every event, and
the report's **Telemetry coverage** section lists anything observed that the
documentation does not mention — so new attributes surface on their own.

---

## 10. Type quirks that matter when parsing

**[verified]**, and all handled by `src/ingest.ts`:

- Numeric attributes arrive as **strings** on events (`duration_ms: "1529"`,
  `success: "true"`, `tool_input_size_bytes: "118"`) but as real numbers on
  spans (`duration_ms: 1545`).
- `tool_parameters` / `tool_input` arrive as OTLP **kvlist** structures, which
  decode to dicts — not as JSON strings, despite the docs describing them as
  "JSON".
- `cost_usd` is a double; `cost_usd_micros` is an integer. They agree.
- On Bash, `bash_command` is only the **program name**; the command line is in
  `full_command`. Reading `bash_command` alone gives you `wc`, not
  `wc -l < /path/file`.
