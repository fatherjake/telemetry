---
name: telemetry
description: >
  Query the local Claude Code telemetry database over the `telemetry` MCP server:
  what past sessions cost, which models and tools they used, how much human
  correction they took, what they committed, which files they touched, and which
  installed skills and MCP servers never fire. Use this skill whenever the user
  asks about Claude Code usage, spend, token or cache consumption, session
  history, "what did I work on", "how much did that cost", friction, rework,
  corrections, wasted skills, whether a skill should have fired, or wants ad-hoc
  SQL over their own telemetry. Covers every project on this machine, not just
  the current one.
---

# Telemetry

A read-only MCP server over a local SQLite database of Claude Code's
OpenTelemetry output. Eight tools, all prefixed `mcp__telemetry__`:

| Tool | Answers |
|---|---|
| `telemetry_overview` | totals for the whole database — start here |
| `telemetry_sessions` | the session list, most recent first |
| `telemetry_session` | one session in full: turns, skills used, skills not used, friction signals |
| `telemetry_friction` | corrections, steering, rejections, rework, across sessions |
| `telemetry_inventory` | installed skills and MCP servers vs. what actually fired |
| `telemetry_files` | files read, written, created; never-opened files under a root |
| `telemetry_schema` | table and column names, for writing SQL |
| `telemetry_sql` | read-only SQL for anything the above miss |

**The database stores facts, not verdicts.** It records what happened — every
prompt, tool call, file touch, command, commit, cost — and computes mechanical
counts over them. It contains no stored judgement about whether any of it was
good. That judgement is your job, made fresh against the question actually
asked, and this file is mostly about how to make it well.

## Before you answer anything

**This is a database, not a live stream.** The MCP reads
`~/.telemetry/telemetry.db`; the receiver writes raw JSONL, and `analyse` moves
one to the other. Sessions from the last few minutes — including the current
one — are usually absent.

Check `observed_to` in `telemetry_overview` first. If it is older than the
question being asked, say so, and offer to run `telemetry analyse` to catch
up. Never present stale numbers as current.

**The current session is never in the database.** It has not ended, so its cost
and turns do not exist yet. "How much has this cost?" cannot be answered here.

**Sessions are named by their own first prompt.** `asked_for` is the verbatim
opening instruction, truncated — not a summary. Read it as the ask. What the
session *did* is in its turns, files and commits, and the two often diverge:
a session that opened with "fix the build" and spent two hours rewriting a
subsystem still has "fix the build" as `asked_for`.

## It spans every project

One database, every project on this machine. Unless the user says otherwise,
answer across all of them, and name the project when it matters. Filter with
`telemetry_sessions(project="…")` or `WHERE project_id = …` in SQL.

---

## Judgement calls, and how to make them

Three questions come up constantly and none has a stored answer. Each is a
recipe, not a lookup.

### "Which skills should have fired and didn't?"

`telemetry_session` returns `skills_used` and `skills_available_unused` — the
latter is every skill installed *and visible to that session's project*, with
its description, that the session never called. It is the candidate list. It is
not a claim that any of them should have fired.

To turn candidates into an answer:

1. Read `asked_for` and the turns to establish what the session was actually
   trying to do — not what it touched, what it was *for*.
2. Read the `description` of each unused skill. That description is the
   contract: it is what a session would have to look like for the skill to be
   the right move.
3. A skill "should have fired" only when the work plainly falls inside its
   description. Most won't. A repo with sixty installed skills has sixty
   skills that were correctly ignored for any given piece of work.
4. **Check whether the skill's own `SKILL.md` was read.** This is the strongest
   signal available and it is free:

   ```
   telemetry_sql: SELECT path, COUNT(*) n FROM file_activity
                   WHERE session_id = ? AND path LIKE '%/skills/%/SKILL.md'
                   GROUP BY path
   ```

   A skill whose `SKILL.md` was opened and then not invoked means the agent
   considered it and declined — which points at the *description* rather than
   at discoverability, and is directly fixable. Say which case you are in.

Report at most a few, each with the evidence that made you say so. "These
sixty skills were not used" is not an answer; it is the input.

### "What went wrong in this session?"

`telemetry_session` returns `friction_signals` — mechanical counts computed on
the fly:

| Signal | Fires when |
|---|---|
| `file_written_repeatedly` | one file written ≥ 4 times |
| `read_write_alternation` | ≥ 4 flips between reading and writing one path |
| `file_read_repeatedly` | one file read ≥ 6 times and never written |
| `command_repeated` | an identical shell command run ≥ 4 times |
| `command_failing` | one program fails ≥ 3 times |
| `tool_failing` | one tool fails ≥ 3 times with the same error |
| `search_storm` | ≥ 10 searches inside a single turn |
| `cost_spike` | one turn is ≥ 30% of the session and ≥ $1 |
| `work_rejected` | ≥ 3 tool calls rejected or aborted by the human |

**A signal is not a defect.** A video frame refined eleven times is iteration
and has no fix; a config file rewritten eleven times is a loop and does.
Telling those apart means reading the turns around it, which you can do.
Thresholds are generous on purpose — expect signals on healthy sessions.

Two rules that keep this honest:

- **Never compute a rate whose denominator you did not fetch.** Six shell
  failures is not "Bash failed 46% of the time" unless you counted the shell
  commands. `telemetry_session.session` carries the totals; use them.
- **A fix must name something concrete.** "Improve context retention" is not a
  finding. "Add to CLAUDE.md: the locale files are generated, edit the source
  YAML instead" is.

### "Why did this correction happen?"

`telemetry_session.corrections` gives the correcting turns with their prompt
text at length, the gap before each, and what they cost. The reason is usually
readable in the words themselves — read the turn before it and the tool calls
in between to see what the agent did that prompted it.

Useful causes to distinguish, because each implies a different fix:

| Cause | Fix |
|---|---|
| a project fact the agent could not infer | write it down (CLAUDE.md, a doc) |
| information that existed but was wrong | update it |
| had what it needed, chose a poor method | a rule or a skill |
| the instruction genuinely permitted that reading | how work gets asked for |
| subjective refinement of creative work | **none — not a defect** |
| something broke technically | none |

That last-but-one matters. Design iteration has no right answer knowable in
advance; scoring it as a miss is dishonest and makes every other number less
trustworthy.

---

## Tool reference

### `telemetry_overview`
No arguments. Period covered, spend, tokens, tool calls, output, friction
totals. Call it first to know what you are working with.

### `telemetry_sessions`
- `limit` (int, default 25)
- `project` (string, substring filter)

One row per session, most recent first: `asked_for`, cost, duration, turns,
corrections, steers, commits, insertions/deletions, reverts.

### `telemetry_session`
- `session_id` (**required**) — full id, or any unique prefix (8 chars is
  normally enough). An ambiguous prefix returns an error listing the matches;
  pass more characters rather than guessing.
- `include_turns` (bool, default `true`) — pass `false` for just the summary.
  Do this when scanning several sessions; turn lists are long.

Returns the session row, `asked_for`, effort, commits, `skills_used`,
`mcp_servers`, `corrections`, `skills_available_unused`, `friction_signals`,
and turn-by-turn detail.

### `telemetry_friction`
- `limit` (int, default 20)

Totals plus per-session breakdown, the files reworked most, and recent
correcting turns. Use it to find *which* sessions to look at; use
`telemetry_session` to understand one.

### `telemetry_inventory`
No arguments. Every installed skill and configured MCP server against observed
use, plus subagents. `skills_never_used` and `mcp_never_used` are the point: a
server that connects every session and is never called is pure startup latency,
and a skill unused over a month is either dead or badly described.

Note `skills_used_but_not_installed` — that means a skill fired that the
inventory scan cannot see, usually a plugin skill.

### `telemetry_files`
- `under` (string) — path prefix filter
- `unread_under` (string) — a directory to check for never-opened files
- `pattern` (string, default `*.md`) — used with `unread_under`
- `limit` (int, default 40)

`unread_under` is the knowledge-base gap analysis: it walks the directory on
disk and subtracts everything ever opened. Judge relevance yourself — a
contacts directory going unread by a coding agent is correct behaviour; a
service spec going unread while that service is being worked on is a real gap.

### `telemetry_schema` / `telemetry_sql`
`SELECT`/`WITH`/`EXPLAIN`/`PRAGMA` only; anything else is refused, and the
connection is opened read-only, so writes cannot succeed even in principle.
Results cap at 500 rows and say when they were truncated.

Views: `costs`, `session_summary`, `project_summary`.

Key tables: `sessions`, `turns`, `api_calls`, `tool_calls`, `skill_calls`,
`skill_inventory`, `mcp_inventory`, `prompts`, `responses`, `file_activity`,
`bash_activity`, `subagent_activity`, `errors`, `git_activity`, `reverts`,
`file_rework`. Costs live on `api_calls.cost_usd`; token columns are
`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`.

Call `telemetry_schema` before writing SQL rather than guessing column names.

---

## Worked queries

Skills used across the last five sessions:

```sql
SELECT s.session_id, k.skill_name, COUNT(*) n
  FROM skill_calls k
  JOIN (SELECT session_id FROM sessions ORDER BY first_seen DESC LIMIT 5) s
    ON s.session_id = k.session_id
 GROUP BY 1, 2 ORDER BY 1, n DESC
```

Skills installed but never invoked anywhere:

```sql
SELECT name, scope, description FROM skill_inventory
 WHERE name NOT IN (SELECT DISTINCT skill_name FROM skill_calls
                     WHERE skill_name IS NOT NULL)
```

Where the money went, by project:

```sql
SELECT p.project_name, ROUND(SUM(a.cost_usd), 2) cost, COUNT(DISTINCT a.session_id) sessions
  FROM api_calls a LEFT JOIN projects p ON p.project_id = a.project_id
 WHERE a.outcome = 'ok' GROUP BY 1 ORDER BY cost DESC
```

Correction rate per session, worst first:

```sql
SELECT session_id, COUNT(*) turns,
       SUM(is_correction) corrections,
       ROUND(1.0 * SUM(is_correction) / COUNT(*), 2) rate,
       ROUND(SUM(CASE WHEN is_correction THEN cost_usd ELSE 0 END), 2) correction_cost
  FROM turns WHERE COALESCE(is_system, 0) = 0
 GROUP BY 1 HAVING turns >= 5 ORDER BY rate DESC LIMIT 15
```

MCP servers that connect but are never called:

```sql
SELECT i.name, i.scope FROM mcp_inventory i
 WHERE i.name NOT IN (SELECT DISTINCT mcp_server_name FROM tool_calls
                       WHERE mcp_server_name IS NOT NULL)
```

## Reading the numbers honestly

- **Cache reads dwarf input tokens** and cost far less. Quoting a single
  "token" figure is misleading — break it out.
- **Cost per session is not cost per value.** A long session that shipped a
  feature and a long session that thrashed look similar in spend and completely
  different in `telemetry_friction`. Report both.
- **Corrections are a rate, not a count.** Eight corrections over ninety turns
  is different from eight over twelve.
- **Turn labels are heuristics.** `is_correction` and `is_steering` come from
  phrase and length cues; `label_source` and `correction_cue` say what fired.
  When a specific label matters to the answer, read the prompt and check.
- **`(unattributed)` sessions** are real work whose repo could not be resolved
  (no git, or the hook missed). Don't silently drop them from totals.
- **Commit attribution is inference.** A commit is credited to the session that
  touched its files, falling back to the time window. `git_activity.attribution`
  says which rule fired.
- **Prompt text may be absent.** If content storage was off when a session was
  ingested, `prompt_text` is NULL and `asked_for` comes back null. That is a
  storage-policy fact, not an empty session — say so rather than reporting
  nothing happened.

## If the tools aren't there

The server is registered per user, so it should be in every session. If the
`mcp__telemetry__*` tools are missing:

    claude mcp list                      # expect: telemetry ... ✔ Connected
    telemetry mcp --register

Registration only takes effect in **new** sessions. If it reports no database
at `~/.telemetry/telemetry.db`, nothing has been ingested yet: run
`telemetry analyse`.

## Privacy

Depending on how telemetry was configured, this database can hold full prompt
and response text, file paths, and bash commands. It is local and read-only
here. Do not paste its contents into anything that leaves the machine without
the user asking for exactly that.
