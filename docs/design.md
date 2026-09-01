# How it works

The design notes for Agent Telemetry: what is stored, how each derived number
is arrived at, and which parts are inference rather than fact.

Start with the [README](../README.md) for installation and day-to-day use.

---

## Where data lives

Everything is under `~/.telemetry`. Point `TELEMETRY_HOME` somewhere else and
every path below follows it.

| Path                           | What                                                          |
| ------------------------------ | ------------------------------------------------------------- |
| `raw/*.jsonl`                  | append-only OTLP archive, one export request per line         |
| `telemetry.db`                 | normalized SQLite database                                    |
| `session_context.jsonl`        | append-only cwd/repo/branch/HEAD written by the session hooks |
| `policy.json`, `ignore`        | storage policy and ignored path globs                         |
| `receiver.log`, `receiver.pid` | receiver runtime state                                        |

The raw files, plus `session_context.jsonl`, are the source of truth. The
database is derived and disposable: delete it, run `telemetry analyse`, and
it rebuilds completely from those files — there is nothing in it that was paid
for and cannot be recomputed. The hooks deliberately write to
`session_context.jsonl` rather than to SQLite, both so a rebuild cannot lose
that context and so a hook running at session start never contends for a
database lock. That is the whole point of keeping the raw stream — when the
schema grows a column, old telemetry can be reinterpreted rather than
re-collected.

Raw files roll over at 64 MB and are never deleted; pruning old ones is your
call, and costs you only the ability to rebuild that far back.

---

## Database schema

Every normalized row keeps `raw_json` plus a `(source_path, source_line)`
pointer back into the archive. Column names mirror Anthropic's attribute names;
tables prefixed `local_` come from reading your filesystem, not from telemetry.

| Table                              | Rows are                                                           | Source                                         |
| ---------------------------------- | ------------------------------------------------------------------ | ---------------------------------------------- |
| `projects`                         | a git repository                                                   | local git                                      |
| `sessions`                         | a Claude Code session                                              | `session.id` + standard attributes             |
| `events`                           | every log record, whatever its type                                | logs signal                                    |
| `metric_points`                    | every metric data point                                            | metrics signal                                 |
| `spans`                            | every span                                                         | traces signal                                  |
| `api_calls`                        | one model call                                                     | `api_request`/`api_error`/`api_refusal`        |
| `tool_calls`                       | one tool invocation                                                | `tool_decision` + `tool_result` + span, merged |
| `skill_calls`                      | a skill invocation                                                 | tool params, `skill.name`                      |
| `file_activity`                    | one read/edit/write of one path                                    | tool params, spans                             |
| `bash_activity`                    | one command                                                        | `full_command`                                 |
| `subagent_activity`                | a subagent run                                                     | `subagent_type`, `agent.name`                  |
| `prompts` / `responses`            | prompt and response metadata                                       | `user_prompt`, `assistant_response`            |
| `errors`                           | api errors, refusals, tool failures, internal errors, MCP failures | several events                                 |
| `turns`                            | one `prompt.id`: a human instruction and everything it cost        | derived                                        |
| `git_activity`                     | a commit                                                           | **local git only**                             |
| `git_commit_files`                 | one file in one commit                                             | **local git only**                             |
| `reverts`                          | a commit that undid another                                        | **local git only**                             |
| `local_session_git_context`        | cwd/repo/branch/HEAD at session start and end                      | **local git only**                             |
| `raw_files`, `ingest_runs`, `meta` | ingest bookkeeping                                                 | —                                              |

Views: `costs` (cost and tokens by session/day/model/source/skill/agent),
`session_summary`, `project_summary`.

`tool_calls` is the interesting one. A single tool invocation shows up in up to
three places — the permission decision, the result, and a span — and they are
merged on `(session_id, tool_use_id)` with `COALESCE`, so no signal overwrites
another. `tool_calls.sources` records which contributed.

---

## Project mapping

Each session is resolved to a `project_id` by the first of these that works:

1. **Session hook** (`telemetry init`) — the real `cwd`, captured at
   `SessionStart` and again at `SessionEnd`, along with repo root, remote,
   branch and HEAD, appended to `~/.telemetry/session_context.jsonl`. Most
   reliable, and the only thing that maps a session which ran no tools at all.
2. **`workspace.host_paths`** — the documented telemetry attribute. _Claude
   Code 2.1.237 does not actually emit it_, so in practice this rarely fires.
   Read anyway, in case it returns.
3. **Path inference** — absolute paths are extracted from tool arguments and
   bash command lines, each is resolved to a git repository, and the most
   frequently referenced repository wins. This is what carries real sessions
   today.
4. Otherwise the session lands in `(unattributed)`.

Which one was used is recorded per session in
`sessions.project_detection_method`, so you can always tell how much to trust a
mapping.

`project_id` is a stable hash: `remote:<hash of host/org/repo>` when a remote
exists — so the same repository groups across clones and across ssh/https
forms — falling back to `root:<hash of path>` then `dir:<hash>`.

### The hooks are optional and explicit

`telemetry init` edits `~/.claude/settings.json`. It shows the exact JSON,
asks for confirmation, and backs up the existing file first.
`telemetry config uninstall` reverses it. Nothing else in this project
touches configuration outside `~/.telemetry`.

---

## File activity, and the created-vs-modified problem

Claude Code reports that a `Write` happened. It does not report whether the
file already existed. So creation is **inferred**, and every row records which
method was used and how much to trust it:

| `create_method` | `create_confidence` | Logic                                                                    |
| --------------- | ------------------- | ------------------------------------------------------------------------ |
| `git_added`     | high                | the file's first `A` commit in the repo is at or after the session start |
| `no_prior_read` | medium              | no earlier `Read` or `Edit` of that path anywhere in the database        |
| `unknown`       | none                | not in a repo and no prior activity to reason from                       |

Both are heuristics. `git_added` is wrong if the file was created before the
session and committed during it. `no_prior_read` is wrong if Claude wrote a
file it had seen in a previous, unobserved session. Treat "files created" as an
estimate, and use `create_confidence` to filter.

```bash
telemetry sql "select count(distinct path) from file_activity where operation='read'"
telemetry sql "select path, count(*) n from file_activity group by path order by n desc limit 10"
telemetry sql "select path from file_activity where created=1 and create_confidence='high'"
```

## Git activity

**Claude Code never emits a commit hash.** It emits
`claude_code.commit.count`, a counter. Everything in `git_activity` therefore
comes from read-only `git log` against repositories seen in telemetry.

A commit is attributed to a session by **which session touched the files the
commit changed**, falling back to the session time window only when there is no
file evidence. Time alone is a poor signal: sessions run concurrently, and "the
one that started most recently" credited a commit to a session that never
opened either changed file — leaving the session that did the work looking
commit-less. File evidence moved attributed spend from 62% to 99.9%.

`git_activity.attribution` records which rule fired, so the inference is
visible rather than implied. **Reverts** are detected from commit subjects and
are the clearest negative signal available: work that had to be undone.

No git command this project runs can modify a repository.

---

## The session — the unit of work

**One session is one item.** It has its own prompts, its own cost, its own
commits, and its own turns. Nothing is grouped, shared or apportioned, which
means every number on a session is a fact about that session rather than a
share of something larger.

An earlier version grouped sessions into _workflows_ by conventional-commit
scope, on the theory that a piece of work spanning three sessions should be
counted once. It bought a truer unit at the price of every number becoming an
estimate. The session is coarser and honest.

A session earns its place by having produced **at least one prompt of its
own**. Hooks record a row for every session that starts, including ones that
exported no telemetry, and a session can also survive with tool activity but no
prompt text — the storage policy in force when it was ingested may have dropped
the text. Either way there is nothing to name it by and nothing to read, so it
stays out of the list. The raw archive still holds those events, so a rebuild
under a policy that stores content brings them back.

### What a session is called

Its **first prompt**, verbatim and truncated — the thing you actually typed to
start it. Nothing is generated and nothing is summarised.

That is a deliberate reversal. An earlier version spent a Haiku call per
session turning its prompts into a one-line goal, so a list of sessions read as
sentences rather than hex ids. It worked, and it was still the wrong shape: the
thing reading that list is a model, which can summarise a hundred first prompts
in the same breath as answering the question, and would rather see the exact
words than someone else's paraphrase of them.

Read `asked_for` as the ask, not as a description of what happened. A session
that opened with "fix the build" and spent two hours rewriting a subsystem
still says "fix the build" — what it _did_ is in its turns, files and commits.

Sessions that produced no commit at all — QA, research, writing, debugging that
didn't land — are peers in the list, not a leftover category. They were **a
third of the spend** here.

---

## Human effort

Claude Code emits no notion of "the agent got this wrong", so this is built
from proxies — and every one is labelled as such, with the cue that fired
recorded next to it so any number can be audited.

The grain is the **turn**: `prompt.id` is stamped on every event a single human
instruction causes, so one row in `turns` is one instruction and everything it
cost.

| Signal         | Meaning                                                    | How                                                                                 |
| -------------- | ---------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| correction     | the developer is telling the agent it got something wrong  | a phrase list, recorded in `correction_cue`                                         |
| steering nudge | a nudge that continues or adjusts work already in progress | prompt ≤ 45 chars                                                                   |
| rework         | the same file returned to across separate turns            | `file_rework`                                                                       |
| override       | you overruled a tool decision                              | `decision_source` = `user_reject` / `user_abort` — reported directly by Claude Code |
| gap before     | how long you spent reading before replying                 | time since the previous turn ended                                                  |

The `telemetry_friction` MCP tool totals these per session and shows the share
of spend that went to correction turns.

### The labels are heuristics, and say so

`is_correction` and `is_steering` come from a phrase list and a length cue.
Both are wrong sometimes: the length heuristic conflates _short_ with
_low-information_, and `promote to production` is 21 characters and ships a
release.

They are kept because they are free and roughly right in bulk — a correction
_rate_ across ninety turns survives a few misreads. Every row carries
`label_source`, `label_confidence` and the `correction_cue` that fired, so any
single label can be checked, and `prompt_text` is right there when it matters.

An earlier version paid a small model to label each turn properly. It was more
accurate and it is gone: the caller reading these rows over MCP can read the
prompt itself, which is strictly more information than a stored label.

### Harness-injected prompts

Not every `user_prompt` event is something a human typed. Monitor
notifications, system reminders and slash-command echoes arrive as prompts and
were inflating turn counts. They are flagged `turns.is_system`, excluded from
every effort metric, and reported separately with their cost — they are real
spend, just not human effort.

---

## File access, including through the shell

Most real file access never touches the file tools: docs get read with `grep`
and `sed`, files get written with `cat >`. A database built only on tool events
reports zero activity for a file that was read a dozen times.

`src/shellfiles.ts` parses stored bash commands for file access — redirects,
`sed -i`, `cp`/`mv`/`rm`, `curl -o`, readers like `cat`/`head`/`grep`/`wc`,
directory listings, and Python heredocs that call `read_text` / `write_text`.
It is quote-aware (a pipe inside `grep "a\|b"` is not a separator), handles
filenames with spaces, resolves relative paths against a leading `cd` or the
session's working directory, and skips a program's pattern argument so
`grep 's/x/y/' f.md` records `f.md` rather than the pattern.

Every row records `via` (`tool` or `shell`) and, for shell rows, an
`op_confidence`:

| Confidence | Example                                                                |
| ---------- | ---------------------------------------------------------------------- |
| high       | `> file`, `cat file`, `sed -i … file`, `cp a b`, `rm file`             |
| medium     | `grep pat file`, `sed -n … file`, a Python heredoc calling `read_text` |

A recursive grep over a directory is recorded as a **search of the directory**,
not a read of every file inside it.

Shell rows are **rebuilt from `bash_activity` on every `analyse`**, so parser
improvements apply to history without re-reading the raw archive.

Inline interpreter code (`python3 -c "…"`) is not treated as a path, but _is_
scanned for real file IO, so `Image.open('/a/img.png')` records the image.
`git mv` / `git rm` / `git checkout --` are recorded as the file operations
they are.

A **plausibility guard** rejects tokens that cannot be filenames — bare
numbers, shell fragments with trailing punctuation (`28:`, `120]`), anything
with no letters in its final component. A wrong path is worse than a missing
one: it invents activity against a file nobody touched.

**Known undercount:** a heredoc that builds a path from variables, or a tool
invoked through a wrapper script, will not be detected. Shell parsing is
inference, and `op_confidence` says how much to trust each row.

---

## Time

Durations come from tracing spans and from turn boundaries. Two clocks exist
and they are not interchangeable:

- **Active time** — the sum of turn wall-clock. What the work actually took.
- **Elapsed time** — active time plus the idle gaps between your turns.

Inside a turn, `llm_request`, `tool.execution` and `tool.blocked_on_user`
**overlap** (tools run in parallel, and a model request happens inside a turn),
so they are reported as separate magnitudes and never stacked into a total.
`tool.blocked_on_user` is the one worth watching: it is the agent sitting idle
waiting for you to answer a permission prompt.

Skill timing is an **upper bound**, not a measurement: a `Skill` tool call
takes milliseconds because it only loads instructions, so what is reported is
the wall-clock of the turns the skill was active in — which included other work
too.

---

## Skills and MCP inventory

Usage alone cannot tell you what _isn't_ being used. Every `analyse` scans the
same configuration Claude Code reads — user, project and plugin skill
directories, and `mcpServers` in `~/.claude.json`, `.mcp.json` and settings —
then joins it to observed usage.

That surfaces three things worth acting on:

- **Dead skills** — installed, never invoked. Over a month this is either a
  skill that should be deleted or one that should be firing and isn't. Which
  of the two is a judgement; `telemetry_session` hands the caller every skill
  a given session had available and did not call, with its description, so the
  question can be asked against one piece of work rather than in the abstract.
  The strongest evidence is free and in `file_activity`: a skill whose
  `SKILL.md` was _read_ and then not invoked was considered and declined, which
  points at the description rather than at discoverability.
- **`connected, unused` servers** — started every session, never called. Pure
  startup latency and context cost.
- **`failing` servers** — connection attempts that error, every session.

MCP `args` and `env` routinely carry tokens, so only the command's program name
is stored. A server configured in more than one scope is marked `*`; its usage
appears on each row and those are the same calls, not additional ones.

---

## Knowledge-base coverage

The point of writing something down is that it gets consulted, so the useful
question is which documents never were:

```
telemetry_files(unread_under="~/notes", pattern="*.md")
```

That walks the directory on disk and subtracts everything ever opened —
including through the shell, so a doc read with `grep` or `sed` counts. A
recursive grep over a directory is recorded as a **search of the directory**,
not a read of each file inside it; crediting all of them would report a healthy
knowledge base where there isn't one.

Which of the unread ones _matter_ is a judgement, and it is left to the caller.
A contacts directory going unread by a coding agent is correct behaviour; a
service specification going unread while that service is being worked on is a
real gap. An earlier version paid a model to pre-classify every document by
audience and agent-relevance so that "cold spot" could mean something narrower.
It needed a scan command, two tables, a sidecar and a `--titles-only` flag to
keep business-sensitive material off the wire — all to answer a question the
caller can answer better with the file list and the session in front of it.

---

## Cost

Every figure comes from Claude Code's own `cost_usd` on the `api_request`
event. Nothing is recomputed from a price list, so nothing drifts when pricing
changes.

Aggregations available: by session, day, project, model, query source
(`main`/`subagent`/`auxiliary`), skill and agent — see the `costs` view.

**Where cost cannot be attributed precisely:**

- Cost belongs to an _API call_, not to a tool call. A single request that
  drove five tool calls is one cost figure. Splitting it across tools would be
  invented, so it is not attempted.
- Skill and agent attribution exists only when Claude Code stamps `skill.name`
  or `agent.name` on the request. Unstamped requests are counted as
  unattributed rather than spread around.
- Third-party skill and plugin names arrive as `custom` unless
  `OTEL_LOG_TOOL_DETAILS=1`, so they collapse into one bucket.
- Sessions that never resolve to a project keep their cost in
  `(unattributed)`.

---

## Judgement lives in the caller

This project computes friction **signals** and stops there.

`telemetry_session` returns them, recomputed on every call — no stored verdict,
no cache, nothing that can go stale:

| Signal                    | Fires when                                      |
| ------------------------- | ----------------------------------------------- |
| `file_written_repeatedly` | one file written ≥ 4 times                      |
| `read_write_alternation`  | ≥ 4 flips between reading and writing one path  |
| `file_read_repeatedly`    | one file read ≥ 6 times and never written       |
| `command_repeated`        | an identical shell command run ≥ 4 times        |
| `command_failing`         | one program fails ≥ 3 times                     |
| `tool_failing`            | one tool fails ≥ 3 times with the same error    |
| `search_storm`            | ≥ 10 searches inside a single turn              |
| `cost_spike`              | one turn is ≥ 30% of the session and ≥ $1       |
| `work_rejected`           | ≥ 3 tool calls rejected or aborted by the human |

Thresholds are deliberately generous: a false negative costs nothing, a false
positive costs the reader's trust.

**A signal is not a defect.** A video frame refined eleven times is iteration
and has no fix; a config file rewritten eleven times is a loop and does.
Telling those apart means reading the turns around it — and the thing asking
is a model with the whole database in reach, so it reads them.

### What used to be here

Four model-backed passes: one that diagnosed each session, one that traced
every correction to a cause, one that judged which installed skill should have
fired, one that profiled a knowledge base for audience and relevance. Each ran
a small model, cached its verdicts to a sidecar, re-imported them on `analyse`,
and owned a table.

They are gone, and the argument against them is the same in every case:

- **The verdict was computed by a weaker model than the one reading it**, from
  a summary rather than from the data, and then frozen.
- **It could not be argued with.** A stored `cause: ambiguous_request` is a
  column. A caller that can read the correcting turn, the instruction before
  it, and every tool call in between can be told "no, look again".
- **It answered a question nobody had asked yet.** "Which skill should have
  fired" is only answerable against a _particular_ notion of what the session
  was for — and that notion arrives with the question.
- **Staleness had to be managed.** Re-running under changed rules meant a
  `--redo` flag, sidecar invalidation, and a real risk of a verdict outliving
  the logic that produced it.

What replaced them is `.claude/skills/telemetry/SKILL.md`: not a wrapper around
the tools, but the method — how to turn `skills_available_unused` into an
answer, which denominators to fetch before quoting a rate, why design iteration
must not be scored as a defect. The reasoning that used to live in six prompt
templates now lives in one document the caller reads.

Two guardrails from that era were worth keeping and are written into the skill:
**never compute a rate whose denominator you did not fetch** (six shell
failures became "Bash failed in 46% of turns" from a model that had the
numerator only), and **a fix must name something concrete** — "improve context
retention" is not a finding, "add this sentence to CLAUDE.md" is.

### One caveat about the counts

Shell-derived file rows resolve relative paths against a best-guess working
directory, and when that guess is one level off the same file lands under two
repo-relative paths — `src/a/B.tsx` and `web-app/src/a/B.tsx`. Left alone
they read as two files each edited half as often. `fold_paths()` merges them on
the suffix relation before any signal is computed. It is a heuristic; the
alternative is knowingly wrong counts.

---

## Inspecting data by hand

```bash
telemetry sql "select event_name, count(*) from events group by 1 order by 2 desc"
telemetry sql "select * from costs where day = date('now')" --json
sqlite3 ~/.telemetry/telemetry.db ".schema tool_calls"
sqlite3 ~/.telemetry/telemetry.db -header -column "select * from session_summary"

# the raw archive is just JSON lines
tail -1 ~/.telemetry/raw/logs.jsonl | node -e 'process.stdin.on("data",d=>console.log(JSON.stringify(JSON.parse(d),null,2)))'
grep -c api_request ~/.telemetry/raw/logs.jsonl
```

To see every attribute of a specific event, including ones this schema does not
normalize:

```bash
telemetry sql "select attrs_json from events where event_name='plugin_loaded' limit 1" --json
```

---
