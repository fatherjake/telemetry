---
name: telemetry
description: >
  Query the local Claude Code telemetry database over the `telemetry` MCP server:
  what past sessions cost, which models and tools they used, how much human
  correction they took, what they committed and shipped, which files and docs
  they touched, and which installed skills and MCP servers never fire. Use this
  skill whenever the user asks about Claude Code usage, spend, token or cache
  consumption, session history, "what did I work on", "how much did that cost",
  friction, rework, corrections, wasted skills, or wants ad-hoc SQL over their
  own telemetry. Covers every project on this machine, not just the current one.
---

# Telemetry

A read-only MCP server over a local SQLite database of Claude Code's OpenTelemetry
output. Nine tools, all prefixed `mcp__telemetry__`:

| Tool | Answers |
|---|---|
| `telemetry_overview` | totals for the whole database — start here |
| `telemetry_sessions` | the session list, most recent first |
| `telemetry_session` | one session in full, including its turns |
| `telemetry_friction` | corrections, steering, rejections, rework |
| `telemetry_inventory` | installed skills and MCP servers vs. what actually fired |
| `telemetry_files` | files read, written, created; never-opened files |
| `telemetry_docs` | knowledge-base coverage and cold spots |
| `telemetry_schema` | table and column names, for writing SQL |
| `telemetry_sql` | read-only SQL for anything the above miss |

## Before you answer anything

**This is a database, not a live stream.** The MCP reads `data/telemetry.db`;
the collector writes raw JSONL, and `ingest` moves one to the other. Sessions
from the last few minutes — including the current one — are usually absent.

Check `observed_to` in `telemetry_overview` first. If it is older than the question
being asked, say so, and offer to run `./telemetry analyse` (from the telemetry
repo) to catch up. Never present stale numbers as current.

**The current session is never in the database.** It has not ended, so its cost
and turns do not exist yet. Questions like "how much has this cost?" cannot be
answered from here.

## It spans every project

The database is per machine, not per repo. A session in any project on this
machine is in here. When the question is about the current project, filter:

    telemetry_sessions(project: "myrepo")

`project` is a case-insensitive substring matched against both project name and
id. Get exact names with:

    telemetry_sql(query: "SELECT project_name, COUNT(*) n FROM sessions
                       JOIN projects USING(project_id) GROUP BY 1 ORDER BY n DESC")

Watch for one repo appearing under two names — projects are keyed on repo
identity, so renaming or moving a checkout starts a new row while the old one
keeps its history. Ask before assuming two similar names are distinct work.

## The tools in detail

### `telemetry_overview` — no parameters
Observation window, spend, tokens, tool calls, what shipped, friction totals.
Read `observed_from`/`observed_to` before trusting anything else.

### `telemetry_sessions`
- `limit` (int, default 25)
- `project` (string, substring filter)

One row per session, most recent first: title, cost, duration, turns,
corrections, steers, commits, insertions/deletions, prod deploys, reverts,
doc gaps. `described: false` means the title is a placeholder — see
*Enrichment* below.

### `telemetry_session`
- `session_id` (**required**) — full id, or any unique prefix (8 chars is
  normally enough). An ambiguous prefix returns an error listing the matches;
  pass more characters rather than guessing.
- `include_turns` (bool, default `true`) — pass `false` for just the summary.
  Do this when scanning several sessions; turn lists are long.

Returns the session row, description, effort, commits, deployments, skills and
MCP servers used, corrections, missed skills, docs read, doc gaps, diagnosis
findings, and turn-by-turn detail.

### `telemetry_friction`
- `limit` (int, default 20)

Totals plus per-session friction, the files edited over and over (`rework_files`),
and recent corrections. This is the cost of getting it wrong: a high-cost session
with zero corrections went well; a cheap one with eight did not.

### `telemetry_inventory` — no parameters
Installed skills and configured MCP servers joined to actual invocations. The
**zero-invocation entries are the point** — they are the things installed and
forgotten. Also returns `skills_used_but_not_installed` (fired but not found on
disk, usually a stale or moved skill) and subagent usage.

### `telemetry_files`
- `under` (string) — path prefix filter
- `unread_under` (string) — directory to scan for files nothing ever opened
- `pattern` (string, default `*.md`) — used with `unread_under`
- `limit` (int, default 40)

File access, hot files, hot directories, created files. `unread_under` is the
interesting one: it answers "what documentation is nobody reading".

### `telemetry_docs`
- `root` (string) — limit to one scanned root
- `limit` (int, default 30) — cold spots returned

Needs a knowledge base to have been scanned (`./telemetry enrich --what docs --root <dir>`);
without one it returns `documents: 0` and says so. A *cold spot* is an
agent-facing document nothing has ever opened. Human-only docs are excluded
deliberately — don't report them as gaps.

### `telemetry_schema`
- `table` (string) — substring filter on table names

42 tables and 3 views. Call this before writing SQL rather than guessing column
names.

### `telemetry_sql`
- `query` (**required**)
- `limit` (int, default 100, hard cap 500)

Must start with `SELECT`, `WITH`, `EXPLAIN` or `PRAGMA`; anything else is
refused, and the connection is opened read-only, so writes cannot succeed even
by accident. Check `truncated` in the response — a truncated result silently
looks like a complete one if you don't.

Prefer the purpose-built tools when they fit; they apply joins and rounding you
would otherwise repeat. Reach for SQL for grouping, trends and correlations.

## Enrichment: fields that may be empty

Several fields exist only after an enrichment pass has been run, and those
passes cost money because they call a model. If a field is empty, that usually
means the pass hasn't run — not that the answer is zero. Say which, and offer
the command.

| Field | Pass |
|---|---|
| session titles / descriptions | `./telemetry enrich --what describe` |
| correction vs. steering labels | `--what classify` |
| correction causes and fixes | `--what corrections` |
| `findings` in `telemetry_session` | `--what diagnose` |
| `missed_skills` | `--what skills` |
| doc profiling and gaps | `--what docs --root <dir>` |

The tools return an explicit note when a pass is missing — pass that on rather
than reporting a hollow result.

## Useful SQL

Spend by day:

    SELECT date(ts) d, ROUND(SUM(cost_usd),2) usd, COUNT(*) calls
    FROM api_calls GROUP BY 1 ORDER BY 1 DESC

Spend by model:

    SELECT model, COUNT(*) calls, ROUND(SUM(cost_usd),2) usd,
           SUM(output_tokens) out_tok, SUM(cache_read_tokens) cache_read
    FROM api_calls WHERE model IS NOT NULL GROUP BY 1 ORDER BY usd DESC

Which tools fail most:

    SELECT tool_name, COUNT(*) n, SUM(NOT success) failures
    FROM tool_calls GROUP BY 1 HAVING failures > 0 ORDER BY failures DESC

Permission rejections by tool:

    SELECT tool_name, COUNT(*) n FROM tool_calls
    WHERE decision = 'reject' GROUP BY 1 ORDER BY n DESC

Three views exist for the common shapes: `costs`, `session_summary`,
`project_summary`.

Key tables: `sessions`, `turns`, `api_calls`, `tool_calls`, `prompts`,
`responses`, `file_activity`, `bash_activity`, `skill_calls`, `errors`,
`git_activity`, `deployments`. Costs live on `api_calls.cost_usd`; token
columns are `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_creation_tokens`.

## Reading the numbers honestly

- **Cache reads dwarf input tokens** and cost far less. Quoting a single "token"
  figure is misleading — break it out.
- **Cost per session is not cost per value.** A long session that shipped a
  feature and a long session that thrashed look similar in spend and completely
  different in `telemetry_friction`. Report both.
- **Corrections are a rate, not a count.** Eight corrections over ninety turns
  is different from eight over twelve.
- **`(unattributed)` sessions** are real work whose repo could not be resolved
  (no git, or the hook missed). Don't silently drop them from totals.

## If the tools aren't there

The server is registered per user, so it should be in every session. If the
`mcp__telemetry__*` tools are missing:

    claude mcp list                      # expect: telemetry ... ✔ Connected
    cd <telemetry repo> && ./telemetry mcp --register

Registration only takes effect in **new** sessions. If it reports no database at
`data/telemetry.db`, nothing has been ingested yet: run `./telemetry analyse`.

## Privacy

Depending on how telemetry was configured, this database can hold full prompt
and response text, file paths, and bash commands. It is local and read-only
here. Do not paste its contents into anything that leaves the machine without
the user asking for exactly that.
